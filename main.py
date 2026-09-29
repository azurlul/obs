# ========================
# Imports & Initial Setup
# ========================

import json
import os
import struct
import sqlite3
import asyncio
import coc
from ppadb.client_async import ClientAsync as AdbClient
from ppadb.sync_async import SyncAsync

# ppadb's async push feeds a float mtime to struct.pack('<I', ...); coerce to int.
# Really had a hard time trying to figure out a fix lol
SyncAsync._little_endian = staticmethod(lambda n: struct.pack('<I', int(n)))

# ============================
# Local Database
# ============================

def open_db(path:str="database/players.db") -> sqlite3.Connection:
    """Open the local SQLite DB and ensure the players table exists."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")  # concurrent reads + faster writes
    db.execute("""
        CREATE TABLE IF NOT EXISTS players (
            tag        TEXT PRIMARY KEY,
            name       TEXT,
            townhall   INTEGER,
            data       TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()
    return db


def scanned(path:str="scanned.txt") -> set:
    """Return the set of tags already processed in a previous run."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def write2file(path:str, text:str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")

# ============================
# Log Streaming (one asyncio task per device)
# ============================

class LogStream:
    """
    Polls a device's logfile with `cat` and publishes each new obstacles dump.
    """
    def __init__(self, dev):
        self.dev = dev
        self.latest = None                # last obstacles dict published
        self.event = asyncio.Event()      # set when a *new* dump arrives
        self._task = asyncio.create_task(self._reader())

    async def _reader(self):
        while True:
            try:
                out = await self.dev.shell("su -c 'cat /data/data/com.supercell.clashofclans/files/logfile.log'")
                idx = out.find('"obstacles":')
                if idx != -1:
                    try:
                        obstacles, _ = json.JSONDecoder().raw_decode(out, idx + len('"obstacles":'))
                        self._publish({"obstacles": obstacles})
                    except (json.JSONDecodeError, ValueError):
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.4)

    def _publish(self, match):
        if match == self.latest:          # dedup identical consecutive dumps
            return
        self.latest = match
        self.event.set()

    def arm(self):
        """Call right before opening a profile: wait for the *next* dump."""
        self.event.clear()

    async def wait(self, timeout):
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.latest

    def stop(self):
        self._task.cancel()

# =========================
# Helper Function to Parse
# =========================

def parse_obs(json_data:dict) -> tuple:
    """
    Parses the obstacles data from JSON and counts specific items by ID.
    
    Returns:
        Tuple of (total_count, count_8000015, count_8000021, count_8000028)
    """
    c1 = c2 = c3 = 0
    for obs in json_data.get("obstacles", []):
        d = obs.get("data")
        if d == 8000015:
            c1 += 1
        elif d == 8000021:
            c2 += 1
        elif d == 8000028:
            c3 += 1
    return (c1 + c2 + c3, c1, c2, c3)

# =========================
# Helper Function to save the Json Data along with the tag, name & townhall inside the DB
# =========================

def save2db(db:sqlite3.Connection, tag:str, name:str, townhall:int, json_data:dict) -> None:
    """
    Upsert the scan data into the local 'players' table (one statement, no round trips).

    Args:
        db (sqlite3.Connection): The open local database connection.
        tag (str): The player's tag.
        name (str): The player's name.
        townhall (int): The player's townhall level.
        json_data (dict): The scan data to save or update.
    """
    try:
        exists = db.execute("SELECT 1 FROM players WHERE tag = ?", (tag,)).fetchone() is not None
        print(f"- #{tag} is in database, updating it now." if exists
              else f"- #{tag} is not in database, adding it now.")
        db.execute(
            """
            INSERT INTO players (tag, name, townhall, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                name       = excluded.name,
                townhall   = excluded.townhall,
                data       = excluded.data,
                updated_at = datetime('now')
            WHERE excluded.data IS NOT players.data
            """,
            (tag, name, townhall, json.dumps(json_data)),
        )
        db.commit()

    except Exception as e:
        print(f"- #{tag} Failed to save in DB: {e}")
        write2file("errors/errors.txt", f"{tag} - {json_data}")
    
# =========================
# Fetch JSON from Device
# =========================

async def visit(dev, tag:str, stream:"LogStream") -> dict:
    """
    Opens the player's profile and waits for the streamed dump. Re-opens every
    2.5s (up to ~10s) in case the first intent didn't take.
    """
    stream.arm()
    await asyncio.sleep(0.5)  # Adjust the delay as needed, sometimes the game takes a bit to load profile
    for _ in range(4):
        await dev.shell(f'am start -a android.intent.action.VIEW -d "clashofclans://OpenPlayerProfile?tag={tag}"')
        match = await stream.wait(2.5)
        if match is not None:
            return match

    return None

# ======================
# ADB Initialization
# ======================

async def launch(dev) -> None:
    """Load the hook at process start via the `wrap.com.supercell.clashofclans` LD_PRELOAD property.
    This runs before CoC forks its self-debugging watcher, so it bypasses the
    anti-tamper that blocks ptrace injection entirely so no more injector needed.
    Used for both startup and recovery."""

    await dev.shell("su -c 'setenforce 0'")
    await dev.push("lib/azurlul.so", "/data/local/tmp/azurlul.so")
    await dev.shell("su -c 'chmod 644 /data/local/tmp/azurlul.so'")  # readable by the app uid
    await dev.shell("su -c 'rm -f /data/data/com.supercell.clashofclans/files/logfile.log'")
    # Preload the hook on every launch of the package, before its anti-debug runs.
    await dev.shell("su -c 'setprop wrap.com.supercell.clashofclans LD_PRELOAD=/data/local/tmp/azurlul.so'")
    # Restart the app so it re-spawns through the wrapper.
    await dev.shell("su -c 'am force-stop com.supercell.clashofclans'")
    await dev.shell("su -c 'am start -n com.supercell.clashofclans/com.supercell.titan.GameApp'")

    # Give the game time to launch, connect and load the home village.
    print(f"[startup] {dev.serial}: launched, waiting 30s for the game to load...")
    await asyncio.sleep(30)

async def startup(client:AdbClient) -> tuple[list, dict]:
    devs = await client.devices()
    print(f"[*] Device(s) initialized: {len(devs)}")

    # Launch + hook every device concurrently.
    await asyncio.gather(*[launch(dev) for dev in devs])

    return (devs, {dev: LogStream(dev) for dev in devs})

# ====================
# Main Scanning Logic
# ====================

async def base():
    coc_client = coc.Client()  # Clash of Clans API client for fetching player data
    client = AdbClient(host='127.0.0.1', port=5037)  # ADB client for connected Android devices
    db = open_db()

    await coc_client.login("bowopo1192@idoidraw.com", "blablabla1234")
    devs, streams = await startup(client)

    # Load tags and skip anything already done in a previous run (resume).
    with open("tags.txt", 'r', encoding="utf-8") as f:
        all_tags = [line.strip().lstrip('#') for line in f if line.strip()]
    done = scanned()
    tags = [t for t in all_tags if t not in done]
    total = len(tags)
    print(f"[*] {total} tags to scan ({len(done)} already done)")

    # Isolates API failures from device failures.
    ready: asyncio.Queue = asyncio.Queue(maxsize=len(devs) * 2 if devs else 1)

    async def fetcher():
        for tag in tags:
            player = None
            for attempt in range(3):
                try:
                    player = await coc_client.get_player(tag)
                    break
                except coc.errors.NotFound:
                    write2file("scanned.txt", tag)   # invalid/banned: never retry
                    player = None
                    break
                except Exception as e:  # maintenance, rate limit, network, gateway...
                    if attempt == 2:
                        print(f"[api] #{tag} giving up: {e}")
                        write2file("errors/retries.txt", tag)
                    else:
                        await asyncio.sleep(2 ** attempt)  # backoff: 1s, 2s
            if player is not None:
                await ready.put((tag, player))
        for _ in devs:            # one sentinel per worker to signal completion
            await ready.put(None)

    async def worker(dev, idx):
        stream = streams[dev]
        misses = 0
        count = 0            # tags this device has processed
        while True:
            item = await ready.get()
            try:
                if item is None:
                    return
                tag, player = item
                count += 1
                try:
                    json_data = await visit(dev, tag, stream)
                except Exception as e:  # device/ADB error: keep going
                    print(f"[device {idx}] #{tag} device error: {e}")
                    write2file("errors/retries.txt", tag)
                    continue

                if json_data is None:
                    misses += 1
                    write2file("errors/retries.txt", tag)
                    if misses >= 15:    # device likely stalled -> try to recover
                        print(f"[device {idx}] 15 misses in a row, re-injecting...")
                        try:
                            await launch(dev)
                        except Exception as e:
                            print(f"[device {idx}] reinject failed: {e}")
                        misses = 0
                    continue

                misses = 0
                try:
                    result = parse_obs(json_data)
                    if result[0] > 0:
                        print(f"[device {idx}] {count}/{total} #{tag} --> {{'any': {result[0]}, 'stone': {result[1]}, 'xmas2012': {result[2]}, 'xmas2013': {result[3]}}}")
                        save2db(db, tag, player.name, player.town_hall, {
                            "any": result[0],
                            "stone": result[1],
                            "xmas2012": result[2],
                            "xmas2013": result[3]
                        })
                    write2file("scanned.txt", tag)
                except Exception as e:
                    print(f"[device {idx}] #{tag} save error: {e}")
                    write2file("errors/retries.txt", tag)
            finally:
                ready.task_done()

    # return_exceptions=True: a crash in one task never tears down the others.
    await asyncio.gather(asyncio.create_task(fetcher()), *[asyncio.create_task(worker(dev, i)) for i, dev in enumerate(devs, start=1)], return_exceptions=True)

    print("\n[*] Finished scanning!")
    for s in streams.values():
        s.stop()
    await coc_client.close()
    db.close()

# =================
# App Entry Point
# =================

if __name__ == "__main__":
    asyncio.run(base())