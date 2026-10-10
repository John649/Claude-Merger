"""Windows integration for Claude Graft, using only the Python standard library.

Filesystem discovery can be tested on any host. Native operations deliberately
raise off Windows, and process-enumeration failures never mean "Claude is closed".
"""
from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class PlatformError(RuntimeError):
    """A Windows integration operation could not be completed safely."""


class ProcessEnumerationError(PlatformError):
    """Claude's running state could not be established; do not merge profiles."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise PlatformError("This operation requires Windows.")


def _reject_reparse_ancestors(path: Path, label: str) -> None:
    """Reject links/junctions before resolving away the evidence of redirection."""
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PlatformError(f"Cannot inspect {label}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PlatformError(f"{label} cannot use a symbolic link, junction, or reparse point: {ancestor}")


def _existing_directory(value: str | Path, label: str) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("the path must be absolute")
        _reject_reparse_ancestors(path, label)
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("the path is not a directory")
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise PlatformError(f"{label} must identify an existing absolute directory: {exc}") from exc


def application_support() -> Path:
    """Return Claude's roaming data root for the registered Windows install.

    Callers append ``Claude`` or ``ClaudeGraft``. MSIX uses the current user's
    package LocalCache/Roaming directory. Legacy installs use APPDATA. A package
    inventory failure or ambiguous registration never falls back to APPDATA.
    """
    package = _registered_claude_package()
    if package is not None:
        local = _optional_environment_directory("LOCALAPPDATA")
        if local is None:
            raise PlatformError("LOCALAPPDATA is unavailable; cannot locate Claude's MSIX data.")
        package_root = _existing_directory(local / "Packages" / package.family, "Claude package data")
        roaming = package_root / "LocalCache" / "Roaming"
        try:
            _reject_reparse_ancestors(roaming, "Claude MSIX roaming data")
            if not roaming.resolve().is_relative_to(package_root):
                raise PlatformError("Claude's MSIX data directory points outside its package data root.")
            # A registered package may not have run yet. Only create beneath its
            # existing per-user package directory, never guess another root.
            roaming.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PlatformError(f"Cannot prepare Claude's MSIX roaming data: {exc}") from exc
        return _existing_directory(roaming, "Claude MSIX roaming data")
    value = os.environ.get("APPDATA")
    if not value:
        raise PlatformError("APPDATA is not set; cannot locate Windows application data.")
    return _existing_directory(value, "APPDATA")


def _validate_executable(value: str | Path) -> Path:
    try:
        path = Path(value)
        if path.suffix.casefold() != ".exe":
            raise ValueError("select an .exe file, not a script or shortcut")
        path = path.resolve(strict=True)
        if path.suffix.casefold() != ".exe" or not path.is_file():
            raise ValueError("select a regular .exe file")
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise PlatformError(f"Invalid executable path: {exc}") from exc


def _validate_claude_executable(value: str | Path) -> Path:
    path = _validate_executable(value)
    if path.name.casefold() != "claude.exe":
        raise PlatformError("Select Claude.exe without renaming it; process detection depends on that filename.")
    return path


def _optional_environment_directory(variable: str) -> Path | None:
    value = os.environ.get(variable)
    if not value:
        return None
    try:
        return _existing_directory(value, variable)
    except PlatformError:
        # An invalid discovery root must not accidentally search the CWD.
        return None


def _version_key(path: Path) -> tuple[tuple[int, ...], bool, str]:
    version = path.parent.name.removeprefix("app-")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-+]([^/\\]+))?", version)
    if match:
        return tuple(int(part) for part in match[1].split(".")), match[2] is None, version
    return (), False, version


def _install_candidates(root: Path) -> list[Path]:
    # Prefer the installer's stable launcher, then the newest version directory.
    try:
        versioned = sorted(root.glob("app-*/Claude.exe"), key=_version_key, reverse=True)
    except OSError:
        versioned = []
    return [root / "Claude.exe", *versioned]


def discover_claude(explicit: str | None = None) -> Path:
    """Find the registered MSIX or a legacy Claude .exe, or validate a custom path."""
    if explicit is not None:
        return _validate_claude_executable(explicit)

    package = _registered_claude_package()
    if package is not None:
        for relative in ("app/claude.exe", "app/Claude.exe", "Claude.exe", "claude.exe"):
            try:
                return _validate_claude_executable(package.install_location / relative)
            except PlatformError:
                continue
        raise PlatformError(
            "The registered Claude MSIX package has no accessible Claude.exe. "
            "This package layout is unsupported; do not change WindowsApps permissions."
        )
    roots: list[Path] = []
    local = _optional_environment_directory("LOCALAPPDATA")
    if local is not None:
        roots.extend((local / "AnthropicClaude", local / "Programs" / "Claude"))
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = _optional_environment_directory(variable)
        if base is not None:
            roots.extend((base / "Claude", base / "AnthropicClaude", base / "Anthropic" / "Claude"))
    for root in dict.fromkeys(roots):
        for candidate in _install_candidates(root):
            try:
                return _validate_claude_executable(candidate)
            except PlatformError:
                continue
    raise PlatformError(
        "Claude.exe was not found. Select the .exe from your Claude desktop installation. "
        "Microsoft Store/MSIX requires a current-user package registered with the name Claude; "
        "other package layouts are unsupported."
    )


# Fixed-width Windows types matter even when this module is imported on Unix.
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _kernel32():
    _require_windows()
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _process_error(operation: str, error: int) -> ProcessEnumerationError:
    return ProcessEnumerationError(
        f"{operation} failed (Windows error {error}); cannot confirm Claude is closed. "
        "Do not merge profiles until process detection succeeds."
    )


def running_claudes() -> list[int]:
    """Return all Claude.exe PIDs, conservatively including other user sessions.

    Uses a Toolhelp32 snapshot without opening or terminating processes. Every
    failure raises instead of returning an unsafe empty or partial process list.
    The result is a snapshot, not a lock preventing a subsequent Claude launch.
    """
    _require_windows()
    try:
        kernel = _kernel32()
        kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel.Process32FirstW.restype = ctypes.c_int32
        kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel.Process32NextW.restype = ctypes.c_int32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int32
        snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (None, 0, _INVALID_HANDLE_VALUE):
            raise _process_error("CreateToolhelp32Snapshot", ctypes.get_last_error())
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            pids: set[int] = set()
            has_entry = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_entry:
                if entry.szExeFile.casefold() == "claude.exe":
                    pids.add(int(entry.th32ProcessID))
                has_entry = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            error = ctypes.get_last_error()
            if error != _ERROR_NO_MORE_FILES:
                raise _process_error("Process enumeration", error)
            return sorted(pids)
        finally:
            if not kernel.CloseHandle(snapshot):
                raise _process_error("CloseHandle", ctypes.get_last_error())
    except OSError as exc:
        raise ProcessEnumerationError(
            f"Windows process detection failed; cannot confirm Claude is closed: {exc}"
        ) from exc


def launch_claude(executable: Path, profile: Path) -> subprocess.Popen:
    """Launch an existing profile with a literal argument list, never a shell."""
    _require_windows()
    executable = _validate_claude_executable(executable)
    profile = _existing_directory(profile, "Claude profile")
    environment = os.environ.copy()
    if profile.name.casefold() != "claude":
        config = profile / ".claude-code"
        _reject_reparse_ancestors(config, "Claude Code profile config")
        if config.is_symlink() or (config.exists() and not config.is_dir()):
            raise PlatformError("The profile's Claude Code config must be its own directory.")
        if not config.resolve().is_relative_to(profile):
            raise PlatformError("The profile's Claude Code config points outside the profile.")
        environment["CLAUDE_CONFIG_DIR"] = str(config)
    return subprocess.Popen(
        [str(executable), f"--user-data-dir={profile}"],
        shell=False,
        cwd=str(executable.parent),
        close_fds=True,
        env=environment,
    )


def _powershell_executable() -> Path:
    # Resolve the OS-supplied executable, not a command from PATH or the CWD.
    kernel = _kernel32()
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise PlatformError("Cannot locate the Windows system directory for shortcut creation.")
    return _validate_executable(Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe")



@dataclass(frozen=True)
class _ClaudePackage:
    install_location: Path
    family: str


_PACKAGE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$packages = @(Get-AppxPackage -Name Claude -ErrorAction Stop |
    Select-Object -Property InstallLocation, PackageFamilyName)
ConvertTo-Json -InputObject $packages -Compress
"""


def _registered_claude_package() -> _ClaudePackage | None:
    """Query only this user's package metadata; do not execute package content."""
    if sys.platform != "win32":
        return None
    script = base64.b64encode(_PACKAGE_SCRIPT.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [str(_powershell_executable()), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
            encoding="utf-8", capture_output=True, check=True, timeout=30,
            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = json.loads(result.stdout.lstrip("\ufeff"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise PlatformError(
            "Cannot inspect Claude's Windows package registration; refusing to guess its data directory. "
            f"{exc}"
        ) from exc
    if not isinstance(rows, list):
        raise PlatformError("Claude package inventory returned an unexpected result; cannot choose a data root.")
    if not rows:
        return None
    if len(rows) != 1:
        raise PlatformError("Multiple Claude packages are registered; cannot safely choose a profile data root.")
    row = rows[0]
    if not isinstance(row, dict):
        raise PlatformError("Claude package inventory returned invalid metadata.")
    family = row.get("PackageFamilyName")
    location = row.get("InstallLocation")
    if not isinstance(family, str) or not re.fullmatch(r"Claude_[A-Za-z0-9]+", family, re.IGNORECASE):
        raise PlatformError("Claude package family is unrecognized; cannot choose its profile data root.")
    if not isinstance(location, str) or not location:
        raise PlatformError("Claude package install location is missing.")
    return _ClaudePackage(_existing_directory(location, "Claude package installation"), family)


def _shortcut_filename(name: str) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise PlatformError("Enter a non-empty Windows shortcut name.")
    if name[-1] in " ." or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name):
        raise PlatformError("Shortcut names cannot contain Windows path separators or reserved characters.")
    stem = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
    if stem in reserved or len(name) > 240:
        raise PlatformError("Choose a shorter, non-reserved Windows shortcut name.")
    return name if name.casefold().endswith(".lnk") else f"{name}.lnk"


# This script is deliberately constant. All user-controlled strings enter as
# JSON data on stdin, never as executable PowerShell source or shell arguments.
_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$payload = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut([string]$payload.path)
$shortcut.TargetPath = [string]$payload.target
$shortcut.Arguments = [string]$payload.arguments
$shortcut.WorkingDirectory = [string]$payload.working_directory
$shortcut.Description = [string]$payload.name
$shortcut.Save()
"""


def create_shortcut(name: str, target: Path, arguments: list[str], destination: Path) -> Path:
    """Create a new .lnk without elevation, overwriting files, or executing target.

    ``destination`` is an existing folder or a full .lnk filename whose parent
    exists. ``arguments`` are quoted for Windows by ``subprocess.list2cmdline``.
    No profile names, paths, or arguments are interpolated into PowerShell code.
    """
    _require_windows()
    filename = _shortcut_filename(name)
    target = _validate_executable(target)
    if not isinstance(arguments, list) or not all(isinstance(arg, str) and "\0" not in arg for arg in arguments):
        raise PlatformError("Shortcut arguments must be a list of strings without NUL characters.")
    destination = Path(destination)
    if not destination.is_absolute():
        raise PlatformError("The shortcut destination must be an absolute path.")
    if destination.is_dir():
        shortcut = _existing_directory(destination, "Shortcut destination") / filename
    else:
        if destination.suffix.casefold() != ".lnk":
            raise PlatformError("Choose a .lnk filename or an existing destination directory.")
        _shortcut_filename(destination.name)
        shortcut = _existing_directory(destination.parent, "Shortcut destination") / destination.name
    if shortcut.exists() or shortcut.is_symlink():
        raise FileExistsError(f"A shortcut or file already exists at {shortcut}; choose another name.")
    payload = {
        "path": str(shortcut),
        "target": str(target),
        "arguments": subprocess.list2cmdline(arguments),
        "working_directory": str(target.parent),
        "name": name,
    }
    encoded_script = base64.b64encode(_SHORTCUT_SCRIPT.encode("utf-16le")).decode("ascii")
    try:
        subprocess.run(
            [str(_powershell_executable()), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            input=json.dumps(payload, ensure_ascii=True),
            encoding="utf-8",
            capture_output=True,
            check=True,
            timeout=30,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlatformError(f"Could not create the Windows shortcut: {exc}") from exc
    if not shortcut.is_file() or shortcut.stat().st_size == 0:
        raise PlatformError("Windows did not create the requested shortcut.")
    return shortcut
