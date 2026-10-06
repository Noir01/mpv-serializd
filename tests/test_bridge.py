import io
import json
import math
import sys
import shutil
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serializd_mpv as bridge

NAME = "Two and a Half Men (2003) - S01E04 - If I Cant Write My Chocolate Song.mkv"


class FakeApi:
    def __init__(self):
        self.existing = None
        self.posts = []
        self.watched = []
        self.deleted = []
        self.post_failure = False
        self.preflight_failure = False
        self.checkbox_failure = False
        self.delete_failure = False

    def existing_log(self, *args):
        if self.preflight_failure:
            raise bridge.ApiError("Offline")
        return self.existing

    def add_diary(self, payload):
        self.posts.append(payload)
        if self.post_failure:
            raise bridge.ApiError("Timeout after send")
        return 101

    def mark_watched(self, *args):
        if self.checkbox_failure:
            raise bridge.ApiError("Offline")
        self.watched.append(args)

    def delete_diary(self, review_id):
        self.deleted.append(review_id)
        if self.delete_failure:
            raise bridge.ApiError("Timeout")


class FilenameTests(unittest.TestCase):
    def test_actual_format(self):
        ep = bridge.parse_episode("D:\\TV\\Two and a Half Men\\" + NAME)
        self.assertEqual(ep, bridge.Episode("Two and a Half Men", 2003, 1, 4))

    def test_release_formats(self):
        for name in ["Two.and.a.Half.Men.S01E04.1080p.mkv", "Two_and_a_Half_Men.1x04.mkv"]:
            with self.subTest(name=name):
                ep = bridge.parse_episode(name)
                self.assertEqual((ep.show, ep.season, ep.number), ("Two and a Half Men", 1, 4))

    def test_unsafe_and_non_episodes(self):
        for name in ["Show.S01E01E02.mkv", "Show.S01E01-E02.mkv", "Show.S01E01-02.mkv",
                     "Show.1x01-02.mkv", "Movie (2003).mkv", "S01E01.mkv", "Show.S01E00.mkv",
                     "Show.S01E1234.mkv", "Show.S01E01-S01E02.mkv"]:
            with self.subTest(name=name):
                self.assertIsNone(bridge.parse_episode(name))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path.cwd() / "work" / "test-tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.folder = self.temp_root / uuid.uuid4().hex
        self.folder.mkdir()
        self.root_patch = patch.object(bridge, "ROOT", self.folder)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        assert self.folder.resolve().is_relative_to(self.temp_root.resolve())
        shutil.rmtree(self.folder)

    def test_clean_checkout_defaults_need_no_private_config(self):
        settings = bridge.config()
        self.assertEqual(settings["threshold"], 0.9)
        self.assertEqual(settings["auto_mode"], "auto")
        self.assertIsNone(settings["example_folder"])
        self.assertEqual(settings["show_ids"], {})

    def test_private_config_overrides_example_without_losing_other_defaults(self):
        (self.folder / "config.example.json").write_text(json.dumps({"threshold": 0.8}), encoding="utf-8")
        (self.folder / "config.json").write_text(json.dumps({"threshold": 0.95, "auto_mode": "dry-run"}), encoding="utf-8")
        settings = bridge.config()
        self.assertEqual(settings["threshold"], 0.95)
        self.assertEqual(settings["auto_mode"], "dry-run")
        self.assertIs(settings["include_specials"], False)

    def test_invalid_thresholds_fail_before_starting_watcher(self):
        for value in (0, -0.1, 1.01, True, "90%"):
            with self.subTest(value=value):
                (self.folder / "config.json").write_text(json.dumps({"threshold": value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    bridge.config()

    def test_non_object_configuration_is_rejected(self):
        (self.folder / "config.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            bridge.config()

    def test_explicit_launcher_without_folder_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "Pass a video file/folder"):
            bridge.media_files([], bridge.config())

    def test_username_input_is_rejected_before_password_prompt_or_network(self):
        with patch("builtins.input", return_value="test-user"), \
                patch.object(bridge.getpass, "getpass") as password, \
                patch.object(bridge.Serializd, "login") as login:
            with self.assertRaisesRegex(ValueError, "email address"):
                bridge.main(["login"])
            password.assert_not_called()
            login.assert_not_called()

    def test_valid_email_uses_hidden_prompt_and_saves_session(self):
        fake_session = {"username": "test-user", "token": "dummy-test-value"}
        with patch("builtins.input", return_value="test+serializd@example.com"), \
                patch.object(bridge.getpass, "getpass", return_value="dummy-test-password"), \
                patch.object(bridge.Serializd, "login", return_value=fake_session) as login, \
                patch.object(bridge, "save_session") as save, patch("builtins.print"):
            bridge.main(["login"])
            login.assert_called_once_with("test+serializd@example.com", "dummy-test-password")
            save.assert_called_once_with(fake_session)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.gate = bridge.Gate({"threshold": 0.9})

    def sample(self, position, duration=100, **extra):
        return {"path": NAME, "position": position, "duration": duration, **extra}

    def test_boundary_and_eof_duplicate(self):
        self.assertIsNone(self.gate.sample(self.sample(89.999)))
        self.assertIsNotNone(self.gate.sample(self.sample(90)))
        self.assertIsNone(self.gate.sample(self.sample(100)))
        self.assertIsNone(self.gate.sample(self.sample(100)))

    def test_eof_below_threshold(self):
        self.assertIsNone(self.gate.sample(self.sample(70)))
        self.assertIsNone(self.gate.sample(self.sample(70)))

    def test_invalid_duration_and_position(self):
        for position, duration in [(None, 100), (90, None), (0, 0), (-1, 100), (math.nan, 100), (90, math.inf)]:
            with self.subTest(position=position, duration=duration):
                self.assertIsNone(self.gate.sample(self.sample(position, duration)))

    def test_manual_mark(self):
        self.assertIsNotNone(self.gate.sample(self.sample(1), manual=True))
        self.assertIsNone(self.gate.sample(self.sample(91)))

    def test_new_file_resets_gate(self):
        self.assertIsNotNone(self.gate.sample(self.sample(91)))
        other = self.sample(91)
        other["path"] = NAME.replace("S01E04", "S01E05")
        self.assertIsNotNone(self.gate.sample(other))

    def test_specials_opt_in(self):
        special = self.sample(91)
        special["path"] = "Show.S00E01.mkv"
        self.assertIsNone(self.gate.sample(special))
        self.assertIsNotNone(bridge.Gate({"threshold": 0.9, "include_specials": True}).sample(special))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path.cwd() / "work" / "test-tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        # mkdir's inherited ACL works with Windows sandbox users; mkdtemp mode 0700 doesn't.
        self.temp_path = self.temp_root / uuid.uuid4().hex
        self.temp_path.mkdir()
        self.path = self.temp_path / "state.sqlite3"
        self.state = bridge.State(self.path)
        self.api = FakeApi()
        self.ep = bridge.parse_episode(NAME)
        self.row = self.state.enqueue("test-account", 2691, 8702, self.ep, NAME, "2026-10-04")

    def tearDown(self):
        self.state.close()
        assert self.temp_path.resolve().is_relative_to(self.temp_root.resolve())
        shutil.rmtree(self.temp_path)

    def refresh(self):
        return self.state.get("test-account", 2691, 1, 4)

    def test_diary_payload_uses_local_calendar_date(self):
        bridge.sync_row(self.state, self.api, self.row)
        payload = self.api.posts[0]
        self.assertEqual(payload["backdate"], "2026-10-04T12:00:00Z")
        self.assertIs(payload["is_log"], True)
        self.assertEqual(payload["season_id"], 8702)
        self.assertEqual(payload["episode_number"], 4)
        self.assertEqual(self.refresh()["status"], "logged")

    def test_duplicate_across_restart_and_filename_change(self):
        bridge.sync_row(self.state, self.api, self.row)
        self.state.close()
        self.state = bridge.State(self.path)
        row = self.state.enqueue("test-account", 2691, 8702, self.ep, "Another.Release.S01E04.mkv", "2026-10-05")
        bridge.sync_row(self.state, self.api, row)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(row["watched_date"], "2026-10-04")

    def test_existing_diary_entry(self):
        self.api.existing = {"id": 52, "isLog": True}
        bridge.sync_row(self.state, self.api, self.row)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.refresh()["status"], "existing")
        bridge.undo_row(self.state, self.api, self.refresh())
        self.assertEqual(self.api.deleted, [])

    def test_uncertain_post_never_automatically_repeats(self):
        self.api.post_failure = True
        bridge.sync_row(self.state, self.api, self.row)
        self.assertEqual(self.refresh()["status"], "uncertain")
        bridge.sync_row(self.state, self.api, self.refresh())
        self.assertEqual(len(self.api.posts), 1)
        # A retry first discovers that the original request actually succeeded.
        self.api.existing = {"id": 101, "isLog": True}
        bridge.sync_row(self.state, self.api, self.refresh(), explicit_retry=True)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.refresh()["status"], "existing")

    def test_explicit_retry_when_remote_confirms_absent(self):
        self.state.update(self.row, "uncertain")
        bridge.sync_row(self.state, self.api, self.refresh(), explicit_retry=True)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.refresh()["status"], "logged")

    def test_preflight_offline_keeps_pending_and_original_date(self):
        self.api.preflight_failure = True
        bridge.sync_row(self.state, self.api, self.row)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.refresh()["status"], "pending")
        self.api.preflight_failure = False
        bridge.sync_row(self.state, self.api, self.refresh())
        self.assertEqual(self.api.posts[0]["backdate"], "2026-10-04T12:00:00Z")

    def test_checkbox_failure_cannot_duplicate_diary(self):
        self.api.checkbox_failure = True
        bridge.sync_row(self.state, self.api, self.row)
        bridge.sync_row(self.state, self.api, self.refresh())
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.refresh()["status"], "logged")
        self.assertIn("checkbox", self.refresh()["error"])

    def test_undo_only_own_confirmed_entry_and_suppresses_relog(self):
        bridge.sync_row(self.state, self.api, self.row)
        bridge.undo_row(self.state, self.api, self.refresh())
        self.assertEqual(self.api.deleted, [101])
        self.assertEqual(self.refresh()["status"], "deleted")
        bridge.sync_row(self.state, self.api, self.refresh())
        self.assertEqual(len(self.api.posts), 1)

    def test_uncertain_undo_blocks_automatic_posts(self):
        bridge.sync_row(self.state, self.api, self.row)
        self.api.delete_failure = True
        bridge.undo_row(self.state, self.api, self.refresh())
        bridge.sync_row(self.state, self.api, self.refresh())
        self.assertEqual(self.refresh()["status"], "delete_uncertain")
        self.assertEqual(len(self.api.posts), 1)

    def test_accounts_are_isolated(self):
        second = self.state.enqueue("second-account", 2691, 8702, self.ep, NAME, "2026-10-05")
        bridge.sync_row(self.state, self.api, self.row)
        bridge.sync_row(self.state, self.api, second)
        self.assertEqual(len(self.api.posts), 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows lock")
    def test_concurrent_players_cannot_duplicate_post(self):
        def submit(_):
            state = bridge.State(self.path)
            try:
                with bridge.ApiLock():
                    row = state.enqueue("test-account", 2691, 8702, self.ep, NAME, "2026-10-04")
                    return bridge.sync_row(state, self.api, row)
            finally:
                state.close()
        with patch.object(bridge, "LOCAL", self.temp_path):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(submit, range(2)))
        self.assertEqual(len(self.api.posts), 1)


class ApiAndCatalogTests(unittest.TestCase):
    def test_remote_ratings_do_not_count_as_diary_entries(self):
        api = bridge.Serializd()
        api.request = Mock(return_value={"userReviews": [
            {"id": 1, "isLog": False, "seasonId": 8702, "episodeNumber": 4},
            {"id": 2, "isLog": True, "seasonId": 8702, "episodeNumber": 5}]})
        self.assertIsNone(api.existing_log("test", 2691, 8702, 4))
        api.request.return_value["userReviews"].append({"id": 3, "isLog": True, "seasonId": 8702, "episodeNumber": 4})
        self.assertEqual(api.existing_log("test", 2691, 8702, 4)["id"], 3)

    def test_changed_duplicate_response_fails_closed(self):
        api = bridge.Serializd()
        api.request = Mock(return_value={"unexpected": []})
        with self.assertRaises(bridge.ApiError):
            api.existing_log("test", 2691, 8702, 4)

    def test_incomplete_remote_entry_fails_closed(self):
        api = bridge.Serializd()
        api.request = Mock(return_value={"userReviews": [{"id": 3, "seasonId": 8702, "episodeNumber": 4}]})
        with self.assertRaises(bridge.ApiError):
            api.existing_log("test", 2691, 8702, 4)

    def test_exact_show_match_year_and_season_cache(self):
        api = Mock()
        api.request.side_effect = [
            {"results": [{"id": 1, "name": "Show", "firstAirDate": "2000-01-01"},
                         {"id": 2, "name": "Show", "firstAirDate": "2020-01-01"}]},
            {"seasonId": 30, "episodes": [{"episodeNumber": 1}, {"episodeNumber": 2}]}]
        catalog = bridge.Catalog(api, {})
        self.assertEqual(catalog.resolve(bridge.Episode("Show", 2020, 1, 1)), (2, 30))
        self.assertEqual(catalog.resolve(bridge.Episode("Show", 2020, 1, 2)), (2, 30))
        self.assertEqual(api.request.call_count, 2)

    def test_ambiguous_show_is_not_guessed(self):
        api = Mock()
        api.request.return_value = {"results": [{"id": 1, "name": "Show"}, {"id": 2, "name": "Show"}]}
        with self.assertRaises(bridge.ApiError):
            bridge.Catalog(api, {}).resolve(bridge.Episode("Show", None, 1, 1))

    def test_episode_validation(self):
        api = Mock()
        api.request.return_value = {"seasonId": 5, "episodes": [{"episodeNumber": 1}]}
        with self.assertRaises(bridge.ApiError):
            bridge.Catalog(api, {"show_ids": {"show": 2}}).resolve(bridge.Episode("Show", None, 1, 100))

    def test_no_id_is_not_treated_as_success(self):
        api = bridge.Serializd()
        api.request = Mock(return_value={"success": True})
        with self.assertRaises(bridge.ApiError):
            api.add_diary({})

    def test_request_method_headers_and_json(self):
        api = bridge.Serializd("fake-test-token")
        api.opener = Mock()
        api.opener.open.return_value = io.BytesIO(b'{"id": 101}')
        self.assertEqual(api.request("/api/show/reviews/add", {"is_log": True}), {"id": 101})
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-test-token")
        self.assertEqual(json.loads(request.data), {"is_log": True})

    def test_action_accepts_empty_success_response(self):
        api = bridge.Serializd()
        api.opener = Mock()
        api.opener.open.return_value = io.BytesIO(b'')
        api.mark_watched(2691, 8702, 4)

    def test_action_rejects_explicit_failure(self):
        api = bridge.Serializd()
        api.opener = Mock()
        api.opener.open.return_value = io.BytesIO(b'{"success": false}')
        with self.assertRaises(bridge.ApiError):
            api.mark_watched(2691, 8702, 4)

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_encrypted_token_roundtrip(self):
        sample = b'{"token":"dummy-test-value"}'
        protected = bridge.dpapi(sample)
        self.assertNotIn(b"dummy-test-value", protected)
        self.assertEqual(bridge.dpapi(protected, decrypt=True), sample)


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path.cwd() / "work" / "test-tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.folder = self.temp_root / uuid.uuid4().hex
        self.folder.mkdir()
        self.project = self.folder / "project"
        self.project.mkdir()
        (self.project / "autostart.lua.in").write_text((bridge.ROOT / "autostart.lua.in").read_text(encoding="utf-8"), encoding="utf-8")
        self.local = self.project / ".local"
        self.local.mkdir()
        self.mpv = self.folder / "bin" / "mpv.exe"
        self.mpv.parent.mkdir()
        self.appdata = self.folder / "appdata"
        self.patches = [patch.object(bridge, "ROOT", self.project), patch.object(bridge, "LOCAL", self.local),
                        patch.object(bridge.shutil, "which", return_value=str(self.mpv)),
                        patch.dict(bridge.os.environ, {"APPDATA": str(self.appdata)}, clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        assert self.folder.resolve().is_relative_to(self.temp_root.resolve())
        shutil.rmtree(self.folder)

    def test_configuration_directory_priority(self):
        self.assertEqual(bridge.mpv_config_directory(self.mpv), self.appdata / "mpv")
        portable = self.mpv.parent / "portable_config"
        portable.mkdir()
        self.assertEqual(bridge.mpv_config_directory(self.mpv), portable)
        custom = self.folder / "custom"
        with patch.dict(bridge.os.environ, {"MPV_HOME": str(custom)}):
            self.assertEqual(bridge.mpv_config_directory(self.mpv), custom.resolve())

    def test_install_uninstall_preserve_other_configuration(self):
        config_dir = self.appdata / "mpv"
        config_dir.mkdir(parents=True)
        conf = config_dir / "mpv.conf"
        conf.write_text("save-position-on-quit=yes\n", encoding="utf-8")
        markers = config_dir / "watch_later"
        markers.mkdir()
        marker = markers / "test-marker"
        marker.write_text("start=123.0\n", encoding="utf-8")
        path = bridge.install_autostart()
        self.assertTrue(path.exists())
        self.assertIn("pythonw.exe", path.read_text(encoding="utf-8"))
        self.assertNotIn("__PROJECT_LITERAL__", path.read_text(encoding="utf-8"))
        bridge.install_autostart()
        self.assertEqual(bridge.uninstall_autostart(), path)
        self.assertFalse(path.exists())
        self.assertEqual(conf.read_text(encoding="utf-8"), "save-position-on-quit=yes\n")
        self.assertEqual(marker.read_text(encoding="utf-8"), "start=123.0\n")

    def test_refuse_to_overwrite_unrelated_script(self):
        path = self.appdata / "mpv" / "scripts" / "serializd-autostart.lua"
        path.parent.mkdir(parents=True)
        path.write_text("-- user script\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            bridge.install_autostart()
        self.assertEqual(path.read_text(encoding="utf-8"), "-- user script\n")

    def test_no_session_is_preview(self):
        with patch.object(bridge, "load_session") as load:
            self.assertFalse(bridge.automatic_live({"auto_mode": "auto"}))
            load.assert_not_called()

    def test_local_login_enables_auto_live(self):
        (self.local / "session.dat").write_bytes(b"dummy test session")
        with patch.object(bridge, "load_session", return_value={"username": "dummy"}) as load:
            self.assertTrue(bridge.automatic_live({"auto_mode": "auto"}))
            load.assert_called_once()

    def test_explicit_preview_ignores_saved_login(self):
        (self.local / "session.dat").write_bytes(b"dummy test session")
        with patch.object(bridge, "load_session") as load:
            self.assertFalse(bridge.automatic_live({"auto_mode": "dry-run"}))
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
