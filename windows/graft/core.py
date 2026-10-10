"""Conservative, journaled chat-record sharing for the Windows companion.

Only local_*.json and deleted_* records in known account/org stores are written.
Credentials, Chromium databases, transcripts and VM data are never copied.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from . import platform

STORES = ("claude-code-sessions", "local-agent-mode-sessions")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,127}\Z")
RECORD = re.compile(r"(?:local_[A-Za-z0-9-]+\.json|deleted_[A-Za-z0-9-]+(?:\.json)?)\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")


class GraftError(RuntimeError):
    """An actionable error, safe to show in the UI."""


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise GraftError("Use a name between 1 and 100 characters.")
    if any(ord(c) < 32 for c in value):
        raise GraftError("Names cannot contain control characters.")
    return value.strip()


def _hash(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _json(path: Path, default=None):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return deepcopy(default)
    except (ValueError, UnicodeError) as exc:
        raise GraftError(f"Unreadable JSON in {path}. Nothing was reset; restore a valid file.") from exc


def _atomic(path: Path, data: bytes):
    """Same-volume replace. A Windows sharing violation leaves the old file intact."""
    fd, temp = tempfile.mkstemp(prefix=".graft-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _save(path: Path, value):
    _atomic(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


class Manager:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else platform.application_support()
        if not self.root.is_absolute() or not self.root.is_dir():
            raise GraftError("The application-data directory must exist and be absolute.")
        self.state_dir = self.root / "ClaudeGraftWindows"
        self._safe(self.state_dir)
        self.state_dir.mkdir(exist_ok=True)
        self.config_file = self.state_dir / "profiles.json"
        self.state_file = self.state_dir / "mirrors.json"
        self.pending_file = self.state_dir / "pending.json"
        self.backup_dir = self.state_dir / "backups"
        self._load_config()

    def _safe(self, path: Path) -> Path:
        """Reject symlinks, junctions, reparse points, and lexical escapes."""
        try:
            parts = path.relative_to(self.root).parts
        except ValueError as exc:
            raise GraftError("Path is outside the application-data directory.") from exc
        if any(p in (".", "..") or ":" in p or "\\" in p for p in parts):
            raise GraftError("Unsafe path in Graft state.")
        current = self.root
        for part in (None, *parts):
            if part is not None:
                current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise GraftError(f"Links and Windows reparse points are not supported: {current}")
        return path

    @contextmanager
    def operation_lock(self):
        path = self._safe(self.state_dir / "operation.lock")
        with path.open("a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise GraftError("Another Graft operation is in progress. Try again when it finishes.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_config(self):
        config = _json(self._safe(self.config_file), {"version": 1, "profiles": [], "groups": [], "autoMerge": False})
        try:
            if not (isinstance(config, dict) and config["version"] == 1):
                raise ValueError("invalid configuration")
            if not (isinstance(config["profiles"], list) and isinstance(config["groups"], list)):
                raise ValueError("invalid configuration")
            if not (isinstance(config.get("autoMerge", False), bool)):
                raise ValueError("invalid configuration")
            for key in ("mainFolder", "profileBase", "mainTranscriptBase"):
                value = config.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value or "\\" in value:
                        raise ValueError("invalid profile location")
                    parts = value.split("/")
                    if any(not part or part in (".", "..") or ":" in part for part in parts):
                        raise ValueError("invalid profile location")
                    self._safe(self.root.joinpath(*parts))
            ids, folders = {"main"}, {"claude", "claudegraftwindows"}
            for profile in config["profiles"]:
                if not (str(uuid.UUID(profile["id"])) == profile["id"]):
                    raise ValueError("invalid configuration")
                if not (profile["id"] not in ids):
                    raise ValueError("invalid configuration")
                ids.add(profile["id"])
                _name(profile["name"])
                if not (profile["folder"] == "Claude-Graft-" + profile["id"]):
                    raise ValueError("invalid configuration")
                if not (profile["folder"].casefold() not in folders):
                    raise ValueError("invalid configuration")
                folders.add(profile["folder"].casefold())
            grouped, group_ids, names = set(), set(), set()
            for group in config["groups"]:
                if not (str(uuid.UUID(group["id"])) == group["id"] and group["id"] not in group_ids):
                    raise ValueError("invalid configuration")
                group_ids.add(group["id"])
                name = _name(group["name"]).casefold()
                if not (name not in names):
                    raise ValueError("invalid configuration")
                names.add(name)
                if not (isinstance(group["members"], list) and len(group["members"]) >= 2):
                    raise ValueError("invalid configuration")
                members = set(group["members"])
                if not (len(members) == len(group["members"]) and members <= ids):
                    raise ValueError("invalid configuration")
                if not (not (members & grouped)):
                    raise ValueError("invalid configuration")
                grouped |= members
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            raise GraftError("Invalid profiles.json. Nothing was reset; restore a valid backup.") from exc
        return config

    def profiles(self):
        return self._load_config()["profiles"]

    def groups(self):
        return self._load_config()["groups"]

    def auto_merge(self):
        return self._load_config().get("autoMerge", False)

    def _not_pending(self):
        if self._safe(self.pending_file).exists():
            raise GraftError("A previous merge is unfinished. Close Claude and use Merge before changing profiles.")

    def set_auto_merge(self, enabled):
        with self.operation_lock():
            config = self._load_config()
            config["autoMerge"] = bool(enabled)
            _save(self.config_file, config)

    def add_profile(self, name):
        name = _name(name)
        with self.operation_lock():
            self._not_pending()
            config = self._load_config()
            if name.casefold() == "main claude" or any(p["name"].casefold() == name.casefold() for p in config["profiles"]):
                raise GraftError("A profile already has that name.")
            identity = str(uuid.uuid4())
            profile = {"id": identity, "name": name, "folder": "Claude-Graft-" + identity}
            self._safe(self.root / config.get("profileBase", "") / profile["folder"]).mkdir()
            config["profiles"].append(profile)
            _save(self.config_file, config)
            return profile

    def profile_path(self, identity):
        if identity == "main":
            return self._safe(self.root / self._load_config().get("mainFolder", "Claude"))
        for profile in self.profiles():
            if profile["id"] == identity:
                return self._safe(self.root / self._load_config().get("profileBase", "") / profile["folder"])
        raise GraftError("Unknown profile. Refresh the profile list.")

    def set_group(self, name, members):
        name = _name(name)
        members = list(dict.fromkeys(members))
        with self.operation_lock():
            self._not_pending()
            config = self._load_config()
            if len(members) < 2 or not set(members) <= ({"main"} | {p["id"] for p in config["profiles"]}):
                raise GraftError("Select at least two existing profiles for the group.")
            existing = next((g for g in config["groups"] if g["name"].casefold() == name.casefold()), None)
            for group in config["groups"]:
                if group is not existing and set(group["members"]) & set(members):
                    raise GraftError("A profile can belong to only one chat group. Remove it from its old group first.")
            group = {"id": existing["id"] if existing else str(uuid.uuid4()), "name": name, "members": members}
            config["groups"] = [g for g in config["groups"] if g is not existing] + [group]
            _save(self.config_file, config)
            return group

    def remove_group(self, identity):
        with self.operation_lock():
            self._not_pending()
            config = self._load_config()
            config["groups"] = [g for g in config["groups"] if g["id"] != identity]
            _save(self.config_file, config)

    def remove_profile(self, identity):
        if identity == "main":
            raise GraftError("The main Claude profile cannot be removed.")
        with self.operation_lock():
            self._not_pending()
            config = self._load_config()
            config["profiles"] = [p for p in config["profiles"] if p["id"] != identity]
            for group in config["groups"]:
                group["members"] = [p for p in group["members"] if p != identity]
            config["groups"] = [g for g in config["groups"] if len(g["members"]) > 1]
            _save(self.config_file, config)
        # Deliberately retain all profile data and backups on disk.

    def _closed(self):
        if platform.running_claudes():
            raise GraftError("Quit every Claude Desktop window and its system-tray process before merging.")

    def open_profile(self, identity, executable=None):
        with self.operation_lock():
            profile = self.profile_path(identity)
            if not profile.is_dir():
                raise GraftError("Profile folder is missing. Open the main Claude normally first, or create a new profile.")
            app = platform.discover_claude(executable)
            if self.auto_merge() and not platform.running_claudes():
                result = self._merge()
                if result["conflicts"] or result["warnings"]:
                    raise GraftError("Merge needs attention. Open Graft and use Merge to review warnings before launching.")
            self._not_pending()
            platform.launch_claude(app, profile)

    def _directories(self, folder):
        if not self._safe(folder).is_dir():
            raise GraftError(f"Expected an existing directory: {folder}")
        result = []
        for item in folder.iterdir():
            self._safe(item)
            if item.is_dir() and TOKEN.fullmatch(item.name):
                result.append(item)
        return sorted(result)

    def _counterpart(self, parent, token):
        candidates = [p for p in self._directories(parent) if p.name.casefold() == token.casefold()]
        if not candidates:
            candidates = [p for p in self._directories(parent)
                          if p.name.casefold().startswith(token.casefold()) or token.casefold().startswith(p.name.casefold())]
        if len(candidates) != 1:
            raise GraftError(f"Missing or ambiguous account/organization folder in {parent}. Sign in and create a Code chat first.")
        return candidates[0]

    def _group_folders(self, group):
        profiles, accounts = [], []
        for identity in group["members"]:
            profile = self.profile_path(identity)
            if not profile.is_dir():
                raise GraftError(f"Profile folder is missing: {profile}")
            config = _json(self._safe(profile / "config.json"))
            account = config.get("lastKnownAccountUuid") if isinstance(config, dict) else None
            if not isinstance(account, str) or not TOKEN.fullmatch(account):
                raise GraftError(f"Sign in to {profile.name} and create a Code chat before merging; account identity is unavailable.")
            profiles.append(profile)
            accounts.append(account)
        result = []
        for store in STORES:
            present = [(p / store).exists() for p in profiles]
            if not any(present):
                continue
            if not all(present):
                raise GraftError(f"{store} is not initialized in every group member. Create a chat in each profile first.")
            roots = [self._counterpart(p / store, a) for p, a in zip(profiles, accounts)]
            orgs = [self._directories(root) for root in roots]
            if any(not group_orgs for group_orgs in orgs):
                raise GraftError(f"No organization folder in {store}. Create a chat in each profile first.")
            if len(set(a.casefold() for a in accounts)) > 1:
                selected_orgs = []
                for items in orgs:
                    if len(items) > 1:
                        populated = [item for item in items if self._records(item)]
                        if len(populated) == 1:
                            items = populated
                    if len(items) != 1:
                        raise GraftError("Cross-account sharing requires exactly one unambiguous organization per profile; multiple populated organizations are never guessed.")
                    selected_orgs.append(items)
                orgs = selected_orgs
                result.append([items[0] for items in orgs])
            else:
                # Same account: map each organization's full/short ID without merging different organizations.
                seen = set()
                for items in orgs:
                    for org in items:
                        matched = [self._counterpart(root, org.name) for root in roots]
                        key = tuple(str(p) for p in matched)
                        if key not in seen:
                            seen.add(key)
                            result.append(matched)
        used = set()
        for scope in result:
            for folder in scope:
                key = str(folder).casefold()
                if key in used:
                    raise GraftError("Ambiguous organization mapping would reuse a folder. Resolve short/full organization IDs before merging.")
                used.add(key)
        return result

    def _records(self, folder):
        result, folded = {}, set()
        for path in self._safe(folder).iterdir():
            if not RECORD.fullmatch(path.name):
                continue
            self._safe(path)
            if not path.is_file():
                raise GraftError(f"Chat record is not a regular file: {path}")
            if path.name.casefold() in folded:
                raise GraftError(f"Case-colliding chat records in {folder}")
            folded.add(path.name.casefold())
            data = path.read_bytes()  # A read error is never treated as a deletion.
            if path.name.startswith("local_"):
                try:
                    record = json.loads(data)
                    if not isinstance(record, dict) or record.get("sessionId") != path.stem:
                        raise ValueError("unknown record schema or mismatched session ID")
                except (ValueError, UnicodeError) as exc:
                    raise GraftError(f"Unreadable chat record: {path}. No records were changed.") from exc
            result[path.name] = data
        return result

    def _blob(self, data):
        digest = _hash(data)
        folder = self._safe(self.backup_dir / "blobs")
        folder.mkdir(parents=True, exist_ok=True)
        file = self._safe(folder / digest)
        if file.exists():
            if _hash(file.read_bytes()) != digest:
                raise GraftError("A backup blob is corrupt. No records were changed.")
        else:
            _atomic(file, data)
        return digest

    def _transaction_path(self, relative):
        if not isinstance(relative, str) or "\\" in relative:
            raise GraftError("Invalid recovery journal path.")
        parts = relative.split("/")
        allowed = {self.profile_path(identity).relative_to(self.root).as_posix()
                   for identity in ["main", *[p["id"] for p in self.profiles()]]}
        from .transcripts import project_root, validate_relative
        for identity in ["main", *[p["id"] for p in self.profiles()]]:
            transcript_root = project_root(self, self.profile_path(identity))
            prefix_parts = transcript_root.relative_to(self.root).parts
            if tuple(parts[:len(prefix_parts)]) == prefix_parts and validate_relative(parts[len(prefix_parts):]):
                return self._safe(self.root.joinpath(*parts))
        prefix = "/".join(parts[:-4])
        if (len(parts) < 5 or prefix not in allowed or parts[-4] not in STORES
                or not TOKEN.fullmatch(parts[-3]) or not TOKEN.fullmatch(parts[-2]) or not RECORD.fullmatch(parts[-1])):
            raise GraftError("Invalid recovery journal path.")
        return self._safe(self.root.joinpath(*parts))

    def _recover(self):
        self._safe(self.pending_file)
        if not self.pending_file.exists():
            return False
        journal = _json(self.pending_file)
        if not isinstance(journal, dict) or journal.get("version") != 1 or not isinstance(journal.get("operations"), list):
            raise GraftError("Invalid recovery journal. Keep pending.json and backups for manual recovery.")
        self._validate_state(journal.get("state"))
        operations = []
        seen_paths = set()
        for op in journal["operations"]:
            if not isinstance(op, dict) or set(op) != {"path", "before", "after"}:
                raise GraftError("Invalid recovery operation.")
            path = self._transaction_path(op.get("path"))
            key = str(path).casefold()
            if key in seen_paths:
                raise GraftError("Duplicate destination in recovery journal. Nothing was written.")
            seen_paths.add(key)
            before, after = op.get("before"), op.get("after")
            if any(d is not None and (not isinstance(d, str) or not HASH.fullmatch(d)) for d in (before, after)):
                raise GraftError("Invalid recovery digest.")
            if before == after:
                raise GraftError("Invalid no-op in recovery journal.")
            if before is not None:
                backup = self._safe(self.backup_dir / "blobs" / before)
                try:
                    saved = backup.read_bytes()
                except FileNotFoundError as exc:
                    raise GraftError("A required pre-merge backup is missing. Recovery stopped before writing.") from exc
                if _hash(saved) != before:
                    raise GraftError("A pre-merge backup is corrupt. Recovery stopped before writing.")
            data = None
            if after is not None:
                data = self._safe(self.backup_dir / "blobs" / after).read_bytes()
                if _hash(data) != after:
                    raise GraftError("A recovery blob is corrupt. Nothing was written.")
                if path.name.startswith("local_"):
                    try:
                        record = json.loads(data)
                        if not isinstance(record, dict) or record.get("sessionId") != path.stem:
                            raise ValueError("record mismatch")
                    except (ValueError, UnicodeError) as exc:
                        raise GraftError("Recovery record does not match its destination. Nothing was written.") from exc
            if data is not None and path.suffix == ".jsonl":
                from .transcripts import validate_bytes
                try:
                    validate_bytes(data)
                except (ValueError, UnicodeError) as exc:
                    raise GraftError("Invalid transcript recovery payload; no files written.") from exc
            current = path.read_bytes() if path.exists() else None
            if _hash(current) not in (before, after):
                raise GraftError(f"{path.name} changed during an unfinished merge. Kept it untouched; review pending.json and backups.")
            operations.append((path, before, after, data))
        for path, before, after, data in operations:
            self._closed()
            self._safe(path)
            current = path.read_bytes() if path.exists() else None
            if _hash(current) == after:
                continue
            if _hash(current) != before:
                raise GraftError(f"{path.name} changed during merging. Merge stopped; backups and pending.json retained.")
            if data is None:
                path.unlink()
            else:
                if path.suffix == ".jsonl":
                    self._safe(path.parent)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._safe(path)
                _atomic(path, data)
        self._safe(self.state_file)
        _save(self.state_file, journal["state"])
        self.pending_file.unlink()
        return True

    @staticmethod
    def _validate_state(state):
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("groups"), dict):
            raise GraftError("Invalid mirrors.json; nothing was reset. Restore a valid file.")
        for group_id, group in state["groups"].items():
            if (not isinstance(group_id, str) or not isinstance(group, dict)
                    or not isinstance(group.get("members"), list)
                    or not all(isinstance(p, str) for p in group["members"])
                    or not isinstance(group.get("scopes"), dict)):
                raise GraftError("Invalid group baseline.")
            for scope, records in group["scopes"].items():
                if not isinstance(scope, str) or not isinstance(records, dict):
                    raise GraftError("Invalid scope baseline.")
                if any(not RECORD.fullmatch(k) or not isinstance(v, str) or not HASH.fullmatch(v)
                       for k, v in records.items()):
                    raise GraftError("Invalid record baseline.")

    def merge(self):
        with self.operation_lock():
            return self._merge()

    def _merge(self):
        self._closed()
        recovered = self._recover()
        state = _json(self._safe(self.state_file), {"version": 1, "groups": {}})
        self._validate_state(state)
        next_state = {"version": 1, "groups": {}}
        report = {"copied": 0, "deleted": 0, "conflicts": 0, "backups": 0, "warnings": [], "recovered": recovered}
        operations = []
        transcript_sets = []
        report["transcriptsCopied"] = 0
        report["resumeHandlesRepaired"] = 0
        for group in self.groups():
            sets = self._group_folders(group)  # Preflight every group before any profile write.
            transcript_sets.extend(sets)
            membership = sorted(group["members"])
            old = state["groups"].get(group["id"], {})
            if not isinstance(old, dict):
                raise GraftError("Invalid group baseline.")
            baselines = old.get("scopes", {}) if old.get("members") == membership else {}
            if not isinstance(baselines, dict):
                raise GraftError("Invalid group baseline.")
            scopes = {}
            for folders in sets:
                folders = sorted(folders, key=lambda p: str(p).casefold())
                scope = "\0".join(p.relative_to(self.root).as_posix() for p in folders)
                baseline = baselines.get(scope, {})
                if not isinstance(baseline, dict) or any(not RECORD.fullmatch(k) or not isinstance(v, str) or not HASH.fullmatch(v) for k, v in baseline.items()):
                    raise GraftError("Invalid record baseline.")
                snapshots = [self._records(p) for p in folders]
                names = sorted(set().union(*(set(s) for s in snapshots), set(baseline)))
                if len({n.casefold() for n in names}) != len(names):
                    raise GraftError("Case-colliding record names across profiles; resolve names before merging.")
                # A wholesale-empty folder can mean lost/restored data, not user deletions.
                empty = [not set(s).intersection(baseline) for s in snapshots]
                if baseline and any(empty) and not all(empty):
                    baseline = {}
                    report["warnings"].append(f"{group['name']}: an emptied folder was refilled; bulk deletion was not propagated.")
                agreed = dict(baseline)
                for name in names:
                    values = [s.get(name) for s in snapshots]
                    digests = [_hash(v) for v in values]
                    versions = set(digests)
                    prior = baseline.get(name)
                    changed = versions - {prior}
                    if len(versions) == 1:
                        wanted = digests[0]
                    elif prior is None:
                        nonempty = versions - {None}
                        wanted = next(iter(nonempty)) if len(nonempty) == 1 else "conflict"
                    elif len(changed) == 1:
                        wanted = next(iter(changed))
                    else:
                        wanted = "conflict"
                    if wanted == "conflict":
                        report["conflicts"] += 1
                        report["warnings"].append(f"{group['name']}: {name} differs in multiple profiles; all versions kept.")
                        continue
                    payload = next((v for v in values if _hash(v) == wanted), None)
                    for folder, before, digest in zip(folders, values, digests):
                        if digest == wanted:
                            continue
                        if before is not None:
                            self._blob(before)
                            report["backups"] += 1
                        if payload is not None:
                            self._blob(payload)
                        operations.append({"path": (folder / name).relative_to(self.root).as_posix(), "before": digest, "after": wanted})
                        report["deleted" if wanted is None else "copied"] += 1
                    if wanted is None:
                        agreed.pop(name, None)
                    else:
                        agreed[name] = wanted
                scopes[scope] = agreed
            next_state["groups"][group["id"]] = {"members": membership, "scopes": scopes}
        from .transcripts import plan
        plan(self, transcript_sets, operations, report)
        if operations:
            journal = {"version": 1, "operations": operations, "state": next_state}
            history = self._safe(self.backup_dir / "transactions")
            history.mkdir(parents=True, exist_ok=True)
            _save(history / (str(uuid.uuid4()) + ".json"), journal)
            _save(self.pending_file, journal)
            self._recover()
        else:
            _save(self.state_file, next_state)
        return report

