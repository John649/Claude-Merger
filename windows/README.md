# Claude Graft for Windows (experimental)

A Python 3.11+ / Tkinter companion for the Windows version of Claude Desktop.
The existing Swift/AppKit application remains macOS-only. This folder is an
independent Windows implementation; it does not run or rebuild the macOS binary.

**Back up your Claude data before trying this. Local-record sharing is
experimental, and is not a guarantee that current Claude Desktop will display
or resume a conversation in another account.** Recent versions can use
server-backed sidebar data. Copying local records alone may not change that
sidebar. Code transcripts, attachments, workspaces, and Cowork virtual-machine
state are not migrated. Cross-account resume needs testing with your exact
Claude version and account combination.

## What works in this implementation

- Create independent profiles, open them with Claude's `--user-data-dir` flag,
  and sign in separately. Extra profiles receive an isolated `CLAUDE_CONFIG_DIR`.
- Create, edit, or remove chat groups with two or more profiles. Main Claude is
  optional. A profile belongs to at most one group, so separate groups stay
  separate. Remove a profile from its old group before adding it to another.
- Three-way synchronization of supported local chat records, based on a stored
  baseline. One-sided changes and eligible deletions propagate; divergent
  changes remain untouched and are reported as conflicts.
- Manual merge, or opt-in merging every two minutes while the manager window is
  open. When enabled, a profile launch also tries a merge if Claude is closed.
  Automatic merging is **off by default**.
- Windows `.lnk` profile shortcuts and a command-line launcher.
- Atomic file replacement, preimage backups, a resumable merge journal, and an
  exclusive operation lock. Unregistering a profile keeps its files on disk.

The sync engine writes only matching `local_*.json` and `deleted_*` records in
initialized `claude-code-sessions` and `local-agent-mode-sessions`
account/organization folders. It does not copy sign-in credentials, browser
cookies, Chromium databases, transcripts, or updater files. Unknown or
ambiguous account/organization layouts, links/junctions/reparse points, and
malformed state fail closed rather than being guessed or reset. Cross-account
groups currently require exactly one organization per profile.

### Differences from the macOS app

This first Windows companion has no sidebar pin/sort mirroring, extension
sharing, Keychain-based usage monitoring, menu-bar app, automatic quit/reopen,
or direct Claude updater integration. It cannot install app updates. It does
not import or modify the macOS Graft configuration. Groups cannot overlap.
Removing a group does not undo already copied records.

Desktop-installer discovery and Microsoft Store/MSIX discovery are supported
by separate Windows paths, but **both require real Windows integration testing**.
MSIX profile locations and authentication callbacks are especially version-
dependent. The implementation uses read-only package discovery and the detected
package family; it does not change the registry, protocol handlers, or package
permissions. Signing in may return to the main Claude instance instead of your
selected profile. Verify the account displayed in every window before use.

## Updating an existing copy (0.1.1 profile-list fix)

Close Graft, then replace the `windows` folder in your existing extracted source
copy with the updated `windows` folder from this ZIP. Reopen `windows/launch.py`
the same way as before. Keep the same data-root option if you use one. Profiles,
groups and sign-ins live in application data, not the source folder; do not
remove or reset those folders. Source-based shortcuts keep working when the
source folder stays at the same location. A previously built EXE must be rebuilt.

Version 0.1.1 fixes profile and group lists hidden behind their container frames.
The fix changes widget ownership; it does not migrate or rewrite profile data.
The defect was reproduced visually and the fixed rows were selected on a Linux
cloud desktop. Rendered hit-testing and refresh regression checks now cover it;
Windows-native verification is still separate.

## Run from source

1. Install Claude Desktop from its official distribution and open/sign in to the
   main profile normally at least once.
2. Install [Python 3.11 or newer from python.org](https://www.python.org/downloads/windows/)
   with Tcl/Tk support. A standard Python installation is needed; the minimal
   embeddable ZIP does not include Tk. Check with `python -m tkinter`.
3. Download/clone this repository and keep the `windows` folder intact.
4. In PowerShell, from the repository root:

   ```powershell
   py -3 windows\launch.py
   ```

   Or, without a console window, invoke the `pythonw.exe` next to the Python
   interpreter you installed:

   ```powershell
   & "C:\Path\To\Python\pythonw.exe" "C:\Path\To\Claude-Merger\windows\launch.py"
   ```

No third-party Python packages are needed at runtime. Run with `python.exe` or
`py` when troubleshooting so errors remain visible in the terminal. If Tk is
missing, modify/reinstall Python with Tcl/Tk support; do not install an unrelated
`tkinter` package from PyPI.

### First use

1. Choose **New profile**, give it a name, then **Open Claude**. Sign in to the
   intended account in that window. Repeat for each additional account.
2. Create a Code chat (and a Cowork chat if you intend to share that local store)
   in each participating profile so its account and organization directories
   exist. Merely creating an empty Graft profile is insufficient.
3. Create a **New group**. Ctrl-click or Shift-click to select at least two
   members; include **Main Claude** only if you want its local records included.
4. **Quit every Claude Desktop window and the system-tray/background process.**
   Closing a visible window may leave Claude running. Graft refuses to merge
   when it detects a Claude process, and never kills or quits it for you.
5. Choose **Merge chats now**. Read any conflicts or warnings. Open a profile
   and check the correct account, sidebar visibility, and actual chat resume
   separately. See the acceptance checklist below before relying on it.
6. Enable **Merge every 2 minutes** only after testing your setup. It runs while
   this manager is open; it is not a background service or scheduled task.

Leave **Claude.exe** blank for automatic discovery. If discovery fails, browse
to the actual executable. Explicit selection validates the file but does not
prove that every Claude release supports isolated profiles. Keep the selected
installation and data root consistent, particularly when both desktop and MSIX
installs exist. If the app reports ambiguity, resolve it before merging.

**Create shortcut** lets you choose a `.lnk` destination. Source installs use the
absolute `launch.py` path plus the installed `pythonw.exe` when available;
portable builds use the actual Graft executable. A selected Claude path and
custom data root are included. Existing shortcuts are not silently overwritten;
choose a new filename. Moving/deleting the source folder, Python installation,
or portable executable requires recreating its shortcuts.

**Unregister** removes the profile registration and its group memberships, not
its data. A resulting group with fewer than two profiles is removed. Existing
shortcuts for that profile ID stop working. There is no automatic data deletion
or registration recovery UI; retain your backups.

## Command line

From the repository root:

```powershell
py -3 windows\launch.py --help
py -3 windows\launch.py --open main
py -3 windows\launch.py --open PROFILE_ID --claude "C:\Path\To\Claude.exe"
py -3 windows\launch.py --merge
```

Or, from the `windows` directory, use `python -m graft`. The same arguments work
with `ClaudeGraftWindows.exe`. `--open` and `--merge` are mutually exclusive;
no action flag opens the GUI. Profile IDs are stored in `profiles.json` and in
created shortcuts. `--merge` emits JSON counts and warnings when a console is
available; GUI-only launches show a result dialog. Exit code 1 means the
operation failed; code 2 means preserved conflicts need attention; code 0 means
no conflicts were found. Inspect warnings even after exit code 0. CLI errors are concise and do not print a traceback.

`--data-dir "C:\absolute\existing\directory"` overrides the application-data
**root**, not an individual profile directory. It is intended for development
and custom installs. It must already exist; the main profile is its `Claude`
child. Avoid pointing it at unrelated data. Both commands and shortcuts honor
this option.

## Data, backups, and recovery

For desktop installers the root is normally `%APPDATA%`. For a discovered MSIX
installation it is the actual package's `LocalCache\Roaming` directory under
`%LOCALAPPDATA%\Packages\<PackageFamilyName>`. Ambiguous installations must be
resolved explicitly. With a custom data root, all paths below are relative to
that root.

- `Claude\`: existing main profile
- `Claude-Graft-<profile-id>\`: isolated additional profile
- `ClaudeGraftWindows\profiles.json`: profile/group settings and auto-merge setting
- `ClaudeGraftWindows\mirrors.json`: synchronization baseline
- `ClaudeGraftWindows\backups\blobs\<sha256>`: immutable record contents,
  including pre-change bytes, addressed by their SHA-256 digest
- `ClaudeGraftWindows\backups\transactions\<id>.json`: each merge plan,
  containing relative destination paths plus `before` / `after` hashes
- `ClaudeGraftWindows\pending.json`: an unfinished operation, retained for safe
  retry; do not delete it to bypass a failure

Keep independent backups of complete profiles as well as Graft state. These
local merge backups cover only records Graft changes, not all Claude data. They
may contain private conversation information: keep them private. They are not
a credential backup, encrypted archive, or automatic retention service.

### Interrupted merge

Keep Claude closed and choose **Merge chats now** again. The journal checks
that each file still matches its expected pre-change or post-change hash and
resumes safely. If a file changed independently, merging stops rather than
overwriting it. Keep the error, current files, backups, and journal for review.

### Restore an individual record (advanced)

There is no one-click restore yet. Close **Claude and Graft**, and back up the
current complete profile plus `ClaudeGraftWindows` directory first. Inspect the
relevant transaction JSON in a text editor:

1. Its `path` is relative to the data root. Ensure it is the exact record you
   intend to restore and does not traverse outside that root.
2. If `before` is a digest, locate the matching file in `backups\blobs`, verify
   its SHA-256 matches, then copy those bytes to that record path. Preserve the
   record's original filename, including its extension.
3. If `before` is `null`, the merge added the file. Remove it only after verifying
   its current SHA-256 equals the transaction's `after` digest and saving a copy.
   Otherwise it may contain newer changes; do not remove it automatically.
4. Do not reopen automatic merging until the profiles, baseline, and any pending
   transaction have been reviewed. Restoring records alone can otherwise be
   interpreted as a new edit on the next merge. Prefer restoring a consistent
   full pre-merge backup of all group profiles and Graft state when available.

PowerShell's `Get-FileHash -Algorithm SHA256 -LiteralPath "..."` can verify a
file against the recorded digest. Do not guess a conflict winner or delete the
baseline/journal to suppress errors.

## Build a portable executable (Windows only)

The application itself uses only Python's standard library. The optional
builder installs pinned [PyInstaller 6.16.0](https://pyinstaller.org/en/v6.16.0/)
into `windows\.build-venv` and creates a one-file, windowed executable:

```powershell
.\windows\build.ps1
# Or select a particular Python executable:
.\windows\build.ps1 -Python "C:\Path\To\Python313\python.exe"
```

Output: `windows\dist\ClaudeGraftWindows.exe`. Build on Windows with Python
3.11+ including Tk; PyInstaller cannot cross-compile Windows executables from
Linux/macOS. Move the executable to its permanent location before creating
profile shortcuts. End users do not need a separate Python installation.

The executable is **unsigned**. Only run a build from a source you trust; this
project does not ask you to disable antivirus, bypass security warnings, or
change PowerShell security policy. The executable has no self-updater and does
not install or update Claude Desktop.

The **Windows companion** GitHub Actions workflow runs Python 3.11/3.13 tests on
Windows and Ubuntu, native Windows process/Tk smoke tests, and a Windows
portable build on matching pushes and pull requests. Its unsigned executable
is a 14-day workflow artifact, not a signed release. A manual run can also
request a portable build via its `portable_exe` input.

## Development and verification

From the repository root:

```powershell
$env:PYTHONPATH = "windows"
python -m unittest discover -s windows/tests -v
python -m compileall -q windows/graft windows/launch.py windows/tests
# Native Windows UI smoke, without opening Claude or touching existing profiles:
$env:GRAFT_TK_SMOKE = "1"
python -m unittest discover -s windows/tests -p test_interface.py -v
```

Tests use temporary files and mocked launches; native smoke tests enumerate
processes read-only and construct the Tk UI against a temporary data root.
Passing CI is not evidence that a real Claude build accepts sign-in, populates
its current sidebar from these records, or resumes cross-account sessions.

### Real Windows acceptance checklist

Use disposable test accounts/profiles and independent backups, on a supported
Windows system with Claude installed:

- Open main and two extra profiles; complete sign-in and verify the displayed
  account in each. Test the authentication callback when another profile is open.
- Confirm each new profile writes to its intended user-data directory and uses
  its isolated CLI config, without reusing another account's credentials.
- Initialize Code/Cowork stores as applicable, create a group without main,
  merge while closed, then test a group that includes main.
- Check the destination **sidebar** and then **actually resume the chat**.
  Record these as distinct results; records copied on disk are not proof of either.
- Change one supported local record in a disposable fixture, verify one-sided
  update and deletion propagation, and verify conflicting changes are preserved.
- Leave Claude running in the tray; manual and automatic merge must refuse to
  modify records. With Claude closed, verify the two-minute timer runs only
  while Graft is open and never launches or quits Claude on its own.
- Cancel every dialog, repeat create/edit/remove actions, and try rapid repeated
  merge/launch clicks. Confirm closing Graft during a merge waits for completion.
- Launch a generated shortcut from a different working directory. Test paths
  containing spaces/non-ASCII characters and both source and packaged Graft.
- Repeat installation discovery, profile isolation, sign-in, and resume checks
  separately for desktop-installer and MSIX builds; one does not validate the other.
- Test an interrupted merge and a restore on disposable fixtures before trusting
  the recovery workflow with real history.

References: [Python Tkinter](https://docs.python.org/3/library/tkinter.html),
[Electron `--user-data-dir` implementation](https://github.com/electron/electron/blob/main/shell/app/electron_main_delegate.cc),
[official Claude Windows/MSIX deployment guidance](https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows),
and Claude Windows compatibility reports
[#90188](https://github.com/anthropics/claude-code/issues/90188) /
[#78467](https://github.com/anthropics/claude-code/issues/78467).
