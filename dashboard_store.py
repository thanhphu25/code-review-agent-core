"""SQLite persistence for projects, immutable snapshots, and review history."""

import json
import sqlite3
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
            ''')

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

    def create_project(self, name: str, kind: str, snapshots: list[Snapshot], repo: str = '', ref: str = '') -> int:
        with self.connect() as db:
            cursor = db.execute('INSERT INTO projects(name,kind,github_repo,ref) VALUES(?,?,?,?)', (name, kind, repo, ref))
            project_id = cursor.lastrowid
            for snapshot in snapshots:
                self._insert_snapshot(db, project_id, snapshot)
            return project_id

    def _insert_snapshot(self, db, project_id: int, snapshot: Snapshot) -> bool:
        cursor = db.execute(
            'INSERT OR IGNORE INTO snapshots(project_id,revision,provenance,files,created_at) VALUES(?,?,?,?,?)',
            (project_id, snapshot.revision, snapshot.provenance, json.dumps(snapshot.files), utc_now()),
        )
        return bool(cursor.rowcount)

    def add_snapshot(self, project_id: int, snapshot: Snapshot) -> bool:
        with self.connect() as db:
            return self._insert_snapshot(db, project_id, snapshot)

    def snapshots(self, project_id: int) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT id, revision, provenance, created_at FROM snapshots
                WHERE project_id = ? ORDER BY id DESC
            ''', (project_id,))]

    def snapshot(self, project_id: int, snapshot_id: int) -> Snapshot | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM snapshots WHERE project_id=? AND id=?', (project_id, snapshot_id)).fetchone()
            return Snapshot(row['revision'], json.loads(row['files']), row['provenance']) if row else None

    def update_codelens(self, project_id: int, project_number: int | None, branch: str, branch_id: int | None):
        with self.connect() as db:
            db.execute('UPDATE projects SET codelens_project_id=?,codelens_branch=?,codelens_branch_id=? WHERE id=?',
                       (project_number, branch, branch_id, project_id))

    def add_review(self, project_id: int, provider: str, result: dict) -> dict:
        created_at = utc_now()
        with self.connect() as db:
            cursor = db.execute('INSERT INTO reviews(project_id,provider,created_at,result) VALUES(?,?,?,?)',
                                (project_id, provider, created_at, json.dumps(result)))
            return {'id': cursor.lastrowid, 'provider': provider, 'created_at': created_at, 'result': result}

    def reviews(self, project_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT id,provider,created_at,result FROM reviews WHERE project_id=? ORDER BY id DESC LIMIT 25',
                              (project_id,))
            return [{**dict(row), 'result': json.loads(row['result'])} for row in rows]
