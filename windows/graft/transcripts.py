"""Plan additive, journaled Code transcript transfers; never transfer credentials."""
import json
import re
from pathlib import Path

UUID = re.compile(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z')
AGENT = re.compile(r'agent-[a-zA-Z0-9-]+\.jsonl\Z')


def project_root(manager, profile):
    config = manager._load_config()
    if profile == manager.profile_path('main') and config.get('mainTranscriptBase'):
        return manager._safe(manager.root / config['mainTranscriptBase'])
    return manager._safe(profile / '.claude-code/projects')


def validate_relative(parts):
    if not parts or any(not p or p in ('.', '..') or ':' in p or '\\' in p for p in parts):
        return False
    if len(parts) == 2:
        return parts[1].endswith('.jsonl') and bool(UUID.fullmatch(parts[1][:-6]))
    return (len(parts) == 4 and UUID.fullmatch(parts[1])
            and parts[2] == 'subagents' and AGENT.fullmatch(parts[3]))


def validate_bytes(data):
    if not data or not data.endswith(b'\n'):
        raise ValueError('Transcript is empty or has an unfinished final line')
    for line in data.splitlines():
        if line.strip() and not isinstance(json.loads(line), dict):
            raise ValueError('Transcript line is not an object')


def plan(manager, folder_sets, operations, report):
    from .core import GraftError, _hash
    planned = {o['path']: o for o in operations}
    additions = []
    blobs = {}
    def bytes_at(path):
        relative = path.relative_to(manager.root).as_posix()
        op = planned.get(relative)
        if op:
            return None if op['after'] is None else (manager.backup_dir/'blobs'/op['after']).read_bytes()
        return path.read_bytes() if path.exists() else None
    def schedule(path, data):
        manager._safe(path)
        relative = path.relative_to(manager.root).as_posix()
        before = path.read_bytes() if path.exists() else None
        after = _hash(data)
        if _hash(before) == after:
            return
        op = {'path': relative, 'before': _hash(before), 'after': after}
        if before is not None:
            blobs[_hash(before)] = before
        blobs[after] = data
        planned[relative] = op
        additions.append(op)
    for folders in folder_sets:
        if folders[0].parent.parent.name != 'claude-code-sessions':
            continue
        profiles = [p.parent.parent.parent for p in folders]
        roots = [project_root(manager, p) for p in profiles]
        records = [manager._records(p) for p in folders]
        names = set().union(*(set(r) for r in records))
        for name in sorted(names):
            if not name.startswith('local_'):
                continue
            current = [json.loads(r[name]) if name in r else None for r in records]
            ids = {r.get('cliSessionId') for r in current if r and UUID.fullmatch(str(r.get('cliSessionId', '')))}
            if not ids:
                continue
            if len(ids) != 1:
                report['warnings'].append(f'{name}: divergent resume IDs; transcripts kept untouched.')
                continue
            identity = next(iter(ids))
            candidates = []
            for root in roots:
                manager._safe(root)
                if not root.exists():
                    continue
                for file in root.glob('*/'+identity+'.jsonl'):
                    manager._safe(file)
                    candidates.append(file)
            if not candidates:
                report['warnings'].append(f'{name}: source transcript unavailable; history was not repaired.')
                continue
            projects = {p.parent.name for p in candidates}
            if len(projects) != 1:
                report['warnings'].append(f'{name}: ambiguous transcript project; history kept untouched.')
                continue
            project = next(iter(projects))
            bundles = {}
            try:
                for file in candidates:
                    data = file.read_bytes()
                    validate_bytes(data)
                    bundles.setdefault(Path(project)/(identity+'.jsonl'), []).append(data)
                    children = file.parent/identity/'subagents'
                    manager._safe(children)
                    if children.exists():
                        for child in children.iterdir():
                            manager._safe(child)
                            if child.is_file() and AGENT.fullmatch(child.name):
                                payload = child.read_bytes()
                                validate_bytes(payload)
                                bundles.setdefault(Path(project)/identity/'subagents'/child.name, []).append(payload)
                chosen = {}
                for relative, versions in bundles.items():
                    longest = max(versions, key=len)
                    if any(not longest.startswith(v) for v in versions):
                        raise ValueError('divergent transcripts')
                    chosen[relative] = longest
                # Preflight the complete bundle before scheduling any of its files.
                for root in roots:
                    for relative, data in chosen.items():
                        target = manager._safe(root/relative)
                        existing = bytes_at(target)
                        if existing is not None and not (data.startswith(existing) or existing.startswith(data)):
                            raise ValueError('destination transcript diverged')
                for folder, root in zip(folders, roots):
                    for relative, data in chosen.items():
                        target = root/relative
                        existing = bytes_at(target)
                        if existing is None or (len(data) > len(existing) and data.startswith(existing)):
                            schedule(target, data)
                            report['transcriptsCopied'] += 1
                    record_path = folder/name
                    record_bytes = bytes_at(record_path)
                    if record_bytes is None:
                        continue
                    record = json.loads(record_bytes)
                    if record.get('cliSessionId') not in (None, identity):
                        continue
                    if record.get('transcriptUnavailable') or not record.get('cliSessionId'):
                        record['cliSessionId'] = identity
                        record.pop('transcriptUnavailable', None)
                        record.pop('unarchivedCliSessionId', None)
                        schedule(record_path, (json.dumps(record, ensure_ascii=False, indent=2)+'\n').encode())
                        report['resumeHandlesRepaired'] += 1
            except (ValueError, UnicodeError) as exc:
                report['warnings'].append(f'{name}: {exc}; transcript bundle kept untouched.')
            # Keep memory bounded to one session bundle, not the entire library.
            for data in blobs.values():
                manager._blob(data)
            blobs.clear()
    if additions:
        operations[:] = list(planned.values())
