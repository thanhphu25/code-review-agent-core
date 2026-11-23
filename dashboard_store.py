"""SQLite persistence for projects, immutable snapshots, and review history."""

import json
import sqlite3
from time import time
from uuid import uuid4
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .dashboard_sources import Snapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.chmod(0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, github_repo TEXT, ref TEXT,
                    kind TEXT NOT NULL, codelens_project_id INTEGER, codelens_branch TEXT,
                    codelens_branch_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                    revision TEXT NOT NULL, provenance TEXT NOT NULL, files TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(project_id, revision)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                    provider TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_settings (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_sessions (
                    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, provider TEXT NOT NULL,
                    base_id INTEGER NOT NULL REFERENCES snapshots(id), head_id INTEGER NOT NULL REFERENCES snapshots(id),
                    related TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graphify_indexes (
                    id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    origin TEXT NOT NULL, username TEXT NOT NULL, upload_key TEXT NOT NULL,
                    remote_id TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
                    detail TEXT NOT NULL DEFAULT '', next_check REAL NOT NULL DEFAULT 0,
                    UNIQUE(snapshot_id, origin, username)
                );
            ''')
            db.execute('BEGIN IMMEDIATE')
            self._add_column(db, 'projects', 'review_provider', "TEXT NOT NULL DEFAULT 'graphify'")
            self._add_column(db, 'snapshots', 'archive', 'BLOB')
            self._add_column(db, 'snapshots', 'session_only', 'INTEGER NOT NULL DEFAULT 0')
            self._add_column(db, 'reviews', 'session_id', 'INTEGER REFERENCES review_sessions(id)')
            db.execute('CREATE INDEX IF NOT EXISTS reviews_by_session ON reviews(session_id, id)')

    @staticmethod
    def _add_column(db, table, name, definition):
        if name not in {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def projects(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM projects ORDER BY id')]

    def project(self, project_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
            return dict(row) if row else None

    def create_project(self, name: str, kind: str, snapshots: list[Snapshot], repo: str = '', ref: str = '',
                       review_provider: str = 'graphify', graphify_account: tuple[str, str] | None = None) -> int:
        with self.connect() as db:
            cursor = db.execute('INSERT INTO projects(name,kind,github_repo,ref,review_provider) VALUES(?,?,?,?,?)',
                                (name, kind, repo, ref, review_provider))
            project_id = cursor.lastrowid
            for snapshot in snapshots:
                self._insert_snapshot(db, project_id, snapshot)
            if snapshots and graphify_account:
                self._enqueue_snapshot(db, project_id, snapshots[-1].revision, graphify_account)
            return project_id

    def _insert_snapshot(self, db, project_id: int, snapshot: Snapshot, *, session_only: bool = False) -> bool:
        cursor = db.execute(
            '''INSERT OR IGNORE INTO snapshots(project_id,revision,provenance,files,created_at,archive,session_only)
               VALUES(?,?,?,?,?,?,?)''',
            (project_id, snapshot.revision, snapshot.provenance, json.dumps(snapshot.files), utc_now(), snapshot.archive,
             session_only),
        )
        inserted = bool(cursor.rowcount)
        if not inserted and snapshot.archive is not None:
            db.execute('UPDATE snapshots SET archive=? WHERE project_id=? AND revision=? AND archive IS NULL',
                       (snapshot.archive, project_id, snapshot.revision))
        if not inserted and not session_only:
            cursor = db.execute('''UPDATE snapshots SET session_only=0
                                   WHERE project_id=? AND revision=? AND session_only=1''',
                                (project_id, snapshot.revision))
            return bool(cursor.rowcount)
        return inserted

    def add_snapshot(self, project_id: int, snapshot: Snapshot,
                     graphify_account: tuple[str, str] | None = None) -> bool:
        with self.connect() as db:
            inserted = self._insert_snapshot(db, project_id, snapshot)
            if graphify_account:
                self._enqueue_snapshot(db, project_id, snapshot.revision, graphify_account)
            return inserted

    def _enqueue_snapshot(self, db, project_id: int, revision: str, account: tuple[str, str]):
        self._enqueue_graphify(db, self._snapshot_id(db, project_id, revision), *account)

    @staticmethod
    def _snapshot_id(db, project_id: int, revision: str) -> int:
        return db.execute('SELECT id FROM snapshots WHERE project_id=? AND revision=?',
                          (project_id, revision)).fetchone()['id']

    def snapshots(self, project_id: int) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT id, revision, provenance, created_at FROM snapshots
                WHERE project_id = ? ORDER BY session_only, id DESC
            ''', (project_id,))]

    def snapshot(self, project_id: int, snapshot_id: int, *, include_archive: bool = False) -> Snapshot | None:
        with self.connect() as db:
            columns = 'revision,files,provenance,archive' if include_archive else 'revision,files,provenance,NULL AS archive'
            row = db.execute(f'SELECT {columns} FROM snapshots WHERE project_id=? AND id=?',
                             (project_id, snapshot_id)).fetchone()
            return Snapshot(row['revision'], json.loads(row['files']), row['provenance'], row['archive']) if row else None

    def has_snapshot(self, project_id: int, snapshot_id: int) -> bool:
        return self.snapshot_info(project_id, snapshot_id) is not None

    def snapshot_info(self, project_id: int, snapshot_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT id,revision,created_at FROM snapshots WHERE project_id=? AND id=?',
                             (project_id, snapshot_id)).fetchone()
            return dict(row) if row else None

    def snapshot_id_at_revision(self, project_id: int, revision: str) -> int | None:
        with self.connect() as db:
            row = db.execute('SELECT id FROM snapshots WHERE project_id=? AND revision=?',
                             (project_id, revision)).fetchone()
            return row['id'] if row else None

    def set_review_provider(self, project_id: int, provider: str):
        with self.connect() as db:
            db.execute('UPDATE projects SET review_provider=? WHERE id=?', (provider, project_id))

    def enqueue_graphify(self, snapshot_id: int, origin: str, username: str, *, retry: bool = False):
        with self.connect() as db:
            self._enqueue_graphify(db, snapshot_id, origin, username, retry=retry)

    @staticmethod
    def _enqueue_graphify(db, snapshot_id: int, origin: str, username: str, *, retry: bool = False):
        db.execute('''INSERT OR IGNORE INTO graphify_indexes(snapshot_id,origin,username,upload_key)
                      VALUES(?,?,?,?)''', (snapshot_id, origin, username, uuid4().hex))
        db.execute('''UPDATE graphify_indexes SET status='queued', detail='', next_check=0
                      WHERE snapshot_id=? AND origin=? AND username=?
                      AND ((status='failed' AND ?) OR (status='ready' AND expires_at<=?))''',
                   (snapshot_id, origin, username, retry, time() + 60))

    def graphify_index(self, snapshot_id: int, origin: str, username: str) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM graphify_indexes WHERE snapshot_id=? AND origin=? AND username=?',
                             (snapshot_id, origin, username)).fetchone()
            return dict(row) if row else None

    def claim_graphify(self, origin: str, username: str) -> dict | None:
        now = time()
        with self.connect() as db:
            row = db.execute('''UPDATE graphify_indexes SET next_check=? WHERE id=(
                SELECT id FROM graphify_indexes WHERE origin=? AND username=?
                AND status IN ('queued','indexing') AND next_check<=? ORDER BY next_check,id LIMIT 1
                ) RETURNING *''', (now + 120, origin, username, now)).fetchone()
            if row is None:
                return None
            source = db.execute('SELECT project_id,revision FROM snapshots WHERE id=?', (row['snapshot_id'],)).fetchone()
            title = db.execute('SELECT name FROM projects WHERE id=?', (source['project_id'],)).fetchone()['name']
            return {**dict(row), **dict(source), 'name': title}

    def update_graphify(self, index_id: int, status: str, *, remote_id: str, detail: str = '',
                        token: str = '', expires_at: float = 0, next_check: float = 0, replace_upload: bool = False):
        with self.connect() as db:
            db.execute('''UPDATE graphify_indexes SET status=?,remote_id=?,detail=?,token=?,expires_at=?,
                          next_check=?,upload_key=COALESCE(?,upload_key) WHERE id=?''',
                       (status, remote_id, detail, token, expires_at, next_check,
                        uuid4().hex if replace_upload else None, index_id))

    def update_codelens(self, project_id: int, project_number: int | None, branch: str, branch_id: int | None):
        with self.connect() as db:
            db.execute('UPDATE projects SET codelens_project_id=?,codelens_branch=?,codelens_branch_id=? WHERE id=?',
                       (project_number, branch, branch_id, project_id))

    def create_session(self, project_id: int, title: str, provider: str, base: int | Snapshot, head: int | Snapshot,
                       related: list[dict]) -> int:
        with self.connect() as db:
            pins = []
            for revision in (base, head):
                if isinstance(revision, Snapshot):
                    self._insert_snapshot(db, project_id, revision, session_only=True)
                    revision = self._snapshot_id(db, project_id, revision.revision)
                pins.append(revision)
            row = db.execute('''INSERT INTO review_sessions(project_id,title,provider,base_id,head_id,related,created_at)
                                VALUES(?,?,?,?,?,?,?) RETURNING id''',
                             (project_id, title, provider, *pins, json.dumps(related), utc_now())).fetchone()
            return row['id']

    def sessions(self, project_id: int) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT id,title,provider,base_id,head_id,created_at
                                                       FROM review_sessions WHERE project_id=? ORDER BY id DESC''',
                                                    (project_id,))]

    def session(self, project_id: int, session_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM review_sessions WHERE project_id=? AND id=?',
                             (project_id, session_id)).fetchone()
            return {**dict(row), 'related': json.loads(row['related'])} if row else None

    def add_review(self, project_id: int, provider: str, result: dict, session_id: int | None = None) -> dict:
        created_at = utc_now()
        with self.connect() as db:
            cursor = db.execute('INSERT INTO reviews(project_id,provider,created_at,result,session_id) VALUES(?,?,?,?,?)',
                                (project_id, provider, created_at, json.dumps(result), session_id))
            return {'id': cursor.lastrowid, 'provider': provider, 'created_at': created_at,
                    'result': result, 'session_id': session_id}

    def reviews(self, project_id: int, session_id: int | None = None) -> list[dict]:
        with self.connect() as db:
            query = '''SELECT id,provider,created_at,result,session_id FROM reviews
                       WHERE project_id=? AND session_id IS ? ORDER BY id DESC'''
            if session_id is None:
                query += ' LIMIT 25'
            rows = db.execute(query, (project_id, session_id))
            return [{**dict(row), 'result': json.loads(row['result'])} for row in rows]

    def review(self, project_id: int, review_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT provider,result,session_id FROM reviews WHERE project_id=? AND id=?',
                             (project_id, review_id)).fetchone()
            return {**dict(row), 'result': json.loads(row['result'])} if row else None
