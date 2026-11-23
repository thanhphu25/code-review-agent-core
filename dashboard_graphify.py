"""Provision Graphify Pilot projects and connect reviews to their exact snapshots."""

from dataclasses import replace
from threading import Event
from time import time

import httpx

from .dashboard_providers import LiveSettings
from .dashboard_settings import ProviderSettings
from .dashboard_sources import graphify_archive
from .dashboard_store import Store
from .graph import ReviewUnavailable


def account(settings: LiveSettings) -> tuple[str, str]:
    return settings.graphify_pilot_url.rstrip('/'), settings.graphify_pilot_user


class PilotIndexer:
    def __init__(self, store: Store, providers: ProviderSettings):
        self.store = store
        self.providers = providers
        self.wake = Event()
        self.stop = Event()

    def enqueue(self, settings: LiveSettings, snapshot_id: int, *, retry: bool = False):
        self.store.enqueue_graphify(snapshot_id, *account(settings), retry=retry)
        self.wake.set()

    def status(self, settings: LiveSettings, snapshot_id: int) -> dict:
        if not settings.availability()['graphify_pilot']:
            return {'status': 'manual' if settings.availability()['graphify'] else 'unconfigured'}
        row = self.store.graphify_index(snapshot_id, *account(settings))
        if not row:
            return {'status': 'not_indexed'}
        if row['status'] == 'ready' and row['expires_at'] <= time() + 60:
            return {'status': 'expired', 'project_id': row['remote_id']}
        return {'status': row['status'], 'project_id': row['remote_id'], 'detail': row['detail']}

    def review_settings(self, settings: LiveSettings, snapshot_id: int, revision: str) -> LiveSettings:
        if not settings.availability()['graphify_pilot']:
            return settings
        row = self.store.graphify_index(snapshot_id, *account(settings))
        if not row or row['status'] != 'ready':
            raise ReviewUnavailable('graphify_head_index_required')
        if row['expires_at'] <= time() + 60:
            self.enqueue(settings, snapshot_id)
            raise ReviewUnavailable('graphify_head_token_refresh_pending')
        return replace(settings, graphify_url=f"{row['origin']}/mcp/{row['remote_id']}",
                       graphify_token=row['token'], graphify_sha=revision,
                       graphify_revision_evidence='graphify_agent_upload')

    def work(self):
        client = None
        credentials = None
        try:
            while not self.stop.is_set():
                self.wake.clear()
                settings = self.providers.get()
                if settings.availability()['graphify_pilot']:
                    origin, username = account(settings)
                    job = self.store.claim_graphify(origin, username)
                    if job:
                        current = (origin, username, settings.graphify_pilot_password)
                        if credentials != current:
                            if client is not None:
                                client.close()
                            client = httpx.Client(base_url=origin, timeout=20, headers={'X-Graphify-Request': '1'})
                            credentials = current
                        self.advance(client, username, settings.graphify_pilot_password, job)
                        continue
                self.wake.wait(1)
        finally:
            if client is not None:
                client.close()

    def advance(self, client: httpx.Client, username: str, password: str, job: dict):
        index_id, remote_id = job['id'], job['remote_id']
        name = ''.join(char for char in job['name'] if char.isprintable()).strip()[:48] or 'Repository'
        title = f"{name} · {job['revision'][:12]} · {job['upload_key']}"
        try:
            if not client.cookies:
                self.login(client, username, password)
            response = client.get('/api/projects')
            if response.status_code == 401:
                self.login(client, username, password)
                response = client.get('/api/projects')
            projects = response.raise_for_status().json()
            project = next((item for item in projects
                            if item['id'] == remote_id or (not remote_id and item['title'] == title)), None)
            if project is None and remote_id:
                self.store.update_graphify(index_id, 'failed', remote_id='', replace_upload=True,
                                           detail='Project Graphify đã bị xóa. Thử index lại.')
                return
            if project is None:
                snapshot = self.store.snapshot(job['project_id'], job['snapshot_id'], include_archive=True)
                response = client.post('/api/projects', params={'title': title}, content=graphify_archive(snapshot),
                                       headers={'Content-Type': 'application/zip'})
                remote_id = response.raise_for_status().json()['id']
                self.store.update_graphify(index_id, 'indexing', remote_id=remote_id, next_check=time() + 2)
                return
            remote_id = project['id']
            if project['status'] == 'ready':
                response = client.post(f'/api/projects/{remote_id}/token')
                token = response.raise_for_status().json()['token']
                self.store.update_graphify(index_id, 'ready', remote_id=remote_id, token=token,
                                           expires_at=time() + 29 * 86400, detail=project.get('detail') or '')
            elif project['status'] == 'failed':
                self.store.update_graphify(index_id, 'failed', remote_id='', replace_upload=True,
                                           detail=project.get('detail') or 'Graphify không index được snapshot.')
            else:
                self.store.update_graphify(index_id, 'indexing', remote_id=remote_id, next_check=time() + 2)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            detail = f'Graphify Pilot trả HTTP {code}.'
            if code in (401, 403):
                detail += ' Kiểm tra URL và tài khoản Pilot.'
            if code == 409:
                detail += ' Kiểm tra số project và trạng thái index trên Pilot.'
            self.store.update_graphify(index_id, 'failed', remote_id=remote_id, detail=detail)
        except httpx.RequestError:
            self.store.update_graphify(index_id, 'failed', remote_id=remote_id,
                                       detail='Kết nối Graphify Pilot bị gián đoạn. Có thể thử lại.')
        except (ValueError, KeyError, TypeError) as exc:
            self.store.update_graphify(index_id, 'failed', remote_id=remote_id,
                                       detail=f'Dữ liệu Graphify Pilot không hợp lệ ({type(exc).__name__}).')

    @staticmethod
    def login(client: httpx.Client, username: str, password: str):
        client.cookies.clear()
        client.post('/api/login', json={'username': username, 'password': password}).raise_for_status()
