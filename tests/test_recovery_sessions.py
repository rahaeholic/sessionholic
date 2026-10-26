"""Private, exact recovery locators; no CLI, SSH or conversation is consulted."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

import session_recovery as recovery


class RecoverySessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = recovery.RecoverySessions(self.root / "state")
        self.source = {"host": "fixture-pc", "agent": "codex", "home": ".codex",
                       "id": "019aaaaa-0000-7000-8000-000000000009", "cwd": "C:\\Projects\\fixture",
                       "title": "합성 작업", "account": "기기 Codex", "hostLabel": "합성 PC",
                       "updatedAt": 100, "openedAt": 200}
        self.terminal_id = "a" * 32

    def remember(self, **changes):
        return self.store.remember({**self.source, **changes}, self.terminal_id)

    def test_private_atomic_record_survives_restart_without_conversation_or_credentials(self):
        source = {**self.source, "messages": [{"text": "never stored body"}],
                  "snippet": "never stored preview", "env": {"TOKEN": "never stored secret"}}
        first = self.store.remember(source, self.terminal_id)
        replacement = recovery.RecoverySessions(self.store.directory)
        self.assertEqual(replacement.get(self.source), first)
        self.assertEqual(first["source"], {key: source[key] for key in ("host", "agent", "home", "id", "cwd")})
        raw = self.store.path.read_text()
        for private in ("never stored", "messages", "snippet", "TOKEN", '"env"'):
            self.assertNotIn(private, raw)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.lock_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)

    def test_identical_observation_does_not_write_or_refresh_opened_time(self):
        first = self.remember()
        before = self.store.state_key()
        with patch.object(self.store, "_write", side_effect=AssertionError("unnecessary write")), \
                patch.object(recovery.time, "time", return_value=99999):
            same = self.store.remember({key: value for key, value in self.source.items() if key != "openedAt"}, self.terminal_id)
        self.assertEqual(same, first)
        self.assertEqual(self.store.state_key(), before)
        self.assertEqual(self.store.snapshot()["revision"], 1)

    def test_explicit_open_updates_recency_and_snapshot_returns_copies(self):
        self.remember()
        newest = self.remember(openedAt=400)
        snap = self.store.snapshot()
        self.assertEqual((snap["ok"], snap["revision"], newest["openedAt"]), (True, 2, 400))
        snap["sessions"][0]["source"]["cwd"] = "/elsewhere"
        newest["source"]["cwd"] = "/elsewhere"
        self.assertEqual(self.store.get(self.source)["source"]["cwd"], self.source["cwd"])

    def test_host_and_codex_home_never_collide(self):
        self.remember()
        self.remember(home=".codex-personal")
        self.remember(host="fixture-mini")
        self.assertEqual(len(self.store.snapshot()["sessions"]), 3)
        for home in (".codex", ".codex-personal"):
            self.assertEqual(self.store.get({**self.source, "home": home})["source"]["home"], home)
        self.assertIsNone(self.store.get({**self.source, "host": "other-pc"}))

    def test_claude_default_home_aliases_and_reused_worker_id_are_separate(self):
        base = {**self.source, "agent": "claude", "id": "worker-fixture", "home": None,
                "sessionId": "019aaaaa-0000-7000-8000-000000000010", "kind": "background"}
        self.store.remember(base)
        for home in (None, "", ".claude"):
            self.assertEqual(self.store.get({**base, "home": home})["source"]["home"], ".claude")
        next_source = {**base, "sessionId": "019aaaaa-0000-7000-8000-000000000011", "openedAt": 300}
        self.store.remember(next_source)
        self.assertEqual(len(self.store.snapshot()["sessions"]), 2)
        self.assertEqual(self.store.get(base)["source"]["sessionId"], base["sessionId"])
        ambiguous = {key: value for key, value in base.items() if key != "sessionId"}
        self.assertIsNone(self.store.get(ambiguous))
        self.assertFalse(self.store.forget(ambiguous))
        self.assertTrue(self.store.forget(base))
        self.assertEqual(self.store.get(next_source)["source"]["sessionId"], next_source["sessionId"])

    def test_changed_cwd_or_execution_kind_cannot_replace_a_saved_identity(self):
        first = self.remember()
        self.assertIsNone(self.store.get({**self.source, "cwd": "C:\\Other"}))
        with self.assertRaises(recovery.RecoveryError) as raised:
            self.remember(cwd="C:\\Other")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.store.get(self.source), first)
        claude = {**self.source, "agent": "claude", "home": ".claude", "kind": "interactive"}
        self.store.remember(claude)
        with self.assertRaises(recovery.RecoveryError):
            self.store.remember({**claude, "kind": "background"})

    def test_a_claude_session_that_moved_folders_keeps_its_first_folder_and_can_be_ended(self):
        # A Claude session's cwd follows its shell (into a worktree); ending it saves its locator
        # first and was refused as a conflict, so 세션 종료 failed on every try.
        claude = {**self.source, "agent": "claude", "home": ".claude", "id": "d8d67c5e", "kind": "background",
                  "sessionId": "d8d67c5e-0710-4aec-b909-a641f745c3ed", "cwd": "/Users/fixture/Personal"}
        first = self.store.remember(claude, self.terminal_id, "released")
        moved = {**claude, "cwd": "/Users/fixture/Personal/repo/.claude/worktrees/task"}
        ended = self.store.remember({**moved, "explicitTermination": True})
        self.assertEqual(ended["source"]["cwd"], "/Users/fixture/Personal", 'its transcript and resume stay there')
        self.assertEqual(ended["provenance"]["closure"], "explicit_termination")
        self.assertEqual(ended["openedAt"], first["openedAt"])
        self.assertEqual(self.store.get(claude)["provenance"]["closure"], "explicit_termination")

    def test_legacy_user_close_and_explicit_termination_are_distinct_and_sticky(self):
        legacy = self.store.remember(self.source, self.terminal_id, "user")
        self.assertEqual(legacy["provenance"]["closure"], "legacy_user_close")
        observed = self.store.remember(self.source, 'b' * 32, 'released')
        self.assertEqual(observed['provenance']['closure'], 'legacy_user_close')
        explicit = self.store.remember({**self.source, "explicitTermination": True}, self.terminal_id, "user")
        self.assertEqual(explicit["provenance"]["closure"], "explicit_termination")
        observed = self.store.remember(self.source, "b" * 32, "released")
        self.assertEqual(observed["provenance"]["closure"], "explicit_termination")
        self.assertEqual(self.store.get(self.source)["provenance"]["closure"], "explicit_termination")

    def test_connection_closure_and_forget_do_not_change_native_identity(self):
        record = self.store.remember(self.source, self.terminal_id, "connection_ended")
        self.assertEqual(record["source"]["id"], self.source["id"])
        self.assertEqual(record["provenance"]["closure"], "connection")
        self.assertTrue(self.store.forget(self.source))
        self.assertIsNone(self.store.get(self.source))
        self.assertFalse(self.store.forget(self.source))

    def test_server_confirmed_view_close_is_recoverable_without_reinterpreting_old_user_close(self):
        view = self.store.remember(self.source, self.terminal_id, "view_closed")
        self.assertEqual(view["provenance"], {"kind": "terminal-native", "terminalId": self.terminal_id,
                                              "closedReason": "view_closed", "closure": "connection"})
        other = {**self.source, "id": str(uuid.UUID(int=99))}
        legacy = self.store.remember(other, self.terminal_id, "user")
        self.assertEqual(legacy["provenance"]["closure"], "legacy_user_close")

    def test_invalid_identity_home_cwd_terminal_or_estimated_proof_is_rejected(self):
        for changes in ({"host": "../elsewhere"}, {"agent": "shell"}, {"id": "not-a-uuid"},
                        {"home": None}, {"home": ".codex/../other"}, {"cwd": "relative"},
                        {"cwd": "C:\\fixture\x00suffix"}, {"linkConfidence": "estimated"},
                        {"agent": "claude", "home": ".claude-work"}, {"openedAt": True}):
            with self.subTest(changes=changes), self.assertRaises(recovery.RecoveryError):
                self.remember(**changes)
        with self.assertRaises(recovery.RecoveryError):
            self.store.remember(self.source, "short-terminal")
        with self.assertRaises(recovery.RecoveryError):
            self.store.remember(self.source, self.terminal_id, "guessed")

    def test_corrupt_or_invalid_store_returns_no_locators_and_is_not_overwritten(self):
        self.remember()
        valid = json.loads(self.store.path.read_text())
        invalid_values = [b"{broken", b"[]", b'{"version":1,"version":1,"revision":0,"sessions":{}}',
                          json.dumps({**valid, "version": 2}).encode(),
                          json.dumps({**valid, "revision": True}).encode(),
                          json.dumps({**valid, "unexpected": "field"}).encode()]
        wrong_key = copy.deepcopy(valid)
        wrong_key["sessions"]["bad-key"] = wrong_key["sessions"].pop(next(iter(wrong_key["sessions"])))
        invalid_values.append(json.dumps(wrong_key).encode())
        wrong_row = copy.deepcopy(valid)
        next(iter(wrong_row["sessions"].values()))["source"]["token"] = "untrusted"
        invalid_values.append(json.dumps(wrong_row).encode())
        for raw in invalid_values:
            with self.subTest(raw=raw[:20]):
                self.store.path.write_bytes(raw)
                for operation in (lambda: self.store.get(self.source), self.store.snapshot, self.remember,
                                  lambda: self.store.forget(self.source)):
                    with self.assertRaises(recovery.RecoveryError):
                        operation()
                self.assertEqual(self.store.path.read_bytes(), raw)

    def test_oversized_store_is_rejected_before_decoding(self):
        self.remember()
        oversized = b" " * (recovery.MAX_BYTES + 1)
        self.store.path.write_bytes(oversized)
        with self.assertRaises(recovery.RecoveryError):
            self.store.get(self.source)
        self.assertEqual(self.store.path.stat().st_size, len(oversized))

    def test_bounded_count_evicts_oldest_reference_and_preserves_newest(self):
        self.assertEqual(recovery.MAX_REFS, 500)
        with patch.object(recovery, "MAX_REFS", 3):
            for index in range(4):
                self.remember(id=str(uuid.UUID(int=index + 1)), openedAt=index + 1)
            rows = self.store.snapshot()["sessions"]
        self.assertEqual([row["openedAt"] for row in rows], [4, 3, 2])
        self.assertIsNone(self.store.get({**self.source, "id": str(uuid.UUID(int=1))}))

    def test_bounded_bytes_evicts_old_refs_without_truncating_identity(self):
        self.assertEqual(recovery.MAX_BYTES, 1024 * 1024)
        with patch.object(recovery, "MAX_BYTES", 1100):
            for index in range(5):
                self.remember(id=str(uuid.UUID(int=index + 1)), openedAt=index + 1)
            rows = self.store.snapshot()["sessions"]
            self.assertLessEqual(self.store.path.stat().st_size, 1100)
            self.assertLess(len(rows), 5)
            self.assertEqual(rows[0]["source"]["id"], str(uuid.UUID(int=5)))

    def test_failed_atomic_replace_preserves_previous_record_and_cleans_tempfile(self):
        first = self.remember()
        before = self.store.path.read_bytes()
        with patch.object(recovery.os, "replace", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(recovery.RecoveryError):
                self.remember(title="다음 제목")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(self.source), first)
        self.assertEqual(list(self.store.directory.glob(".recovery-*.tmp")), [])

    def test_unsafe_permissions_hardlinks_and_special_files_fail_closed(self):
        self.remember()
        for path in (self.store.path, self.store.lock_path):
            path.chmod(0o644)
            with self.assertRaises(recovery.RecoveryError):
                self.store.get(self.source)
            self.assertIsNone(self.store.state_key())
            path.chmod(0o600)
        self.store.directory.chmod(0o755)
        with self.assertRaises(recovery.RecoveryError):
            self.store.snapshot()
        self.store.directory.chmod(0o700)
        os.link(self.store.path, self.root / "hardlink")
        with self.assertRaises(recovery.RecoveryError):
            self.store.get(self.source)
        (self.root / "hardlink").unlink()
        self.store.path.unlink()
        os.mkfifo(self.store.path, 0o600)
        with self.assertRaises(recovery.RecoveryError):
            self.store.snapshot()

    def test_symlink_in_directory_store_or_lock_is_rejected_without_target_writes(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        (self.root / "alias").symlink_to(target, target_is_directory=True)
        with self.assertRaises(recovery.RecoveryError):
            recovery.RecoverySessions(self.root / "alias" / "child").remember(self.source)
        self.assertEqual(list(target.iterdir()), [])
        self.remember()
        original = self.store.path.read_bytes()
        for path in (self.store.path, self.store.lock_path):
            previous = path.read_bytes()
            path.unlink()
            destination = target / path.name
            destination.write_bytes(previous)
            destination.chmod(0o600)
            path.symlink_to(destination)
            with self.assertRaises(recovery.RecoveryError):
                self.store.remember(self.source)
            self.assertEqual(destination.read_bytes(), previous)
            path.unlink()
            path.write_bytes(previous)
            path.chmod(0o600)
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_independent_store_instances_serialize_updates(self):
        def remember(index):
            store = recovery.RecoverySessions(self.store.directory)
            return store.remember({**self.source, "id": str(uuid.UUID(int=index + 1)), "openedAt": index + 1})
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(remember, range(12)))
        snapshot = self.store.snapshot()
        self.assertEqual((len(snapshot["sessions"]), snapshot["revision"]), (12, 12))
        self.assertEqual({row["source"]["id"] for row in rows},
                         {row["source"]["id"] for row in snapshot["sessions"]})


if __name__ == "__main__":
    unittest.main()
