# mpv-serializd

Automatically log TV episodes to your Serializd diary when mpv reaches 90% of an episode. Each episode is logged once per account, with a watch date, and replaying it does not create another entry.

Use your normal mpv commands. mpv keeps control of folder playlists, resume markers, playback position, and settings; a small Lua script starts a Python watcher in the background.

**Windows only.** This is an unofficial integration using Serializd's private API, which can change. It is not affiliated with Serializd or mpv.

## Requirements

- Windows with [mpv](https://mpv.io/installation/) on `PATH`.
- [Python](https://www.python.org/downloads/windows/) 3.10 or newer, including `pythonw.exe` for background operation.
- A Serializd account. No third-party Python packages are required.

`Run.ps1` finds Python on `PATH` or through the Windows `py` launcher. To choose an interpreter explicitly, set `SERIALIZD_PYTHON` to its full executable path.

## Setup

Clone the repository, or download and extract its ZIP into a folder you will keep:

```powershell
git clone https://github.com/Noir01/mpv-serializd.git
cd mpv-serializd
.\Install.cmd
.\Login.cmd
```

At login, enter your Serializd **email address**, then your password in the hidden local prompt. The password is never saved. A session token is encrypted with Windows DPAPI for your Windows account. Do not put passwords or tokens in GitHub issues, configuration, or chat.

Verify the saved login without creating any entries:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 check
```

Close any already running mpv instance and launch normally:

```powershell
mpv "D:\TV\Your Show"
```

The installed Lua script points to the cloned folder. If you move the folder, rerun `Install.cmd` from its new location. The installer respects `MPV_HOME`, a `portable_config` directory beside mpv, or `%APPDATA%\mpv`. It preserves existing player configuration and resume files.

Ordinary mpv launches use preview mode until you sign in locally, then automatically log episodes on subsequent launches. Preview makes no Serializd requests or account changes. To try preview after signing in:

```powershell
mpv --script-opts=serializd-auto-mode=dry-run "D:\TV\Your Show"
```

## During playback

| Action | Result |
| --- | --- |
| Reach 90% of runtime | Create one dated diary entry and mark the episode watched |
| Finish or replay the episode | No additional entry |
| Ctrl+Shift+L | Manually log the current episode before the threshold |
| Ctrl+Shift+U | Delete this integration's confirmed entry for the current episode |

The threshold measures **playback position**, including seeking and resumed playback. It does not measure unique minutes watched. A paused episode at the threshold qualifies. Samples are taken once per second; an EOF event alone does not count as watched.

Undo only deletes a confirmed diary entry created by this integration. It leaves the watched checkbox set and keeps the episode suppressed in the local ledger. Entries created elsewhere are not deleted. A custom mpv key binding can override either shortcut.

## Episode filenames and configuration

Recognized examples:

```text
Show.Name.S01E04.1080p.mkv
Show Name (2020) - S01E04 - Episode Title.mkv
Show_Name.1x04.mkv
```

The title, optional year, season, and episode are parsed from the filename. Unmapped titles must have an exact match in Serializd's catalog; ambiguous matches are skipped. Movies, unrecognized filenames, combined episode files such as `S01E01E02`, and season-zero specials are skipped for logging.

Defaults work without a local configuration file. To customize them:

```powershell
Copy-Item .\config.example.json .\config.json
```

Edit `config.json`, which Git ignores:

| Setting | Default | Purpose |
| --- | --- | --- |
| `threshold` | `0.9` | Playback fraction required, above zero and at most one |
| `auto_mode` | `"auto"` | Preview before login, live after login; also accepts `"dry-run"`, `"live"`, or `"off"` |
| `include_specials` | `false` | Allow season-zero episodes |
| `example_folder` | `null` | Optional default folder for the explicit `play` and `scan` commands |
| `show_ids` | `{}` | Explicit title-to-TMDB mappings for ambiguous show names |

A mapping can look like `"two and a half men|2003": 2691`. Keys use lowercase titles with punctuation replaced by spaces, optionally followed by `|year`. Values are TMDB show IDs. Season and episode numbers are validated against Serializd before submission. Restart mpv after changing configuration.

## Other commands

The `.cmd` launchers handle PowerShell's execution policy for this script. If direct `Run.ps1` execution is blocked, prefix its arguments with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1`.

```powershell
.\Run.ps1 scan 'D:\TV\Your Show'                # Parse filenames offline
.\Run.ps1 scan --online 'D:\TV\Your Show'       # Validate public metadata; no diary writes
.\Run.ps1 login                               # Local email/password prompt
.\Run.ps1 login --token                       # Hidden prompt for an existing session token
.\Run.ps1 check                               # Verify authentication only
.\Run.ps1 status                              # Inspect the account's local ledger
.\Run.ps1 retry                               # Check remote state before retrying unresolved entries
.\Run.ps1 undo 'Show.Name.S01E04.mkv'           # Undo a confirmed entry created here
.\Run.ps1 uninstall                           # Remove the automatic startup script
```

`Preview.cmd` and `Play.cmd` are optional explicit launchers; pass file/folder paths or drag files onto them. Their folder scan is nonrecursive and sorted by filename. They can play skipped filenames normally. The installed startup script detects these launchers to avoid starting a second watcher.

Uninstalling retains your encrypted session and ledger. Keep the ledger to retain duplicate protection. `--no-config` or `--load-scripts=no` disables mpv's normal automatic script loading. `--script-opts=serializd-auto-mode=off` disables this integration for one launch.

## Diary behavior and recovery

Serializd distinguishes a watched checkbox from a dated diary entry. This bridge posts a diary entry to `/api/show/reviews/add`, then sets the watched checkbox with `/api/episode_log/add`. Entries have no rating, review, tags, or like and are not marked as rewatches. Dates use the local calendar date represented at UTC noon, matching the website's date helper.

Before posting, the bridge checks your remote episode diary. Its SQLite ledger prevents repeats across restarts, renamed files, and concurrent player instances. State is separated by account. Ratings without a diary entry do not count as a prior log. Signing in or scanning a folder never backfills your library.

Account state, logs, and the generated installation record live in `.local/`, which Git ignores. Do not share that folder. Background diagnostics are in `.local/sync.log`, and results appear in mpv's on-screen display.

- `pending`: no diary POST has been attempted. A live watcher retries the remote check about once a minute, preserving the original watch date.
- `sending` or `uncertain`: a submission may have reached Serializd. It is never repeated automatically. Check your diary, then use `retry`, which checks remote state before submitting again.
- `delete_uncertain`: an undo may have succeeded. Check your diary manually; the episode stays suppressed.

A failed duplicate check blocks a write. If setting the watched checkbox fails after a diary entry succeeds, the bridge reports the failure without repeating the entry. The local lock coordinates this integration's own processes; another application writing concurrently is outside that lock.

## Development

```powershell
python -m unittest discover -s tests -v
```

The tests use fake API responses and temporary state, with no account access or network requests. Windows CI tests Python 3.10, 3.12, and 3.14, including native DPAPI and file locking. See [VALIDATION.md](VALIDATION.md) for playback checks and tested limits.

Contributions are welcome. Include the Python/mpv versions and a minimal reproduction when reporting a bug. Remove usernames, paths, filenames, and other personal details from shared logs; never share session tokens or passwords.

## References and license

The implementation is original, standard-library Python plus Lua. Protocol fields were checked against [Serializd's web client](https://www.serializd.com/). Player integration follows the [mpv Lua and JSON IPC documentation](https://mpv.io/manual/master/#json-ipc). The [serializd-py documentation](https://velocidensity.github.io/serializd-py/client.html) is another reference for the unofficial API; that library is not a dependency or bundled here.

Released under the [MIT License](LICENSE).
