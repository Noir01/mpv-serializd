"""Personal Windows mpv -> Serializd diary bridge. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import getpass
import json
import logging
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
API_BASE = "https://serializddesktop.onrender.com"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts"}
AUTO_HEADER = "-- Managed by mpv-serializd\n"
LOG = logging.getLogger("serializd-mpv")


@dataclass(frozen=True)
class Episode:
    show: str
    year: int | None
    season: int
    number: int

    @property
    def label(self):
        return f"{self.show} S{self.season:02}E{self.number:02}"

    @property
    def show_key(self):
        return normalize(self.show) + (f"|{self.year}" if self.year else "")


def normalize(value):
    return re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).strip()


def parse_episode(path: str) -> Episode | None:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    match = re.search(r"(?i)(?<![a-z0-9])(?:s(\d{1,2})e(\d{1,3})|(\d{1,2})x(\d{1,3}))(?!\d)", name)
    if not match:
        return None
    tail = name[match.end():]
    # A combined file needs timing information for each episode; don't guess.
    if re.match(r"(?i)^[ ._-]*(?:e\d|s\d+e\d|\d{1,3}(?:\D|$))", tail):
        return None
    prefix = name[:match.start()].strip(" ._-[]")
    year_match = re.search(r"(?:\((\d{4})\)|[ ._](\d{4}))$", prefix)
    year = None
    if year_match:
        year = int(year_match.group(1) or year_match.group(2))
        prefix = prefix[:year_match.start()].strip(" ._-")
    show = re.sub(r"[._]+", " ", prefix).strip(" -")
    season, number = (int(match.group(1)), int(match.group(2))) if match.group(1) else (int(match.group(3)), int(match.group(4)))
    if not show or number < 1:
        return None
    return Episode(show, year, season, number)


def diary_payload(show_id, season_id, episode_number, watched_date):
    # Serializd's own date helper uses the selected LOCAL calendar date at UTC noon.
    date = dt.date.fromisoformat(watched_date)
    return {"show_id": show_id, "season_id": season_id, "episode_number": episode_number,
            "review_text": "", "rating": 0, "contains_spoiler": False,
            "backdate": f"{date.isoformat()}T12:00:00Z", "is_log": True,
            "is_rewatch": False, "tags": [], "allows_comments": True, "like": False}


class ApiError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Serializd:
    def __init__(self, token=None):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, payload=None, params=None, expect_object=True):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Origin": "https://www.serializd.com", "Referer": "https://www.serializd.com/",
                   "X-Requested-With": "serializd_vercel", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read()
                result = json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ApiError("Serializd rejected authentication; run Login.cmd again.") from None
            raise ApiError(f"Serializd returned HTTP {exc.code}.") from None
        except (OSError, ValueError) as exc:
            # Do not include server bodies, passwords, or tokens in diagnostics.
            raise ApiError(f"Serializd request failed ({type(exc).__name__}).") from None
        if (result is False or (isinstance(result, dict) and
                (result.get("success") is False or result.get("error"))) or
                (expect_object and not isinstance(result, dict))):
            raise ApiError("Serializd returned an unexpected or unsuccessful response.")
        return result

    def login(self, email, password):
        response = self.request("/api/login", {"email": email, "password": password})
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise ApiError("Login did not return an access token.")
        self.token = token
        return self.identity()

    def identity(self):
        result = self.request("/api/user_information")
        username = result.get("username")
        if not isinstance(username, str) or not username:
            raise ApiError("Serializd did not return an account username.")
        return {"username": username, "token": self.token}

    def existing_log(self, username, show_id, season_id, number):
        result = self.request(f"/api/user/{urllib.parse.quote(username, safe='')}/reviews/show/{show_id}/v2",
                              params={"season_id": season_id, "episode_number": number,
                                      "include_episode_reviews": "true"})
        reviews = result.get("userReviews")
        if not isinstance(reviews, list):
            raise ApiError("Cannot verify existing diary entries: response format changed.")
        for review in reviews:
            if (not isinstance(review, dict) or not {"isLog", "seasonId", "episodeNumber"}.issubset(review)
                    or not isinstance(review["isLog"], bool)):
                raise ApiError("Cannot verify an unexpected diary entry format.")
            if (review.get("isLog") is True and review.get("seasonId") == season_id
                    and review.get("episodeNumber") == number):
                return review
        return None

    def add_diary(self, payload):
        result = self.request("/api/show/reviews/add", payload)
        review_id = result.get("id")
        if not isinstance(review_id, int) or isinstance(review_id, bool) or review_id <= 0:
            raise ApiError("Diary request returned no entry ID; verify your diary before retrying.")
        return review_id

    def mark_watched(self, show_id, season_id, number):
        self.request("/api/episode_log/add", {"show_id": show_id, "season_id": season_id,
                     "episode_numbers": [number], "should_get_next_episode": False}, expect_object=False)

    def delete_diary(self, review_id):
        self.request("/api/show/reviews/delete", {"review_id": review_id}, expect_object=False)


def config():
    result = {"threshold": 0.9, "auto_mode": "auto", "include_specials": False,
              "example_folder": None, "show_ids": {}}
    for filename in ("config.example.json", "config.json"):
        path = ROOT / filename
        if path.exists():
            values = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(values, dict):
                raise ValueError(f"{filename} must contain a JSON object.")
            result.update(values)
    threshold = result.get("threshold", 0.9)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("config.json threshold must be a fraction above 0 and at most 1.")
    return result


class Catalog:
    def __init__(self, api, settings):
        self.api = api
        self.shows = dict(settings.get("show_ids", {}))
        self.seasons = {}

    def resolve(self, episode):
        show_id = self.shows.get(episode.show_key)
        if show_id is None:
            results = self.api.request("/api/search/shows", params={"search_query": episode.show}).get("results")
            if not isinstance(results, list):
                raise ApiError("Show search response format changed.")
            candidates = [r for r in results if normalize(r.get("name", "")) == normalize(episode.show)
                          and (not episode.year or str(r.get("firstAirDate", "")).startswith(str(episode.year)))]
            if len(candidates) != 1:
                raise ApiError(f"No unique exact match for {episode.show!r}; add its TMDB ID to config.json show_ids.")
            show_id = candidates[0].get("id")
            self.shows[episode.show_key] = show_id
        if not isinstance(show_id, int) or isinstance(show_id, bool) or show_id <= 0:
            raise ApiError("Invalid TMDB show ID in mapping.")
        key = (show_id, episode.season)
        if key not in self.seasons:
            self.seasons[key] = self.api.request(f"/api/show/{show_id}/season/{episode.season}")
        season = self.seasons[key]
        season_id = season.get("seasonId")
        if not isinstance(season_id, int) or not any(e.get("episodeNumber") == episode.number for e in season.get("episodes", [])):
            raise ApiError(f"Episode not found in Serializd: {episode.label}.")
        return show_id, season_id


def dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Encrypted session storage requires Windows.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "mpv-serializd", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save_session(session):
    LOCAL.mkdir(exist_ok=True)
    path = LOCAL / "session.dat"
    temp = LOCAL / "session.tmp"
    temp.write_bytes(dpapi(json.dumps(session).encode("utf-8")))
    os.replace(temp, path)


def load_session():
    path = LOCAL / "session.dat"
    if not path.exists():
        raise ValueError("Sign in locally with Login.cmd first. Do not send your password in chat.")
    return json.loads(dpapi(path.read_bytes(), decrypt=True))


def mpv_config_directory(mpv_executable):
    if os.environ.get("MPV_HOME"):
        return Path(os.environ["MPV_HOME"]).expanduser().resolve()
    portable = Path(mpv_executable).resolve().parent / "portable_config"
    if portable.is_dir():
        return portable
    return Path(os.environ["APPDATA"]) / "mpv"


def render_autostart(python_executable):
    pythonw = Path(python_executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise ValueError("Automatic background sync requires pythonw.exe beside your Python interpreter.")
    source = (ROOT / "autostart.lua.in").read_text(encoding="utf-8")
    return (AUTO_HEADER + f"-- Project: {ROOT.as_posix()}\n" + source
            .replace("__PROJECT_LITERAL__", json.dumps(ROOT.as_posix(), ensure_ascii=False))
            .replace("__PYTHON_LITERAL__", json.dumps(pythonw.as_posix(), ensure_ascii=False)))


def install_autostart():
    mpv = shutil.which("mpv.exe") or shutil.which("mpv")
    if not mpv:
        raise ValueError("mpv not found.")
    target = mpv_config_directory(mpv) / "scripts" / "serializd-autostart.lua"
    source = render_autostart(sys.executable)
    if target.exists():
        previous = target.read_text(encoding="utf-8-sig")
        if not previous.startswith(AUTO_HEADER + f"-- Project: {ROOT.as_posix()}\n"):
            raise ValueError(f"A different script already exists at {target}; it was left untouched.")
        if previous != source:
            backup = target.with_name(target.name + ".backup-" + uuid.uuid4().hex[:8])
            shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    LOCAL.mkdir(exist_ok=True)
    (LOCAL / "installation.json").write_text(json.dumps({"script": str(target)}, indent=2), encoding="utf-8")
    return target


def uninstall_autostart():
    record = LOCAL / "installation.json"
    if not record.exists():
        raise ValueError("No automatic-start installation record found.")
    target = Path(json.loads(record.read_text(encoding="utf-8"))["script"])
    if target.exists():
        expected = AUTO_HEADER + f"-- Project: {ROOT.as_posix()}\n"
        if target.name != "serializd-autostart.lua" or not target.read_text(encoding="utf-8-sig").startswith(expected):
            raise ValueError("Installed script identity changed; removal was refused.")
        target.unlink()
    record.unlink()
    return target


def automatic_live(settings):
    mode = settings.get("auto_mode", "auto")
    if mode == "dry-run":
        return False
    if mode not in {"auto", "live"}:
        raise ValueError("auto_mode must be auto, dry-run, live, or off.")
    if mode == "live":
        load_session()
        return True
    if not (LOCAL / "session.dat").exists():
        return False
    load_session()
    return True


class State:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS episodes (
            account TEXT, show_id INTEGER, season INTEGER, episode INTEGER, season_id INTEGER,
            label TEXT, filename TEXT, watched_date TEXT, status TEXT, review_id INTEGER,
            error TEXT, updated TEXT, PRIMARY KEY(account,show_id,season,episode))""")
        self.db.commit()

    def close(self):
        self.db.close()

    def enqueue(self, account, show_id, season_id, episode, filename, watched_date):
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?)""",
                            (account, show_id, episode.season, episode.number, season_id, episode.label,
                             filename, watched_date, "pending", dt.datetime.now().isoformat()))
        return self.get(account, show_id, episode.season, episode.number)

    def get(self, account, show_id, season, number):
        return self.db.execute("SELECT * FROM episodes WHERE account=? AND show_id=? AND season=? AND episode=?",
                               (account, show_id, season, number)).fetchone()

    def update(self, row, status, review_id=None, error=None):
        with self.db:
            self.db.execute("""UPDATE episodes SET status=?, review_id=COALESCE(?,review_id),error=?,updated=?
                WHERE account=? AND show_id=? AND season=? AND episode=?""",
                            (status, review_id, error, dt.datetime.now().isoformat(), row["account"],
                             row["show_id"], row["season"], row["episode"]))

    def rows(self, account):
        return self.db.execute("SELECT * FROM episodes WHERE account=? ORDER BY updated DESC", (account,)).fetchall()


class ApiLock:
    """Serialize account writes across mpv windows and manual commands."""
    def __enter__(self):
        import msvcrt
        LOCAL.mkdir(exist_ok=True)
        self.file = open(LOCAL / "api.lock", "a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        deadline = time.monotonic() + 60
        while True:
            try:
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise ApiError("Another Serializd operation is busy; try again shortly.")
                time.sleep(0.1)

    def __exit__(self, *args):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def sync_row(state, api, row, explicit_retry=False):
    # The caller holds ApiLock. A crash/timeout after a POST is never retried automatically.
    row = state.get(row["account"], row["show_id"], row["season"], row["episode"])
    if row["status"] in {"logged", "existing", "deleted"}:
        return f"Already handled: {row['label']} ({row['status']})."
    if row["status"] != "pending" and not explicit_retry:
        return f"Needs diary verification: {row['label']} ({row['status']}); run status, then retry."
    try:
        existing = api.existing_log(row["account"], row["show_id"], row["season_id"], row["episode"])
    except ApiError as exc:
        state.update(row, row["status"], error=str(exc))
        return f"Waiting to verify {row['label']}: {exc}"
    if existing:
        state.update(row, "existing", existing.get("id"))
        return f"Already in your diary: {row['label']}."
    state.update(row, "sending")
    try:
        review_id = api.add_diary(diary_payload(row["show_id"], row["season_id"], row["episode"], row["watched_date"]))
    except ApiError as exc:
        state.update(row, "uncertain", error=str(exc))
        return f"Check your diary for {row['label']}; submission result is uncertain. {exc}"
    state.update(row, "logged", review_id)
    try:
        api.mark_watched(row["show_id"], row["season_id"], row["episode"])
    except ApiError as exc:
        state.update(row, "logged", error=f"Diary saved; watched checkbox failed: {exc}")
        return f"Diary saved: {row['label']}; watched checkbox needs attention."
    return f"Logged: {row['label']} on {row['watched_date']}."


class Gate:
    def __init__(self, settings):
        self.threshold = settings["threshold"]
        self.specials = settings.get("include_specials", False)
        self.path = None
        self.attempted = False

    def sample(self, sample, manual=False):
        path = sample.get("path")
        if path != self.path or sample.get("new_file"):
            self.path, self.attempted = path, False
        if not path or (self.attempted and not manual):
            return None
        episode = parse_episode(path)
        if not episode or (episode.season == 0 and not self.specials):
            return None
        if not manual:
            pos, duration = sample.get("position"), sample.get("duration")
            if (not isinstance(pos, (int, float)) or not isinstance(duration, (int, float))
                    or not math.isfinite(pos) or not math.isfinite(duration) or duration <= 0
                    or pos < 0 or pos / duration < self.threshold):
                return None
        self.attempted = True
        return episode


class Pipe:
    def __init__(self, name):
        import msvcrt
        self.file = open(name, "r+b", buffering=0)
        self.handle = msvcrt.get_osfhandle(self.file.fileno())
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        self.kernel.PeekNamedPipe.restype = ctypes.c_int
        self.buffer = bytearray()
        self.write_lock = threading.Lock()

    def close(self):
        self.file.close()

    def send(self, command):
        data = (json.dumps({"command": command}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.write_lock:
            self.file.write(data)

    def receive(self, timeout=1):
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self.buffer:
                line, _, remaining = self.buffer.partition(b"\n")
                self.buffer = bytearray(remaining)
                try:
                    return json.loads(line)
                except (ValueError, UnicodeError):
                    continue
            count = ctypes.c_ulong()
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(count), None):
                raise EOFError("mpv disconnected")
            if count.value:
                data = self.file.read(min(count.value, 65536))
                if not data:
                    raise EOFError("mpv disconnected")
                self.buffer.extend(data)
            elif time.monotonic() >= deadline:
                return None
            else:
                time.sleep(0.02)


class Worker:
    def __init__(self, settings, live, notify):
        self.settings, self.live, self.notify = settings, live, notify
        self.session = load_session() if live else None
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self.run, name="serializd-sync", daemon=False)
        self.thread.start()

    def run(self):
        state = None
        try:
            if self.live:
                api = Serializd(self.session["token"])
                catalog = Catalog(api, self.settings)
                state = State(LOCAL / "state.sqlite3")
            last_retry = 0
            while True:
                try:
                    job = self.jobs.get(timeout=1)
                except queue.Empty:
                    if self.live and time.monotonic() - last_retry > 60:
                        last_retry = time.monotonic()
                        try:
                            with ApiLock():
                                for row in state.rows(self.session["username"]):
                                    if row["status"] == "pending":
                                        self.notify(sync_row(state, api, row))
                                        time.sleep(0.5)
                        except (ApiError, OSError, sqlite3.Error) as exc:
                            self.notify(f"Pending entries will wait: {exc}")
                    continue
                if job is None:
                    return
                kind, episode, filename, watched_date = job
                try:
                    if not self.live:
                        self.notify(f"Preview: would {'undo' if kind == 'undo' else 'log'} {episode.label} on {watched_date}.")
                    else:
                        show_id, season_id = catalog.resolve(episode)
                        with ApiLock():
                            if kind == "undo":
                                row = state.get(self.session["username"], show_id, episode.season, episode.number)
                                self.notify(undo_row(state, api, row))
                            else:
                                row = state.enqueue(self.session["username"], show_id, season_id, episode, filename, watched_date)
                                self.notify(sync_row(state, api, row))
                        time.sleep(0.5)
                except (ApiError, OSError, ValueError, sqlite3.Error) as exc:
                    self.notify(f"Could not handle {episode.label}: {exc}")
        except Exception:
            LOG.exception("Sync worker stopped")
        finally:
            if state:
                state.close()

    def stop(self):
        self.jobs.put(None)
        self.thread.join()


def undo_row(state, api, row):
    if not row or row["status"] != "logged" or not row["review_id"]:
        return "Undo skipped: no confirmed diary entry created by this integration."
    state.update(row, "deleting")
    try:
        api.delete_diary(row["review_id"])
    except ApiError as exc:
        state.update(row, "delete_uncertain", error=str(exc))
        return "Undo result uncertain; check your diary. Automatic logging remains blocked for this episode."
    state.update(row, "deleted")
    return f"Removed diary entry: {row['label']}. Watched checkbox remains set."


def watch(pipe_name, settings, live, process=None, resume=False):
    if os.name != "nt":
        raise ValueError("This version targets Windows named pipes.")
    # Authenticate before starting a watcher or issuing playback commands.
    if live:
        load_session()
    deadline = time.monotonic() + 15
    while True:
        try:
            pipe = Pipe(pipe_name)
            break
        except OSError:
            if (process and process.poll() is not None) or time.monotonic() >= deadline:
                raise ValueError("Cannot connect to mpv. Start with Play.cmd/Preview.cmd, or load bridge.lua and enable IPC.")
            time.sleep(0.1)
    notices = queue.Queue()
    worker = Worker(settings, live, notices.put)
    gate = Gate(settings)
    LOG.info("%s connected. Ctrl+Shift+L = mark, Ctrl+Shift+U = undo, Ctrl+C = stop watcher.", "Live" if live else "Preview")
    try:
        pipe.send(["script-message", "serializd-ready", "live" if live else "preview"])
        if resume:
            pipe.send(["set_property", "pause", False])
        while True:
            while not notices.empty():
                message = notices.get_nowait()
                LOG.info(message)
                pipe.send(["show-text", message, 5000])
            event = pipe.receive()
            if event is None:
                if not worker.thread.is_alive():
                    raise ValueError("Sync worker stopped. See console diagnostics.")
                continue
            if event.get("event") == "shutdown":
                break
            if event.get("event") != "client-message":
                continue
            args = event.get("args", [])
            if len(args) != 2 or args[0] not in {"serializd-sample", "serializd-mark", "serializd-undo"}:
                continue
            try:
                sample = json.loads(args[1])
                if not isinstance(sample, dict):
                    continue
            except (ValueError, TypeError):
                continue
            kind = args[0]
            if kind == "serializd-undo":
                episode = parse_episode(sample.get("path", ""))
                gate.attempted = True
            else:
                episode = gate.sample(sample, manual=kind == "serializd-mark")
            if episode:
                worker.jobs.put(("undo" if kind == "serializd-undo" else "log", episode,
                                 sample.get("path", ""), dt.datetime.now().astimezone().date().isoformat()))
    except (EOFError, BrokenPipeError):
        pass
    except KeyboardInterrupt:
        LOG.info("Watcher stopped; finishing queued requests.")
    finally:
        pipe.close()
        worker.stop()
        while not notices.empty():
            LOG.info(notices.get_nowait())


def media_files(paths, settings):
    if not paths:
        folder = settings.get("example_folder")
        if not folder:
            raise ValueError("Pass a video file/folder, or set example_folder in your local config.json.")
        paths = [folder]
    files = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            files.extend(sorted((f for f in path.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS),
                                key=lambda p: p.name.casefold()))
        elif path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            files.append(path)
        else:
            raise ValueError(f"Not a video file or existing folder: {path}")
    if not files:
        raise ValueError("No videos found.")
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="Sign in locally; only an encrypted token is saved")
    login.add_argument("--token", action="store_true", help="Prompt for an existing token instead of password")
    scan = sub.add_parser("scan", help="Parse filenames without logging anything")
    scan.add_argument("paths", nargs="*")
    scan.add_argument("--online", action="store_true", help="Also validate public show and episode metadata")
    play = sub.add_parser("play", help="Start mpv with the bridge and watch the playlist")
    play.add_argument("paths", nargs="*")
    mode = play.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Write entries to your Serializd diary")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    play.add_argument("--mpv", help="Full path to mpv.exe")
    play.add_argument("--mpv-option", action="append", default=[], help="Extra option; use --mpv-option=--speed=2 syntax")
    attach = sub.add_parser("watch", help="Attach to mpv with bridge.lua loaded")
    attach.add_argument("--pipe", default=r"\\.\pipe\mpv-serializd")
    mode = attach.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--auto", action="store_true", help="Use live mode after local login, preview until then")
    attach.add_argument("--background", action="store_true", help="Write diagnostics to .local/sync.log")
    sub.add_parser("install", help="Add automatic sync to your normal mpv launches")
    sub.add_parser("uninstall", help="Remove only this integration's mpv startup script")
    sub.add_parser("status", help="Show local diary ledger without account requests")
    sub.add_parser("check", help="Verify saved account authentication; no diary writes")
    sub.add_parser("retry", help="Verify then retry pending/uncertain submissions")
    undo = sub.add_parser("undo", help="Delete only a confirmed entry created by this bridge")
    undo.add_argument("filename")
    args = parser.parse_args(argv)
    settings = config()
    if args.command == "login":
        api = Serializd()
        if args.token:
            api.token = getpass.getpass("Serializd access token (hidden): ").strip()
            session = api.identity()
        else:
            email = input("Serializd email (not username): ").strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                raise ValueError("Enter the email address associated with your Serializd account.")
            password = getpass.getpass("Serializd password (hidden; never saved): ")
            session = api.login(email, password)
            del password
        save_session(session)
        print(f"Signed in as {session['username']}. Token encrypted for this Windows user. No episodes logged.")
    elif args.command == "scan":
        catalog = Catalog(Serializd(), settings) if args.online else None
        found = skipped = 0
        for path in media_files(args.paths, settings):
            ep = parse_episode(str(path))
            if ep:
                ids = catalog.resolve(ep) if catalog else (settings.get("show_ids", {}).get(ep.show_key), None)
                print(f"{ep.label} | year={ep.year} | TMDB={ids[0]}" + (f" | season ID={ids[1]}" if catalog else ""))
                found += 1
            else:
                print(f"Skipped: {path.name}")
                skipped += 1
        print(f"Recognized {found}; skipped {skipped}. No episodes logged.")
    elif args.command == "play":
        if args.live:
            load_session()
        files = media_files(args.paths, settings)
        mpv = args.mpv or shutil.which("mpv.exe") or shutil.which("mpv")
        if not mpv:
            raise ValueError("mpv not found; pass --mpv with its full executable path.")
        name = r"\\.\pipe\mpv-serializd-" + uuid.uuid4().hex
        options = args.mpv_option
        if any(o.startswith(("--input-ipc-", "--script=", "--scripts=", "--pause")) for o in options):
            raise ValueError("Extra options cannot replace the bridge IPC, script, or startup pause.")
        command = [mpv, *options, f"--input-ipc-server={name}", f"--script={ROOT / 'bridge.lua'}", "--pause=yes", "--", *map(str, files)]
        # Hide the helper console; mpv's own player window is still available.
        process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            watch(name, settings, args.live, process=process, resume=True)
        except Exception:
            if process.poll() is None:
                process.terminate()
            raise
    elif args.command == "watch":
        watch(args.pipe, settings, automatic_live(settings) if args.auto else args.live)
    elif args.command == "install":
        print(f"Installed: {install_autostart()}\nUse your usual mpv command. Preview until local login; live afterward.")
    elif args.command == "uninstall":
        print(f"Removed: {uninstall_autostart()}\nResume files, mpv settings, and local Serializd state were retained.")
    elif args.command in {"status", "check", "retry", "undo"}:
        session = load_session()
        api = Serializd(session["token"])
        if args.command == "check":
            identity = api.identity()
            if identity["username"] != session["username"]:
                raise ValueError("Account identity changed; run Login.cmd again.")
            print(f"Signed in as {session['username']}. Saved authentication works. No episodes logged.")
            return 0
        state = State(LOCAL / "state.sqlite3")
        try:
            if args.command == "status":
                print(f"Account: {session['username']}")
                rows = state.rows(session["username"])
                for row in rows:
                    print(f"{row['status']:16} {row['watched_date']} {row['label']} | entry={row['review_id']}" + (f" | {row['error']}" if row['error'] else ""))
                if not rows:
                    print("No local entries yet.")
            else:
                with ApiLock():
                    if args.command == "retry":
                        for row in state.rows(session["username"]):
                            if row["status"] in {"pending", "sending", "uncertain"}:
                                print(sync_row(state, api, row, explicit_retry=True))
                                time.sleep(0.5)
                    else:
                        episode = parse_episode(args.filename)
                        if not episode:
                            raise ValueError("Cannot recognize an episode from that filename.")
                        show_id, _ = Catalog(api, settings).resolve(episode)
                        print(undo_row(state, api, state.get(session["username"], show_id, episode.season, episode.number)))
        finally:
            state.close()
    return 0


if __name__ == "__main__":
    log_options = {}
    if "--background" in sys.argv:
        LOCAL.mkdir(exist_ok=True)
        log_options = {"filename": LOCAL / "sync.log", "encoding": "utf-8"}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", **log_options)
    try:
        raise SystemExit(main())
    except (ApiError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)
