"""Read-only snapshots for the review dashboard."""

import difflib
import hashlib
import io
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx

from .graph import ReviewInput, validate_path

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 60 * 1024 * 1024
MAX_DIFF_CHARS = 40_000
HEX_SHA = re.compile(r'[0-9a-f]{40}')
GITHUB_REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+')
IGNORED_DIRECTORIES = {'.git', '.venv', 'node_modules', '__pycache__', 'vendor', 'dist'}


@dataclass(frozen=True)
class Snapshot:
    revision: str
    files: dict[str, str]
    provenance: str
    archive: bytes | None = None


def read_archive(content: bytes, git_sha: str | None = None, *, strip_root: bool = False) -> Snapshot:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError('ZIP exceeds 20 MiB.')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise ValueError('ZIP exceeds the expanded size or file-count limit.')
            comment = archive.comment.decode('ascii', errors='ignore').strip().lower()
            archive_sha = comment if HEX_SHA.fullmatch(comment) else None
            if git_sha and archive_sha and git_sha != archive_sha:
                raise ValueError('Archive commit differs from the revision returned by GitHub.')
            files = {}
            for entry in entries:
                if entry.is_dir():
                    continue
                path = validate_path(entry.filename)
                parts = path.split('/')
                if strip_root:
                    if len(parts) == 1:
                        raise ValueError('Expected a GitHub archive with one wrapping directory.')
                    parts = parts[1:]
                    path = '/'.join(parts)
                if any(part in IGNORED_DIRECTORIES or part == '.env' or part.startswith('.env.') for part in parts):
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size > 256_000:
                    continue
                if path in files:
                    raise ValueError('ZIP contains a duplicate path.')
                body = archive.read(entry)
                if b'\0' in body:
                    continue
                try:
                    files[path] = body.decode('utf-8')
                except UnicodeDecodeError:
                    continue
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError('Upload a readable, unencrypted ZIP archive.') from exc
    if not files:
        raise ValueError('The archive contains no readable source files.')
    archive = _pilot_archive(content, strip_root=strip_root)
    if git_sha:
        return Snapshot(git_sha, files, 'github', archive)
    if archive_sha:
        return Snapshot(archive_sha, files, 'git_archive', archive)
    fingerprint = hashlib.sha256(archive).hexdigest()
    return Snapshot(f'zip-{fingerprint}', files, 'zip', archive)


def _pilot_archive(content: bytes, *, strip_root: bool) -> bytes:
    """Keep the full source tree and add a root for Pilot to remove."""
    output = io.BytesIO()
    try:
        with (zipfile.ZipFile(io.BytesIO(content)) as source,
              zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target):
            for entry in sorted(source.infolist(), key=lambda item: item.filename):
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if entry.is_dir() or mode not in (0, stat.S_IFREG):
                    continue
                parts = validate_path(entry.filename).split('/')
                if strip_root:
                    parts = parts[1:]
                if any(part.casefold() in IGNORED_DIRECTORIES | {'graphify-out'}
                       or part.casefold().startswith(('.env', '.graphify')) for part in parts):
                    continue
                with source.open(entry) as incoming, target.open('repository/' + '/'.join(parts), 'w') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError('Upload a readable, unencrypted ZIP archive.') from exc
    return output.getvalue()


def graphify_archive(snapshot: Snapshot) -> bytes:
    if snapshot.archive is not None:
        return snapshot.archive
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for path, content in snapshot.files.items():
            if not any(part.casefold().startswith(('.env', '.graphify')) for part in path.split('/')):
                target.writestr(f'repository/{path}', content)
    return output.getvalue()


def _github_api(repo: str, ref: str) -> str:
    if not GITHUB_REPO.fullmatch(repo) or '..' in repo or not ref or len(ref) > 180:
        raise ValueError('Enter a public GitHub repository as owner/repo and a branch or commit.')
    return f'https://api.github.com/repos/{repo}'


def github_commits(client: httpx.Client, repo: str, ref: str, page: int) -> dict:
    response = client.get(f'{_github_api(repo, ref)}/commits', params={'sha': ref, 'per_page': 20, 'page': page})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('GitHub did not return a commit list.')
    commits = []
    try:
        for item in payload:
            sha = item.get('sha', '')
            if not HEX_SHA.fullmatch(sha):
                raise ValueError('GitHub did not return a commit SHA.')
            metadata = item['commit']
            subject = metadata['message'].partition('\n')[0].strip()[:200]
            parent = next(iter(item.get('parents', [])), {}).get('sha', '')
            commits.append({'sha': sha, 'subject': subject, 'date': metadata['committer']['date'],
                            'parent_sha': parent if HEX_SHA.fullmatch(parent) else None})
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError('GitHub returned incomplete commit metadata.') from exc
    return {'commits': commits, 'has_more': 'next' in response.links}


def github_snapshot(client: httpx.Client, repo: str, ref: str) -> Snapshot:
    api = _github_api(repo, ref)
    response = client.get(f'{api}/commits/{quote(ref, safe="")}')
    response.raise_for_status()
    sha = response.json().get('sha', '')
    if not HEX_SHA.fullmatch(sha):
        raise ValueError('GitHub did not return a commit SHA.')
    with client.stream('GET', f'https://codeload.github.com/{repo}/zip/{sha}') as archive:
        archive.raise_for_status()
        data = bytearray()
        for chunk in archive.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise ValueError('GitHub archive exceeds 20 MiB.')
    return read_archive(bytes(data), sha, strip_root=True)


def snapshot_diff(base: Snapshot, head: Snapshot) -> tuple[ReviewInput, list[str]]:
    changed = []
    chunks = []
    size = 0
    for path in sorted(base.files.keys() | head.files.keys()):
        before, after = base.files.get(path), head.files.get(path)
        if before == after:
            continue
        changed.append(path)
        for line in difflib.unified_diff(
            (before or '').splitlines(), (after or '').splitlines(),
            fromfile=f'a/{path}' if before is not None else '/dev/null',
            tofile=f'b/{path}' if after is not None else '/dev/null', lineterm='',
        ):
            size += len(line) + 1
            if size > MAX_DIFF_CHARS:
                raise ValueError('Diff exceeds 40,000 characters. Select closer snapshots.')
            chunks.append(line)
    if not chunks:
        raise ValueError('These snapshots have no reviewable line changes.')
    return ReviewInput(base.revision, head.revision, '\n'.join(chunks)), changed


def inventory(files: dict[str, str]) -> list[dict]:
    return [{'path': path, 'lines': len(text.splitlines()), 'extension': PurePosixPath(path).suffix}
            for path, text in sorted(files.items())]
