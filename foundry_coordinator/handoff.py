"""Explicit, hash-bound adoption of existing task work; default starts stay clean."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path, PurePosixPath

from .profile import matches_any


def scope_hash(scope: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(scope)).encode()).hexdigest()


def _file(root: Path, rel: str) -> Path:
    path = PurePosixPath(rel)
    if not rel or '\\' in rel or ':' in rel or path.is_absolute() or '..' in path.parts:
        raise ValueError(f"handoff path must be workspace-relative: {rel!r}")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"handoff path escapes workspace: {rel}")
    current = target
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError(f"handoff path uses a link: {rel}")
        current = current.parent
    if not target.is_file():
        raise ValueError(f"handoff currently requires a regular existing file: {rel}")
    return target


def capture(ws, spec, state_dir: str, paths: list[str], approver: str) -> dict:
    root = Path(ws.path).resolve()
    archive = (Path(state_dir).resolve() / 'handoffs').resolve() / uuid.uuid4().hex
    if archive.is_relative_to(root):
        raise ValueError('handoff backups must live outside the coding workspace')
    dirty = {p for _, p in ws.status()}
    originals = {}
    for rel in sorted(set(paths)):
        if rel not in dirty or not matches_any(rel, spec.scope):
            raise ValueError(f"handoff must name an existing dirty task-scope file: {rel}")
        originals[rel] = _file(root, rel).read_bytes()
    if not originals:
        raise ValueError('handoff must name at least one file')
    head = ws.head()
    archive.mkdir(parents=True, exist_ok=False)
    files = {}
    for rel, content in originals.items():
        destination = archive / 'files' / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as f:
            f.write(content)
        if _file(root, rel).read_bytes() != content:
            raise ValueError(f"handoff file changed while taking its backup: {rel}")
        files[rel] = hashlib.sha256(content).hexdigest()
    if ws.head() != head:
        raise ValueError('workspace revision changed while taking handoff backups')
    manifest = dict(project_id=spec.project_id, task_id=spec.task_id, approved_by=approver,
                    head=head, scope_hash=scope_hash(spec.scope), archive=str(archive), files=files)
    (archive / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def validate(ws, spec, state_dir: str, manifest: dict) -> None:
    if manifest.get('project_id') != spec.project_id or manifest.get('task_id') != spec.task_id:
        raise ValueError('handoff task identity changed')
    if manifest.get('head') != ws.head() or manifest.get('scope_hash') != scope_hash(spec.scope):
        raise ValueError('handoff revision or task scope changed; approve a fresh handoff')
    archive = Path(manifest['archive']).resolve()
    if not archive.is_relative_to((Path(state_dir).resolve() / 'handoffs').resolve()):
        raise ValueError('handoff backup location is outside coordinator storage')
    for rel, digest in manifest['files'].items():
        if not matches_any(rel, spec.scope):
            raise ValueError(f"handoff file is outside task scope: {rel}")
        for label, root in [('workspace', Path(ws.path).resolve()), ('backup', archive / 'files')]:
            if hashlib.sha256(_file(root, rel).read_bytes()).hexdigest() != digest:
                raise ValueError(f"handoff {label} bytes changed: {rel}")
