import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graft import core

ACCOUNT = "11111111-1111-1111-1111-111111111111"
ORG = "22222222-2222-2222-2222-222222222222"


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.closed = patch("graft.core.platform.running_claudes", return_value=[])
        self.closed.start()
        self.addCleanup(self.closed.stop)
        self.manager = core.Manager(self.root)
        self.profiles = [self.manager.add_profile("Work"), self.manager.add_profile("Personal")]
        self.folders = []
        for profile in self.profiles:
            root = self.manager.profile_path(profile["id"])
            (root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ACCOUNT, "secret": "not copied"}))
            folder = root / core.STORES[0] / ACCOUNT / ORG
            folder.mkdir(parents=True)
            self.folders.append(folder)
        self.group = self.manager.set_group("Shared", [p["id"] for p in self.profiles])

    def record(self, index, name="local_chat-a.json", title="hello", **extra):
        path = self.folders[index] / name
        path.write_text(json.dumps({"sessionId": name.removesuffix(".json"), "title": title, **extra}), encoding="utf-8")
        return path

    def test_new_records_union_and_idempotence(self):
        self.record(0)
        self.record(1, "local_chat-b.json")
        report = self.manager.merge()
        self.assertEqual(report["copied"], 2)
        self.assertEqual(self.manager.merge()["copied"], 0)
        self.assertEqual((self.folders[0] / "local_chat-a.json").read_bytes(), (self.folders[1] / "local_chat-a.json").read_bytes())

    def test_changed_record_propagates_and_old_content_backed_up(self):
        original = self.record(0).read_bytes()
        self.manager.merge()
        self.record(1, title="renamed", isArchived=True)
        result = self.manager.merge()
        self.assertEqual(result["copied"], 1)
        self.assertEqual(result["backups"], 1)
        self.assertEqual(json.loads((self.folders[0] / "local_chat-a.json").read_text())["title"], "renamed")
        self.assertEqual((self.manager.backup_dir / "blobs" / core._hash(original)).read_bytes(), original)

    def test_deletion_propagates_when_other_records_remain(self):
        self.record(0)
        self.record(0, "local_keep.json")
        self.manager.merge()
        (self.folders[1] / "local_chat-a.json").unlink()
        self.assertEqual(self.manager.merge()["deleted"], 1)
        self.assertFalse((self.folders[0] / "local_chat-a.json").exists())

    def test_emptied_folder_is_refilled_not_bulk_deleted(self):
        self.record(0)
        self.manager.merge()
        (self.folders[1] / "local_chat-a.json").unlink()
        report = self.manager.merge()
        self.assertEqual(report["deleted"], 0)
        self.assertTrue(report["warnings"])
        self.assertTrue((self.folders[1] / "local_chat-a.json").exists())

    def test_simultaneous_edits_preserved(self):
        self.record(0)
        self.manager.merge()
        one = self.record(0, title="one").read_bytes()
        other = self.record(1, title="two").read_bytes()
        result = self.manager.merge()
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual((self.folders[0] / "local_chat-a.json").read_bytes(), one)
        self.assertEqual((self.folders[1] / "local_chat-a.json").read_bytes(), other)
        self.assertEqual(self.manager.merge()["conflicts"], 1)

    def test_initial_collision_not_overwritten(self):
        self.record(0, title="one")
        self.record(1, title="two")
        self.assertEqual(self.manager.merge()["conflicts"], 1)

    def test_delete_edit_conflict_preserved(self):
        self.record(0)
        self.record(0, "local_keep.json")
        self.manager.merge()
        (self.folders[0] / "local_chat-a.json").unlink()
        other = self.record(1, title="changed").read_bytes()
        self.assertEqual(self.manager.merge()["conflicts"], 1)
        self.assertFalse((self.folders[0] / "local_chat-a.json").exists())
        self.assertEqual((self.folders[1] / "local_chat-a.json").read_bytes(), other)

    def test_three_member_group_converges_in_one_pass(self):
        profile = self.manager.add_profile("Third")
        root = self.manager.profile_path(profile["id"])
        (root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ACCOUNT}))
        folder = root / core.STORES[0] / ACCOUNT / ORG
        folder.mkdir(parents=True)
        self.manager.set_group("Shared", [p["id"] for p in self.profiles] + [profile["id"]])
        self.record(0)
        self.assertEqual(self.manager.merge()["copied"], 2)
        self.assertTrue((folder / "local_chat-a.json").exists())

    def test_credentials_and_unknown_files_untouched(self):
        self.record(0)
        unknown = self.folders[0] / "credentials.json"
        unknown.write_text("secret")
        configs = [(self.manager.profile_path(p["id"]) / "config.json").read_bytes() for p in self.profiles]
        self.manager.merge()
        self.assertFalse((self.folders[1] / "credentials.json").exists())
        self.assertEqual(configs, [(self.manager.profile_path(p["id"]) / "config.json").read_bytes() for p in self.profiles])

    def test_running_process_blocks_all_writes(self):
        self.record(0)
        with patch("graft.core.platform.running_claudes", return_value=[42]):
            with self.assertRaisesRegex(core.GraftError, "Quit every"):
                self.manager.merge()
        self.assertFalse((self.folders[1] / "local_chat-a.json").exists())

    def test_process_enumeration_error_blocks(self):
        self.record(0)
        with patch("graft.core.platform.running_claudes", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                self.manager.merge()
        self.assertFalse((self.folders[1] / "local_chat-a.json").exists())

    def test_malformed_record_blocks_all_profile_writes(self):
        self.record(0)
        (self.folders[1] / "local_bad.json").write_text("{")
        with self.assertRaisesRegex(core.GraftError, "Unreadable chat"):
            self.manager.merge()
        self.assertFalse((self.folders[1] / "local_chat-a.json").exists())

    def test_missing_profile_does_not_recreate_history(self):
        self.record(0)
        self.manager.merge()
        self.folders[1].rename(self.folders[1].with_name("hidden-org"))
        with self.assertRaises(core.GraftError):
            self.manager.merge()
        self.assertTrue((self.folders[0] / "local_chat-a.json").exists())

    def test_read_failure_is_not_a_deletion(self):
        path = self.record(0)
        self.manager.merge()
        original = Path.read_bytes
        def unreadable(p):
            if p == path:
                raise PermissionError("sharing violation")
            return original(p)
        with patch.object(Path, "read_bytes", unreadable):
            with self.assertRaises(PermissionError):
                self.manager.merge()
        self.assertTrue((self.folders[1] / path.name).exists())

    def test_interrupted_write_has_retryable_journal(self):
        self.record(0)
        atomic = core._atomic
        def fail(path, data):
            if path.parent == self.folders[1]:
                raise PermissionError("file locked")
            atomic(path, data)
        with patch("graft.core._atomic", side_effect=fail):
            with self.assertRaises(PermissionError):
                self.manager.merge()
        self.assertTrue(self.manager.pending_file.exists())
        report = self.manager.merge()
        self.assertTrue(report["recovered"])
        self.assertFalse(self.manager.pending_file.exists())
        self.assertTrue((self.folders[1] / "local_chat-a.json").exists())

    def test_recovery_does_not_overwrite_external_change(self):
        self.record(0)
        with patch("graft.core.Manager._recover", side_effect=[False, PermissionError("locked")]):
            with self.assertRaises(PermissionError):
                self.manager.merge()
        changed = self.record(1, title="outside change").read_bytes()
        with self.assertRaisesRegex(core.GraftError, "changed during"):
            self.manager.merge()
        self.assertEqual((self.folders[1] / "local_chat-a.json").read_bytes(), changed)

    def interrupted_deletion(self):
        original = self.record(0).read_bytes()
        self.record(0, "local_keep.json")
        self.manager.merge()
        (self.folders[0] / "local_chat-a.json").unlink()
        with patch("graft.core.Manager._recover", side_effect=[False, PermissionError("locked")]):
            with self.assertRaises(PermissionError):
                self.manager.merge()
        return original

    def test_missing_preimage_blocks_recovery_deletion(self):
        original = self.interrupted_deletion()
        (self.manager.backup_dir / "blobs" / core._hash(original)).unlink()
        with self.assertRaisesRegex(core.GraftError, "backup is missing"):
            self.manager.merge()
        self.assertTrue((self.folders[1] / "local_chat-a.json").exists())
        self.assertTrue(self.manager.pending_file.exists())

    def test_corrupt_preimage_blocks_recovery_deletion(self):
        original = self.interrupted_deletion()
        (self.manager.backup_dir / "blobs" / core._hash(original)).write_bytes(b"corrupt")
        with self.assertRaisesRegex(core.GraftError, "backup is corrupt"):
            self.manager.merge()
        self.assertTrue((self.folders[1] / "local_chat-a.json").exists())

    def test_missing_after_key_is_not_implicit_deletion(self):
        self.interrupted_deletion()
        journal = json.loads(self.manager.pending_file.read_text())
        del journal["operations"][0]["after"]
        self.manager.pending_file.write_text(json.dumps(journal))
        with self.assertRaisesRegex(core.GraftError, "Invalid recovery operation"):
            self.manager.merge()
        self.assertTrue((self.folders[1] / "local_chat-a.json").exists())

    def test_null_recovery_journal_is_not_overwritten(self):
        self.record(0)
        self.manager.pending_file.write_text("null")
        with self.assertRaisesRegex(core.GraftError, "Invalid recovery journal"):
            self.manager.merge()
        self.assertEqual(self.manager.pending_file.read_text(), "null")
        self.assertFalse((self.folders[1] / "local_chat-a.json").exists())
        self.assertFalse(self.manager.state_file.exists())

    def test_pending_blocks_removal(self):
        self.manager.pending_file.write_text("{}")
        with self.assertRaisesRegex(core.GraftError, "unfinished"):
            self.manager.remove_profile(self.profiles[0]["id"])

    def test_remove_profile_preserves_data(self):
        record = self.record(0)
        self.manager.remove_profile(self.profiles[0]["id"])
        self.assertTrue(record.exists())
        self.assertEqual(self.manager.groups(), [])

    def test_unicode_and_windows_reserved_names_safe_generated_folders(self):
        for name in ["CON", "Work: Personal / 😺", "NUL.txt", "LPT1."]:
            profile = self.manager.add_profile(name)
            self.assertEqual(profile["folder"], "Claude-Graft-" + profile["id"])

    def test_manifest_path_traversal_rejected(self):
        config = self.manager._load_config()
        config["profiles"][0]["folder"] = "../other"
        self.manager.config_file.write_text(json.dumps(config))
        with self.assertRaises(core.GraftError):
            self.manager.profiles()

    def test_corrupt_state_not_silently_reset(self):
        self.manager.state_file.write_text("{")
        with self.assertRaises(core.GraftError):
            self.manager.merge()
        self.assertEqual(self.manager.state_file.read_text(), "{")

    def test_unknown_group_member_and_overlapping_groups_rejected(self):
        with self.assertRaises(core.GraftError):
            self.manager.set_group("Bad", ["main", "missing"])
        with self.assertRaises(core.GraftError):
            self.manager.set_group("Bad", ["main", self.profiles[0]["id"]])

    def test_account_switch_loses_baseline_safely(self):
        self.record(0)
        self.manager.merge()
        other = "99999999-9999-9999-9999-999999999999"
        root = self.manager.profile_path(self.profiles[1]["id"])
        (root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": other}))
        folder = root / core.STORES[0] / other / ORG
        folder.mkdir(parents=True)
        self.assertEqual(self.manager.merge()["copied"], 1)
        self.assertTrue((folder / "local_chat-a.json").exists())

    def test_ambiguous_cross_account_orgs_block(self):
        other = "99999999-9999-9999-9999-999999999999"
        root = self.manager.profile_path(self.profiles[1]["id"])
        (root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": other}))
        (root / core.STORES[0] / other / ORG).mkdir(parents=True)
        (root / core.STORES[0] / other / "33333333").mkdir()
        with self.assertRaisesRegex(core.GraftError, "exactly one"):
            self.manager.merge()

    def test_symlink_record_rejected(self):
        target = self.root / "outside.txt"
        target.write_text("{}")
        try:
            (self.folders[0] / "local_link.json").symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation requires Windows Developer Mode or admin")
        with self.assertRaisesRegex(core.GraftError, "reparse"):
            self.manager.merge()

    def test_operation_lock_prevents_concurrent_manager(self):
        other = core.Manager(self.root)
        with self.manager.operation_lock():
            with self.assertRaisesRegex(core.GraftError, "in progress"):
                other.add_profile("blocked")

    def test_unknown_record_schema_rejected(self):
        (self.folders[0] / "local_unknown.json").write_text('{}')
        with self.assertRaisesRegex(core.GraftError, "Unreadable chat"):
            self.manager.merge()
        self.assertFalse((self.folders[1] / "local_unknown.json").exists())

    def test_ambiguous_short_org_mapping_rejected_before_writes(self):
        # A short org must not be paired both with B's short and full org.
        short = ORG[:8]
        self.folders[0].rename(self.folders[0].with_name(short))
        self.folders[0] = self.folders[0].with_name(short)
        (self.folders[1].parent / short).mkdir()
        self.record(0)
        with self.assertRaisesRegex(core.GraftError, "reuse a folder"):
            self.manager.merge()
        self.assertFalse(self.manager.pending_file.exists())

    def test_auto_merge_defaults_off(self):
        self.assertFalse(self.manager.auto_merge())
        self.manager.set_auto_merge(True)
        self.assertTrue(core.Manager(self.root).auto_merge())


if __name__ == "__main__":
    unittest.main()
