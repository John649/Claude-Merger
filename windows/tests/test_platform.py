"""Synthetic filesystem and mocked Windows API tests; never launch Claude."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from graft import platform as win


class FilesystemCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        package = patch.object(win, "_registered_claude_package", return_value=None)
        self.package = package.start()
        self.addCleanup(package.stop)

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a real executable")
        return path


class ApplicationSupportTests(FilesystemCase):
    def test_appdata_root_is_not_graft_subdirectory(self):
        os.environ["APPDATA"] = str(self.root)
        self.assertEqual(win.application_support(), self.root)

    def test_missing_or_empty_appdata_fails(self):
        for value in (None, ""):
            with self.subTest(value=value):
                if value is not None:
                    os.environ["APPDATA"] = value
                with self.assertRaisesRegex(win.PlatformError, "APPDATA"):
                    win.application_support()

    def test_invalid_appdata_does_not_fall_back_or_create(self):
        for value in ("relative", str(self.root / "missing"), str(self.file("file"))):
            with self.subTest(value=value):
                os.environ["APPDATA"] = value
                with self.assertRaises(win.PlatformError):
                    win.application_support()
        self.assertFalse((self.root / "missing").exists())

    def test_msix_root_wins_over_legacy_and_is_created_only_under_package(self):
        family = "Claude_testpublisher"
        package_root = self.root / "Packages" / family
        package_root.mkdir(parents=True)
        os.environ.update(LOCALAPPDATA=str(self.root), APPDATA=str(self.root))
        self.package.return_value = win._ClaudePackage(self.root, family)
        result = win.application_support()
        self.assertEqual(result, package_root / "LocalCache" / "Roaming")
        self.assertTrue(result.is_dir())

    def test_msix_missing_package_data_fails_without_creating_it(self):
        os.environ.update(LOCALAPPDATA=str(self.root), APPDATA=str(self.root))
        self.package.return_value = win._ClaudePackage(self.root, "Claude_missing")
        with self.assertRaisesRegex(win.PlatformError, "package data"):
            win.application_support()
        self.assertFalse((self.root / "Packages").exists())

    def test_msix_missing_localappdata_does_not_use_appdata(self):
        os.environ["APPDATA"] = str(self.root)
        self.package.return_value = win._ClaudePackage(self.root, "Claude_testpublisher")
        with self.assertRaisesRegex(win.PlatformError, "LOCALAPPDATA"):
            win.application_support()

    def test_inventory_error_cannot_fall_back_to_legacy(self):
        os.environ["APPDATA"] = str(self.root)
        self.package.side_effect = win.PlatformError("Cannot inspect packages")
        with self.assertRaisesRegex(win.PlatformError, "Cannot inspect"):
            win.application_support()

    def test_msix_roaming_symlink_cannot_escape_package(self):
        package_root = self.root / "Packages" / "Claude_test"
        package_root.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (package_root / "LocalCache").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is unavailable")
        os.environ["LOCALAPPDATA"] = str(self.root)
        self.package.return_value = win._ClaudePackage(self.root, "Claude_test")
        with self.assertRaisesRegex(win.PlatformError, "reparse point"):
            win.application_support()
        self.assertFalse((outside / "Roaming").exists())


class DiscoveryTests(FilesystemCase):
    def test_explicit_exe_can_have_spaces_quotes_and_uppercase_extension(self):
        executable = self.file("custom ' install/Claude.EXE")
        self.assertEqual(win.discover_claude(str(executable)), executable)
        self.package.assert_not_called()

    def test_invalid_explicit_path_never_falls_back(self):
        os.environ["LOCALAPPDATA"] = str(self.root)
        self.file("AnthropicClaude/Claude.exe")
        directory = self.root / "directory.exe"
        directory.mkdir()
        for path in ("", self.file("renamed.exe"), self.file("evil.cmd"), self.file("evil.bat"), self.file("shortcut.lnk"), directory, self.root / "absent.exe"):
            with self.subTest(path=path), self.assertRaises(win.PlatformError):
                win.discover_claude(str(path))

    def test_prefers_stable_installer_launcher(self):
        os.environ["LOCALAPPDATA"] = str(self.root)
        self.file("AnthropicClaude/app-9.9.9/Claude.exe")
        expected = self.file("AnthropicClaude/Claude.exe")
        self.assertEqual(win.discover_claude(), expected)

    def test_versioned_install_uses_numeric_order(self):
        os.environ["LOCALAPPDATA"] = str(self.root)
        self.file("AnthropicClaude/app-1.9.0/Claude.exe")
        self.file("AnthropicClaude/app-1.10.0-beta/Claude.exe")
        expected = self.file("AnthropicClaude/app-1.10.0/Claude.exe")
        self.assertEqual(win.discover_claude(), expected)

    def test_programs_install(self):
        os.environ["LOCALAPPDATA"] = str(self.root)
        expected = self.file("Programs/Claude/Claude.exe")
        self.assertEqual(win.discover_claude(), expected)

    def test_program_files_variants(self):
        expected = self.file("Claude/Claude.exe")
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(self.root)}, clear=True):
                self.assertEqual(win.discover_claude(), expected)

    def test_bad_environment_does_not_search_relative_paths(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": ".", "ProgramFiles": "relative"}):
            with self.assertRaisesRegex(win.PlatformError, "Microsoft Store/MSIX"):
                win.discover_claude()

    def test_registered_msix_preferred_over_legacy(self):
        os.environ["LOCALAPPDATA"] = str(self.root)
        self.file("AnthropicClaude/Claude.exe")
        expected = self.file("package/app/claude.exe")
        self.package.return_value = win._ClaudePackage(self.root / "package", "Claude_test")
        self.assertEqual(win.discover_claude(), expected)

    def test_unknown_msix_layout_does_not_launch_legacy(self):
        os.environ["LOCALAPPDATA"] = str(self.root)
        self.file("AnthropicClaude/Claude.exe")
        self.package.return_value = win._ClaudePackage(self.root, "Claude_test")
        with self.assertRaisesRegex(win.PlatformError, "unsupported"):
            win.discover_claude()

    def test_discovery_does_not_allow_symlink_to_renamed_exe(self):
        renamed = self.file("renamed.exe")
        link = self.root / "AnthropicClaude" / "Claude.exe"
        link.parent.mkdir()
        try:
            link.symlink_to(renamed)
        except OSError:
            self.skipTest("Creating file symlinks is unavailable")
        os.environ["LOCALAPPDATA"] = str(self.root)
        with self.assertRaises(win.PlatformError):
            win.discover_claude()

    def test_exe_symlink_to_script_is_rejected(self):
        script = self.file("not-an-exe.cmd")
        link = self.root / "Claude.exe"
        try:
            link.symlink_to(script)
        except OSError:
            self.skipTest("Creating file symlinks is unavailable")
        with self.assertRaises(win.PlatformError):
            win.discover_claude(str(link))


class PackageInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        platform = patch.object(sys, "platform", "win32")
        platform.start()
        self.addCleanup(platform.stop)
        powershell = patch.object(win, "_powershell_executable", return_value=self.root / "powershell.exe")
        powershell.start()
        self.addCleanup(powershell.stop)
        process = patch.object(win.subprocess, "run")
        self.run = process.start()
        self.addCleanup(process.stop)

    def result(self, rows):
        self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps(rows), "")

    def test_empty_current_user_inventory(self):
        self.result([])
        self.assertIsNone(win._registered_claude_package())
        args, kwargs = self.run.call_args
        command = args[0]
        script = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertIn("Get-AppxPackage -Name Claude -ErrorAction Stop", script)
        self.assertNotIn("-AllUsers", script)
        self.assertNotIn("-ExecutionPolicy", command)
        self.assertIn("-NoProfile", command)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 30)

    def test_one_package_validates_metadata(self):
        self.result([{"InstallLocation": str(self.root), "PackageFamilyName": "Claude_abcd123"}])
        self.assertEqual(win._registered_claude_package(), win._ClaudePackage(self.root, "Claude_abcd123"))

    def test_ambiguous_packages_fail_closed(self):
        self.result([{}, {}])
        with self.assertRaisesRegex(win.PlatformError, "Multiple"):
            win._registered_claude_package()

    def test_malformed_inventory_fails_closed(self):
        for rows in ({}, None, [None], [{}], [{"InstallLocation": str(self.root), "PackageFamilyName": "../../other"}], [{"InstallLocation": "relative", "PackageFamilyName": "Claude_test"}]):
            with self.subTest(rows=rows):
                self.result(rows)
                with self.assertRaises(win.PlatformError):
                    win._registered_claude_package()
        self.run.return_value.stdout = "not json"
        with self.assertRaises(win.PlatformError):
            win._registered_claude_package()

    def test_command_error_and_timeout_fail_closed(self):
        for error in (OSError("unavailable"), subprocess.CalledProcessError(1, ["powershell.exe"]), subprocess.TimeoutExpired("powershell.exe", 30)):
            with self.subTest(error=error):
                self.run.side_effect = error
                with self.assertRaisesRegex(win.PlatformError, "refusing to guess"):
                    win._registered_claude_package()


class ProcessEnumerationTests(unittest.TestCase):
    def setUp(self):
        requirement = patch.object(win, "_require_windows")
        requirement.start()
        self.addCleanup(requirement.stop)
        self.kernel = Mock()
        self.kernel.CreateToolhelp32Snapshot.return_value = 123
        self.kernel.CloseHandle.return_value = 1
        library = patch.object(win, "_kernel32", return_value=self.kernel)
        self.library = library.start()
        self.addCleanup(library.stop)
        error = patch.object(ctypes, "get_last_error", return_value=win._ERROR_NO_MORE_FILES, create=True)
        self.last_error = error.start()
        self.addCleanup(error.stop)

    def processes(self, entries):
        iterator = iter(entries)

        def next_entry(snapshot, pointer):
            self.assertEqual(snapshot, 123)
            entry = ctypes.cast(pointer, ctypes.POINTER(win._PROCESSENTRY32W)).contents
            self.assertEqual(entry.dwSize, ctypes.sizeof(win._PROCESSENTRY32W))
            try:
                pid, executable = next(iterator)
            except StopIteration:
                return 0
            entry.th32ProcessID = pid
            entry.szExeFile = executable
            return 1

        self.kernel.Process32FirstW.side_effect = next_entry
        self.kernel.Process32NextW.side_effect = next_entry

    def test_exact_case_insensitive_name_sorted_deduplicated(self):
        self.processes([(9, "Claude.exe"), (1, "Claude-Other.exe"), (2, "CLAUDE.EXE"), (9, "claude.exe"), (3, "python.exe")])
        self.assertEqual(win.running_claudes(), [2, 9])
        self.kernel.CreateToolhelp32Snapshot.assert_called_once_with(win._TH32CS_SNAPPROCESS, 0)
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_empty_snapshot_is_valid_only_at_no_more_files(self):
        self.processes([])
        self.assertEqual(win.running_claudes(), [])
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_invalid_snapshot_never_returns_empty(self):
        for handle in (None, 0, win._INVALID_HANDLE_VALUE):
            with self.subTest(handle=handle):
                self.kernel.CreateToolhelp32Snapshot.return_value = handle
                self.last_error.return_value = 5
                with self.assertRaisesRegex(win.ProcessEnumerationError, "cannot confirm"):
                    win.running_claudes()
        self.kernel.CloseHandle.assert_not_called()

    def test_first_failure_closes_snapshot_and_raises(self):
        self.processes([])
        self.last_error.return_value = 5
        with self.assertRaises(win.ProcessEnumerationError):
            win.running_claudes()
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_partial_enumeration_never_returns_partial_list(self):
        self.processes([(42, "claude.exe")])
        self.last_error.return_value = 299
        with self.assertRaisesRegex(win.ProcessEnumerationError, "299"):
            win.running_claudes()
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_close_failure_is_not_reported_as_success(self):
        self.processes([])
        self.kernel.CloseHandle.return_value = 0
        with self.assertRaisesRegex(win.ProcessEnumerationError, "CloseHandle"):
            win.running_claudes()

    def test_loading_native_api_failure_is_clear(self):
        self.library.side_effect = OSError("library not available")
        with self.assertRaisesRegex(win.ProcessEnumerationError, "cannot confirm"):
            win.running_claudes()

    def test_native_api_exception_still_closes_snapshot(self):
        self.kernel.Process32FirstW.side_effect = OSError("failed")
        with self.assertRaises(win.ProcessEnumerationError):
            win.running_claudes()
        self.kernel.CloseHandle.assert_called_once_with(123)


class LaunchTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        requirement = patch.object(win, "_require_windows")
        requirement.start()
        self.addCleanup(requirement.stop)
        process = patch.object(win.subprocess, "Popen")
        self.popen = process.start()
        self.addCleanup(process.stop)
        self.exe = self.file("install/Claude.exe")
        self.profile = self.root / "Claude-work & account"
        self.profile.mkdir()

    def test_literal_argument_list_and_isolated_child_environment(self):
        os.environ.update(CLAUDE_CONFIG_DIR="original", PRESERVED="yes")
        self.assertIs(win.launch_claude(self.exe, self.profile), self.popen.return_value)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [str(self.exe), f"--user-data-dir={self.profile}"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(self.exe.parent))
        self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], str(self.profile / ".claude-code"))
        self.assertEqual(kwargs["env"]["PRESERVED"], "yes")
        self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "original")
        self.assertFalse((self.profile / ".claude-code").exists())

    def test_main_profile_preserves_default_environment(self):
        profile = self.root / "Claude"
        profile.mkdir()
        win.launch_claude(self.exe, profile)
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.popen.call_args.kwargs["env"])

    def test_invalid_executable_or_profile_never_launches(self):
        for exe, profile in ((self.file("renamed.exe"), self.profile), (self.file("evil.cmd"), self.profile), (self.exe, self.root / "missing"), (self.exe, self.file("profile-file"))):
            with self.subTest(exe=exe, profile=profile), self.assertRaises(win.PlatformError):
                win.launch_claude(exe, profile)
        self.popen.assert_not_called()

    def test_config_symlink_is_rejected_even_inside_profile(self):
        inside = self.profile / "inside"
        inside.mkdir()
        try:
            (self.profile / ".claude-code").symlink_to(inside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is unavailable")
        with self.assertRaisesRegex(win.PlatformError, "reparse point"):
            win.launch_claude(self.exe, self.profile)
        self.popen.assert_not_called()

    def test_config_file_is_rejected(self):
        (self.profile / ".claude-code").write_text("not a directory")
        with self.assertRaises(win.PlatformError):
            win.launch_claude(self.exe, self.profile)
        self.popen.assert_not_called()


class ShortcutTests(FilesystemCase):
    def setUp(self):
        super().setUp()
        requirement = patch.object(win, "_require_windows")
        requirement.start()
        self.addCleanup(requirement.stop)
        powershell = patch.object(win, "_powershell_executable", return_value=self.root / "system" / "powershell.exe")
        powershell.start()
        self.addCleanup(powershell.stop)
        process = patch.object(win.subprocess, "run")
        self.run = process.start()
        self.addCleanup(process.stop)
        self.target = self.file("install ' & $/pythonw.exe")

        def fake_save(*args, **kwargs):
            payload = json.loads(kwargs["input"])
            Path(payload["path"]).write_bytes(b"synthetic shortcut")
            return subprocess.CompletedProcess(args[0], 0, "", "")

        self.run.side_effect = fake_save

    def test_shortcut_data_never_becomes_script_source(self):
        name = "Claude - O'Brien & $(Write-Output 9) 日本"
        arguments = ["-m", "graft", "--profile", "O'Brien & $(Write-Output 9)", 'a "quote"', "", "C:\\ends with slash\\"]
        result = win.create_shortcut(name, self.target, arguments, self.root)
        self.assertEqual(result, self.root / f"{name}.lnk")
        args, kwargs = self.run.call_args
        script = base64.b64decode(args[0][-1]).decode("utf-16le")
        self.assertEqual(script, win._SHORTCUT_SCRIPT)
        self.assertNotIn(name, script)
        self.assertNotIn(str(self.target), script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertNotIn("-ExecutionPolicy", args[0])
        payload = json.loads(kwargs["input"])
        self.assertEqual(payload["target"], str(self.target))
        self.assertEqual(payload["arguments"], subprocess.list2cmdline(arguments))
        self.assertEqual(payload["name"], name)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 30)

    def test_full_filename_is_authoritative(self):
        target = self.root / "custom.LNK"
        self.assertEqual(win.create_shortcut("Description", self.target, [], target), target)

    def test_lnk_suffix_is_not_duplicated(self):
        self.assertEqual(win.create_shortcut("Work.lnk", self.target, [], self.root), self.root / "Work.lnk")

    def test_existing_file_is_not_overwritten(self):
        existing = self.file("Work.lnk")
        original = existing.read_bytes()
        with self.assertRaises(FileExistsError):
            win.create_shortcut("Work", self.target, [], self.root)
        self.assertEqual(existing.read_bytes(), original)
        self.run.assert_not_called()

    def test_invalid_windows_names_rejected(self):
        for name in ("", ".", "..", "../escape", "a\\b", "C:evil", "a|b", "a\nline", "a\0nul", "trailing.", "trailing ", "CON", "con.lnk", "LPT9", "COM²", "NUL.txt", "a" * 241):
            with self.subTest(name=name), self.assertRaises(win.PlatformError):
                win.create_shortcut(name, self.target, [], self.root)
        self.run.assert_not_called()

    def test_invalid_destination_and_arguments_rejected(self):
        for arguments, destination in (("--bad", self.root), (["bad\0arg"], self.root), ([1], self.root), ([], Path("relative.lnk")), ([], self.root / "unknown"), ([], self.root / "missing" / "Work.lnk"), ([], self.root / "CON.lnk")):
            with self.subTest(arguments=arguments, destination=destination), self.assertRaises(win.PlatformError):
                win.create_shortcut("Work", self.target, arguments, destination)
        self.run.assert_not_called()

    def test_failed_command_or_missing_output_is_not_success(self):
        for failure in (OSError("blocked"), subprocess.CalledProcessError(1, ["powershell"]), subprocess.TimeoutExpired("powershell", 30)):
            with self.subTest(failure=failure):
                self.run.side_effect = failure
                with self.assertRaises(win.PlatformError):
                    win.create_shortcut("Work", self.target, [], self.root)
        self.run.side_effect = None
        with self.assertRaisesRegex(win.PlatformError, "did not create"):
            win.create_shortcut("Work", self.target, [], self.root)


class NativeBoundaryTests(unittest.TestCase):
    def test_windows_reparse_attribute_is_rejected(self):
        path = Path("/synthetic/path")
        info = Mock(st_mode=0o040700, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=info):
            with self.assertRaisesRegex(win.PlatformError, "reparse point"):
                win._reject_reparse_ancestors(path, "Synthetic directory")

    def test_native_functions_fail_clearly_off_windows(self):
        with patch.object(sys, "platform", "linux"):
            for function, arguments in ((win.running_claudes, ()), (win.launch_claude, (Path("x.exe"), Path("p"))), (win.create_shortcut, ("Work", Path("x.exe"), [], Path(".")))):
                with self.subTest(function=function.__name__), self.assertRaisesRegex(win.PlatformError, "requires Windows"):
                    function(*arguments)
            self.assertIsNone(win._registered_claude_package())

    @unittest.skipUnless(sys.platform == "win32", "Windows ABI assertion")
    def test_process_entry_windows_abi(self):
        expected = 568 if ctypes.sizeof(ctypes.c_void_p) == 8 else 556
        self.assertEqual(ctypes.sizeof(win._PROCESSENTRY32W), expected)


if __name__ == "__main__":
    unittest.main()
