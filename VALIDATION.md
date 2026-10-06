# Validation

## Local playback checks

The initial Windows implementation was tested with mpv 0.41.0 and Python 3.12 on October 4, 2026:

| Check | Result |
| --- | --- |
| Playback below 90% | No preview log |
| Playback from 88% through EOF | Exactly one preview log |
| Manual mark and undo shortcuts | Exactly one preview event each; clean shutdown |
| Automatic startup | Background watcher connected during ordinary folder playback |
| Automatic watcher at 95% while paused | Exactly one preview log; pause preserved |
| Folder resume | Same episode resumed at a saved position of 123 seconds |
| Existing resume files | Unchanged after isolated tests |
| Installation/removal | Existing configuration and marker files preserved |
| Public catalog lookup | Show, season, and episode metadata resolved |
| Real authentication | Login and saved-session identity check succeeded |

The user subsequently reported the installed integration working. The playback checks used preview mode and isolated resume files. They did not create test diary entries. Deletion of a real diary entry has not been separately verified.

## Automated coverage

All 44 tests passed on the clean public copy before publication. The suite covers filename formats, multi-episode rejection, threshold boundaries, invalid playback samples, manual marking, specials, local persistence, duplicate checks, separate accounts, concurrency, exact title/year matching, episode validation, original watch dates on retries, uncertain submissions, watched-checkbox failures, undo, Windows session encryption, installation, automatic mode selection, local configuration overrides, and login input validation.

Automated tests make no network requests and need no credentials or media files. GitHub Actions runs the suite on Windows with Python 3.10, 3.12, and 3.14. They do not certify ongoing compatibility with Serializd's private API.

## Known limits

- Windows is the supported runtime; other operating systems need different IPC, locking, and credential storage.
- The completion threshold measures playback position, including seeking, rather than viewing coverage.
- Episode identification depends on filenames and exact catalog matches or explicit mappings.
- Combined episode files are skipped; specials require opt-in.
- An ambiguous submission never retries automatically, preventing a second diary entry after an unknown result.
- The private Serializd API may change independently of this project.
