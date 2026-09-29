# Checker

Proof-of-concept tool that scans Clash of Clans player tags for specific rare obstacles (village decorations) and stores the results in a local SQLite database.

> Experimental. Use at your own risk and make sure your use complies with the game's and API's terms of service.

## How it works

1. Player tags are read from `tags.txt`.
2. The official Clash of Clans API (`coc.py`) validates each tag and provides the player's name and Town Hall level.
3. `main.py` pushes `lib/azurlul.so` to each connected rooted Android emulator (over ADB) and loads it into the game with the `wrap.com.supercell.clashofclans` `LD_PRELOAD` property, then restarts the game.
4. For each tag, the game is sent an `OpenPlayerProfile` intent. The script polls the log file, extracts the `"obstacles"` array and counts the watched obstacle IDs:

   | Obstacle ID | Name in output |
   | ----------- | -------------- |
   | `8000015`   | `stone`        |
   | `8000021`   | `xmas2012`     |
   | `8000028`   | `xmas2013`     |

5. Players with at least one match are upserted into `database/players.db`.

Multiple emulators are supported and run in parallel. A single fetcher task feeds tags to one worker per device. Runs are resumable, and a device that misses 15 dumps in a row is relaunched automatically.

## Requirements

- Python 3.10+
- A rooted Android **x86_64** emulator with `su` available to the ADB shell without a signed whitelist (Genymotion, LDPlayer and MEmu are known to work; BlueStacks is not supported)
- Clash of Clans installed and logged in on the emulator
- ADB server running with the emulator(s) visible in `adb devices`
- Clash of Clans Developer API account: <https://developer.clashofclans.com/>

## Setup

```bash
pip install -r requirements.txt
```

### API credentials

`base()` in `main.py` logs in with `coc_client.login(email, password)`. Replace the values there with your own developer-portal credentials.

```python
await coc_client.login("xxx@email.com", "xxx")
```

### Player tags

Put one tag per line in `tags.txt`. The leading `#` is optional.

```text
#9ABC123
XYZ9876
```

### Hook library

A prebuilt `lib/azurlul.so` (x86_64) is expected at that path. Get it at: <https://github.com/azurlul/hook>

## Usage

1. Start the emulator(s) and confirm they show up in `adb devices`.
2. Fill in `tags.txt` and your API credentials.
3. Run:

```bash
python main.py
```

On start, the script hooks and launches the game on each device and waits 30 seconds for it to load. Then it scans.

## Demonstration

[![Watch the demo](https://img.youtube.com/vi/g5ccXKAaUXM/maxresdefault.jpg)](https://www.youtube.com/watch?v=g5ccXKAaUXM)

## Output files

| Path                  | Contents                                                                                      |
| --------------------- | --------------------------------------------------------------------------------------------- |
| `database/players.db` | SQLite table `players` (`tag`, `name`, `townhall`, `data` JSON with the counts, `updated_at`) |
| `scanned.txt`         | Tags already processed or invalid. Skipped on the next run. Delete it to rescan everything.   |
| `errors/retries.txt`  | Tags that failed (API, device or timeout). Copy them into `tags.txt` to retry.                |
| `errors/errors.txt`   | Records that failed to save to the database.                                                  |

Open the database with DB Browser for SQLite, SQLiteStudio, or <https://inloop.github.io/sqlite-viewer/>.

## Project structure

```text
main.py            Entry point: ADB, log streaming, API fetching, DB
tags.txt           Input tags
requirements.txt   Python dependencies
lib/azurlul.so     Prebuilt hook loaded into the game
database/          SQLite database (created at runtime)
errors/            Retry/error logs (created at runtime)
```

## Troubleshooting

| Problem                            | Check                                                                                                            |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError`              | Run `pip install -r requirements.txt` with the same Python you use to run `main.py`.                             |
| `0 device(s)` / nothing happens    | Start the emulator, enable ADB, and run `adb devices`.                                                           |
| Every tag ends up in `retries.txt` | Root (`su`) must work from the ADB shell. Verify the hook loaded and that the ABI matches the emulator (x86_64). |
| Game never opens the profile       | Increase the load delay in `launch()` or the pause in `visit()`.                                                 |
| API login or requests fail         | Verify credentials. The API may be down for maintenance or rate-limiting. The script retries 3 times.            |
| Database empty                     | Only players with at least one watched obstacle are saved. Check the console output.                            |

## Disclaimer

Provided as-is for educational and research purposes. You are responsible for complying with all applicable laws, terms of service, and third-party policies. The author accepts no liability for account actions, data loss, or other consequences of use.
