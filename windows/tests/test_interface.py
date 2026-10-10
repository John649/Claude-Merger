"""CLI tests on every host, plus an opt-in native Windows GUI smoke test."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from graft.__main__ import main, report_error


class CommandLineTests(unittest.TestCase):
    @patch("graft.core.Manager")
    def test_open_forwards_executable(self, manager):
        self.assertEqual(main(["--open", "profile-123", "--claude", r"C:\Some Folder\Claude.exe"]), 0)
        manager.return_value.open_profile.assert_called_once_with(
            "profile-123", executable=r"C:\Some Folder\Claude.exe")

    @patch("graft.core.Manager")
    def test_merge_prints_result(self, manager):
        manager.return_value.merge.return_value = {"copied": 3, "deleted": 0, "conflicts": 1, "backups": 2}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--merge"]), 2)
        self.assertIn('"copied": 3', output.getvalue())
        self.assertIn('"conflicts": 1', output.getvalue())
        manager.return_value.merge.assert_called_once_with()

    @patch("graft.core.Manager")
    def test_conflict_free_merge_returns_success(self, manager):
        manager.return_value.merge.return_value = {"copied": 0, "conflicts": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--merge"]), 0)

    @patch("graft.core.Manager")
    def test_data_root_is_explicit(self, manager):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main(["--data-dir", folder, "--open", "main"]), 0)
            manager.assert_called_once_with(Path(folder))

    @patch("graft.core.Manager", side_effect=RuntimeError("Invalid configuration\nNothing was changed."))
    def test_safe_error_and_nonzero_status(self, manager):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main(["--merge"]), 1)
        self.assertEqual(output.getvalue(), "Claude Graft: Invalid configuration Nothing was changed.\n")
        self.assertNotIn("Traceback", output.getvalue())

    def test_actions_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--open", "main", "--merge"])
        self.assertEqual(raised.exception.code, 2)

    def test_help_does_not_initialize_state(self):
        with patch("graft.core.Manager") as manager, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--help"])
        manager.assert_not_called()

    @patch("graft.gui.run")
    @patch("graft.core.Manager")
    def test_default_opens_gui(self, manager, gui):
        self.assertEqual(main([]), 0)
        gui.assert_called_once_with(manager.return_value, claude=None, data_dir=None)


class InterfaceHelpersTests(unittest.TestCase):
    def test_shortcut_arguments_are_individual_values(self):
        from graft.gui import shortcut_command
        with patch("sys.frozen", False, create=True):
            target, arguments = shortcut_command("some-id", "Claude Path/Claude.exe", Path("data dir"))
        self.assertTrue(target.is_absolute())
        self.assertTrue(target.is_file())
        self.assertTrue(Path(arguments[0]).is_absolute())
        self.assertEqual(Path(arguments[0]).name, "launch.py")
        self.assertEqual(arguments[1:3], ["--open", "some-id"])
        self.assertIn("--claude", arguments)
        self.assertIn("--data-dir", arguments)
        self.assertIn("data dir", arguments[-1])

    def test_frozen_shortcut_does_not_use_temporary_extraction_path(self):
        from graft.gui import shortcut_command
        with patch("sys.frozen", True, create=True):
            target, arguments = shortcut_command("some-id")
        self.assertEqual(target, Path(sys.executable).resolve())
        self.assertEqual(arguments, ["--open", "some-id"])

    def test_recovery_summary_distinguishes_new_change_counts(self):
        from graft.gui import merge_summary
        result = merge_summary({"recovered": True, "copied": 0})
        self.assertIn("Recovered an interrupted merge", result)
        self.assertIn("new changes after recovery", result)
        self.assertIn("0 copied", result)

    def test_conflict_summary_preserves_warning(self):
        from graft.gui import merge_summary
        result = merge_summary({"copied": 1, "conflicts": 2, "warnings": ["Some records differ."]})
        self.assertIn("2 conflicts", result)
        self.assertIn("left unchanged", result)
        self.assertIn("Some records differ.", result)


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("GRAFT_TK_SMOKE") == "1",
                     "native Tk smoke runs explicitly on Windows CI")
class NativeWindowsSmokeTests(unittest.TestCase):
    def test_process_enumeration_is_native_and_safe(self):
        from graft.platform import running_claudes
        self.assertTrue(all(isinstance(pid, int) and pid > 0 for pid in running_claudes()))

    def test_tk_application_build_refresh_and_close(self):
        import tkinter as tk
        from graft.core import Manager
        from graft.gui import App
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            root.withdraw()
            try:
                manager = Manager(Path(folder))
                first = manager.add_profile("Personal")
                second = manager.add_profile("Work")
                manager.set_group("Shared", [first["id"], second["id"]])
                app = App(root, manager)
                root.update()
                self.assertEqual(len(app.profiles.get_children()), 3)
                self.assertEqual(len(app.groups.get_children()), 1)
                app.refresh()
                root.update()
                self.assertFalse(app.automatic.get())
                app.close()
                self.assertTrue(app.closed)
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass


@unittest.skipUnless(os.environ.get("GRAFT_TK_SMOKE") == "1",
                     "mapped Tk regression requires an explicit graphical test session")
class ProfileListRenderingTests(unittest.TestCase):
    def test_lists_are_uncovered_selectable_and_survive_refresh(self):
        import tkinter as tk
        from graft.core import Manager
        from graft.gui import App
        with tempfile.TemporaryDirectory() as folder:
            manager = Manager(Path(folder))
            first = manager.add_profile("Personal")
            second = manager.add_profile("Work")
            group = manager.set_group("Shared", [first["id"], second["id"]])
            settings_before = manager.config_file.read_bytes()
            root = tk.Tk()
            app = None
            try:
                app = App(root, manager)
                root.deiconify()
                root.lift()
                root.update()
                for tree, row in ((app.profiles, second["id"]), (app.groups, group["id"])):
                    # A tree can have rows, a bbox and winfo_ismapped()==1 while
                    # an incorrectly parented sibling covers every pixel/click.
                    self.assertEqual(tree.winfo_parent(), str(tree.pack_info()["in"]))
                    tree.see(row)
                    root.update()
                    x, y, width, height = tree.bbox(row)
                    self.assertGreater(width, 10)
                    for point in ((x + 10, y + height // 2), (x + 10, 5)):
                        topmost = root.winfo_containing(tree.winfo_rootx() + point[0],
                                                       tree.winfo_rooty() + point[1])
                        self.assertIs(topmost, tree, "The list must receive hits on its rows and headings")
                    self.assertEqual(tree.identify_row(y + height // 2), row)
                    self.assertEqual(tree.identify_region(x + 10, 5), "heading")
                    tree.selection_set(row)
                self.assertEqual(app._selected_profile()["id"], second["id"])
                self.assertEqual(app._selected_group()["id"], group["id"])
                for _ in range(3):
                    app.refresh()
                    root.update()
                    self.assertEqual(app.profiles.selection(), (second["id"],))
                    self.assertEqual(app.groups.selection(), (group["id"],))
                root.geometry("740x620")
                root.update()
                self.assertTrue(app.profiles.bbox(second["id"]))
                self.assertTrue(app.groups.bbox(group["id"]))
                self.assertEqual(manager.config_file.read_bytes(), settings_before)
            finally:
                if app is not None:
                    app.close()
                else:
                    root.destroy()


if __name__ == "__main__":
    unittest.main()
