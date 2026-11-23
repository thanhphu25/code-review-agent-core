"""Local dashboard for repository snapshots, reviews, and provider comparison."""

import base64
import binascii
import json
import os
import secrets
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .dashboard_graphify import PilotIndexer, account
from .dashboard_providers import LiveSettings, RelatedTarget, live_review
from .dashboard_settings import ProviderPatch, ProviderSettings, public_settings
from .dashboard_sources import (
    HEX_SHA, MAX_ARCHIVE_BYTES, MAX_DIFF_CHARS, Snapshot, github_commits, github_snapshot, inventory, read_archive, snapshot_diff,
)
from .dashboard_store import Store
from .demo import CALLER_FILE, DEMO_INPUT, SIGNATURE_FILE, demo_source, run_demo
from .graph import RelatedRepository, ReviewUnavailable

STATIC = Path(__file__).parent / 'static'


class ProjectRequest(BaseModel):
    name: str = Field(default='', max_length=100)
    kind: Literal['github', 'zip']
    repo: str = Field(default='', max_length=170)
    ref: str = Field(default='main', min_length=1, max_length=180)
    base_ref: str = Field(default='', max_length=180)
    review_provider: Literal['codelens', 'graphify'] = 'graphify'


class ModeRequest(BaseModel):
    provider: Literal['codelens', 'graphify']


class CodeLensRequest(BaseModel):
    project_id: int | None = Field(default=None, gt=0)
    branch: str = Field(default='', max_length=180)
    branch_id: int | None = Field(default=None, gt=0)


class TurnRequest(BaseModel):
    focus: str = Field(default='', max_length=2000)
    follow_up_to: int | None = Field(default=None, gt=0)


class ReviewRequest(TurnRequest):
    base_id: int = Field(gt=0)
    head_id: int = Field(gt=0)
    providers: list[Literal['codelens', 'graphify', 'demo']] = Field(min_length=1, max_length=2)


class RelatedSnapshotRequest(BaseModel):
    project_id: int = Field(gt=0)
    head_id: int = Field(gt=0)
    base_id: int | None = Field(default=None, gt=0)


class SessionRequest(BaseModel):
    title: str = Field(default='', max_length=100)
    provider: Literal['codelens', 'graphify', 'demo']
    base_id: int | None = Field(default=None, gt=0)
    head_id: int | None = Field(default=None, gt=0)
    base_ref: str = Field(default='', max_length=180)
    head_ref: str = Field(default='', max_length=180)
    related: list[RelatedSnapshotRequest] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def validate_pair(self):
        self.base_ref = self.base_ref.strip()
        self.head_ref = self.head_ref.strip()
        refs = bool(self.base_ref) and bool(self.head_ref)
        ids = self.base_id is not None and self.head_id is not None
        if not ((refs and self.base_id is None and self.head_id is None)
                or (ids and not self.base_ref and not self.head_ref)):
            raise ValueError('Chọn hai snapshot hoặc nhập cả base và head từ GitHub.')
        return self


def _demo_snapshots() -> list[Snapshot]:
    common = {CALLER_FILE: demo_source(CALLER_FILE)}
    return [
        Snapshot(DEMO_INPUT.base_sha, {**common, SIGNATURE_FILE: 'def total(items):\n    return sum(items)\n'}, 'fixture'),
        Snapshot(DEMO_INPUT.head_sha, {**common, SIGNATURE_FILE: 'def total(items, fee):\n    return sum(items) + fee\n'}, 'fixture'),
    ]


def create_app(db_path: Path, settings: LiveSettings | None = None, password: str = '') -> FastAPI:
    store = Store(db_path)
    providers = ProviderSettings(store, settings or LiveSettings.from_env())
    pilot = PilotIndexer(store, providers)
    if not store.projects():
        store.create_project('Pricing demo', 'demo', _demo_snapshots())
    @asynccontextmanager
    async def lifespan(_app):
        pilot.stop.clear()
        worker = Thread(target=pilot.work, name='graphify-indexer', daemon=True)
        worker.start()
        try:
            yield
        finally:
            pilot.stop.set()
            pilot.wake.set()
            await run_in_threadpool(worker.join)

    app = FastAPI(title='Code Review Agent', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.middleware('http')
    async def access(request: Request, call_next):
        if password:
            scheme, _, token = request.headers.get('authorization', '').partition(' ')
            try:
                expected = f'review:{password}'.encode()
                allowed = scheme.lower() == 'basic' and secrets.compare_digest(base64.b64decode(token, validate=True), expected)
            except (binascii.Error, ValueError):
                allowed = False
            if not allowed:
                return JSONResponse({'detail': 'Authentication required.'}, 401,
                                    headers={'WWW-Authenticate': 'Basic realm="Code Review"'})
        if request.method in ('POST', 'PATCH'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Request origin differs from the dashboard host.'}, 403)
            length = request.headers.get('content-length', '')
            if length.isdigit() and int(length) > MAX_ARCHIVE_BYTES + 1024 * 1024:
                return JSONResponse({'detail': 'Request exceeds the upload limit.'}, 413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def get_project(project_id: int) -> dict:
        project = store.project(project_id)
        if project is None:
            raise HTTPException(404, 'Repository not found.')
        return project

    @contextmanager
    def github_client():
        try:
            with httpx.Client(timeout=18, headers={'User-Agent': 'code-review-agent-core'}) as http:
                yield http
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f'GitHub returned HTTP {exc.response.status_code}. Check the public repo, ref and rate limit.') from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, 'GitHub could not be reached.') from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def fetch_github(repo: str, ref: str) -> Snapshot:
        with github_client() as http:
            return github_snapshot(http, repo, ref)

    def snapshot_for_github_ref(project: dict, ref: str) -> tuple[int | Snapshot, Snapshot]:
        cached_id = store.snapshot_id_at_revision(project['id'], ref.lower()) if HEX_SHA.fullmatch(ref.lower()) else None
        if cached_id is not None:
            snapshot = store.snapshot(project['id'], cached_id)
            return cached_id, snapshot
        snapshot = fetch_github(project['github_repo'], ref)
        return snapshot, snapshot

    def auto_graphify(provider: str) -> tuple[str, str] | None:
        if provider == 'graphify':
            settings = providers.get()
            if settings.availability()['graphify_pilot']:
                return account(settings)
        return None

    def selected_snapshots(project_id: int, base_id: int, head_id: int):
        base = store.snapshot(project_id, base_id)
        head = store.snapshot(project_id, head_id)
        if base is None or head is None or base_id == head_id:
            raise HTTPException(400, 'Choose two different snapshots of this repository.')
        return base, head

    def make_diff(base: Snapshot, head: Snapshot):
        try:
            return snapshot_diff(base, head)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def snapshot_inventory(snapshot: Snapshot | None) -> dict:
        paths = inventory(snapshot.files) if snapshot else []
        return {'file_count': len(paths), 'line_count': sum(path['lines'] for path in paths), 'paths': paths[:100]}

    def get_session(project_id: int, session_id: int) -> dict:
        session = store.session(project_id, session_id)
        if session is None:
            raise HTTPException(404, 'Không tìm thấy phiên chat trong repository này.')
        return session

    def session_scope(session: dict, *, prepare: bool = False) -> list[dict]:
        settings = providers.get()
        graphify = session['provider'] == 'graphify'
        entries = []
        for position, item in enumerate([session, *session['related']]):
            repo = get_project(item['project_id'])
            head = store.snapshot_info(repo['id'], item['head_id'])
            base = store.snapshot_info(repo['id'], item['base_id']) if item['base_id'] is not None else None
            entry = {'key': f"repo_{repo['id']}" if position else 'primary', 'project_id': repo['id'],
                     'name': repo['name'], 'base': base, 'head': head}
            if graphify:
                status = pilot.status(settings, head['id'])
                if prepare and status['status'] in ('not_indexed', 'expired'):
                    pilot.enqueue(settings, head['id'])
                    status = pilot.status(settings, head['id'])
                if position and status['status'] == 'manual':
                    status = {'status': 'unconfigured', 'detail': 'Cần kết nối Pilot để index riêng repo này.'}
                entry['graphify'] = status
            elif session['provider'] == 'codelens':
                entry['codelens'] = bool(repo['codelens_project_id'] and repo['codelens_branch'])
            entries.append(entry)
        return entries

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/projects')
    def list_projects():
        return {'projects': store.projects(), 'available': providers.get().availability(),
                'can_configure_providers': bool(password)}

    @app.get('/api/provider-settings')
    def provider_settings():
        if not password:
            raise HTTPException(403, 'Set a dashboard password before managing provider settings.')
        return public_settings(providers.get())

    @app.patch('/api/provider-settings')
    async def update_provider_settings(request: Request):
        if not password:
            raise HTTPException(403, 'Set a dashboard password before managing provider settings.')
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 32 * 1024:
                raise HTTPException(413, 'Provider settings exceed the request limit.')
            payload.extend(chunk)
        try:
            patch = ProviderPatch.model_validate_json(payload)
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_context=False, include_url=False)
            detail = [error['msg'] for error in errors]
            raise HTTPException(422, ' · '.join(detail)) from None
        try:
            settings = await run_in_threadpool(providers.save, patch)
            pilot.wake.set()
            return public_settings(settings)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/projects', status_code=201)
    def create_project(body: ProjectRequest):
        name = (body.name or body.repo).strip()
        if not name:
            raise HTTPException(400, 'Enter a repository name.')
        repo, ref, base_ref = body.repo.strip(), body.ref.strip(), body.base_ref.strip()
        revisions = []
        if body.kind == 'github':
            if base_ref:
                revisions.append(fetch_github(repo, base_ref))
            revisions.append(fetch_github(repo, ref))
        project_id = store.create_project(name, body.kind, revisions, repo, ref, body.review_provider,
                                          auto_graphify(body.review_provider))
        pilot.wake.set()
        return {'id': project_id}

    @app.get('/api/projects/{project_id}')
    def project_detail(project_id: int):
        project = get_project(project_id)
        snapshots = store.snapshots(project_id)
        settings = providers.get()
        if project['kind'] != 'demo':
            for snapshot in snapshots:
                snapshot['graphify'] = pilot.status(settings, snapshot['id'])
        latest = store.snapshot(project_id, snapshots[0]['id']) if snapshots else None
        return {'project': project, 'snapshots': snapshots, 'reviews': store.reviews(project_id),
                'sessions': store.sessions(project_id),
                'inventory': snapshot_inventory(latest)}

    @app.post('/api/projects/{project_id}/refresh')
    def refresh(project_id: int):
        project = get_project(project_id)
        if project['kind'] != 'github':
            raise HTTPException(400, 'Refresh applies to GitHub repositories.')
        latest = fetch_github(project['github_repo'], project['ref'])
        changed = store.add_snapshot(project_id, latest, auto_graphify(project['review_provider']))
        pilot.wake.set()
        return {'changed': changed, 'revision': latest.revision}

    @app.get('/api/projects/{project_id}/commits')
    def commit_history(project_id: int, page: Annotated[int, Query(ge=1)] = 1,
                       anchor: Annotated[str | None, Query(pattern=r'^[0-9a-f]{40}$')] = None):
        project = get_project(project_id)
        if project['kind'] != 'github':
            raise HTTPException(400, 'Lịch sử commit chỉ áp dụng cho repository GitHub public.')
        if anchor is None:
            snapshots = store.snapshots(project_id)
            anchor = snapshots[0]['revision'] if snapshots else project['ref']
        with github_client() as http:
            result = github_commits(http, project['github_repo'], anchor, page)
        if not HEX_SHA.fullmatch(anchor) and result['commits']:
            anchor = result['commits'][0]['sha']
        return {**result, 'anchor': anchor if HEX_SHA.fullmatch(anchor) else None}

    @app.post('/api/projects/{project_id}/snapshots')
    async def upload(project_id: int, file: UploadFile, index: bool = True):
        project = get_project(project_id)
        if project['kind'] != 'zip':
            raise HTTPException(400, 'Upload snapshots to a ZIP repository.')
        try:
            snapshot = await run_in_threadpool(read_archive, await file.read(MAX_ARCHIVE_BYTES + 1))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()
        graphify_account = auto_graphify(project['review_provider']) if index else None
        changed = await run_in_threadpool(store.add_snapshot, project_id, snapshot, graphify_account)
        pilot.wake.set()
        return {'changed': changed, 'revision': snapshot.revision}

    @app.patch('/api/projects/{project_id}/mode')
    def set_mode(project_id: int, body: ModeRequest):
        if get_project(project_id)['kind'] == 'demo':
            raise HTTPException(400, 'Pricing demo uses its own fixture.')
        store.set_review_provider(project_id, body.provider)
        return {'provider': body.provider}

    @app.get('/api/projects/{project_id}/snapshots/{snapshot_id}/graphify')
    def graphify_status(project_id: int, snapshot_id: int):
        if not store.has_snapshot(project_id, snapshot_id):
            raise HTTPException(404, 'Snapshot not found.')
        return pilot.status(providers.get(), snapshot_id)

    @app.post('/api/projects/{project_id}/snapshots/{snapshot_id}/graphify', status_code=202)
    def provision_graphify(project_id: int, snapshot_id: int):
        if get_project(project_id)['kind'] == 'demo' or not store.has_snapshot(project_id, snapshot_id):
            raise HTTPException(404, 'Snapshot not found.')
        settings = providers.get()
        if not settings.availability()['graphify_pilot']:
            raise HTTPException(409, 'Cấu hình URL và tài khoản Graphify Pilot trước.')
        pilot.enqueue(settings, snapshot_id, retry=True)
        return pilot.status(settings, snapshot_id)

    @app.get('/api/projects/{project_id}/diff')
    def diff(project_id: int, base_id: int, head_id: int):
        base, head = selected_snapshots(project_id, base_id, head_id)
        review_input, changed = make_diff(base, head)
        return {'diff': review_input.diff, 'changed_files': changed}

    @app.patch('/api/projects/{project_id}/codelens')
    def configure_codelens(project_id: int, options: CodeLensRequest):
        get_project(project_id)
        store.update_codelens(project_id, options.project_id, options.branch.strip(), options.branch_id)
        return {'saved': True}

    @app.post('/api/projects/{project_id}/sessions', status_code=201)
    def create_session(project_id: int, body: SessionRequest):
        project = get_project(project_id)
        if (body.provider == 'demo') != (project['kind'] == 'demo') or (body.provider == 'demo' and body.related):
            raise HTTPException(400, 'Pricing demo dùng phiên review mẫu riêng.')
        ids = [project_id, *(item.project_id for item in body.related)]
        if len(set(ids)) != len(ids):
            raise HTTPException(400, 'Mỗi repository chỉ xuất hiện một lần trong phiên.')
        if body.base_ref:
            if project['kind'] != 'github':
                raise HTTPException(400, 'Nhập commit hoặc ref trực tiếp chỉ áp dụng cho repository GitHub public.')
            pinned_base, base = snapshot_for_github_ref(project, body.base_ref)
            pinned_head, head = snapshot_for_github_ref(project, body.head_ref)
            if base.revision == head.revision:
                raise HTTPException(400, 'Base và head đang trỏ tới cùng một commit.')
        else:
            pinned_base, pinned_head = body.base_id, body.head_id
            base, head = selected_snapshots(project_id, pinned_base, pinned_head)
        review_input, _ = make_diff(base, head)
        size = len(review_input.diff)
        for item in body.related:
            related = get_project(item.project_id)
            if related['kind'] == 'demo' or not store.has_snapshot(item.project_id, item.head_id):
                raise HTTPException(400, 'Chọn head thuộc repository liên quan.')
            if item.base_id is not None:
                related_base, related_head = selected_snapshots(item.project_id, item.base_id, item.head_id)
                size += len(make_diff(related_base, related_head)[0].diff)
        if size > MAX_DIFF_CHARS:
            raise HTTPException(400, 'Tổng diff của phiên vượt 40.000 ký tự.')
        title = body.title.strip() or f'Review {head.revision[:9]}'
        session_id = store.create_session(project_id, title, body.provider, pinned_base, pinned_head,
                                          [item.model_dump() for item in body.related])
        session = get_session(project_id, session_id)
        return {'session': session, 'scope': session_scope(session, prepare=True), 'reviews': []}

    @app.get('/api/projects/{project_id}/sessions/{session_id}')
    def session_detail(project_id: int, session_id: int):
        session = get_session(project_id, session_id)
        head = store.snapshot(project_id, session['head_id'])
        return {'session': session, 'scope': session_scope(session), 'reviews': store.reviews(project_id, session_id),
                'inventory': snapshot_inventory(head)}

    @app.post('/api/projects/{project_id}/sessions/{session_id}/prepare')
    def prepare_session(project_id: int, session_id: int):
        return {'scope': session_scope(get_session(project_id, session_id), prepare=True)}

    @app.get('/api/projects/{project_id}/sessions/{session_id}/scope')
    def session_scope_status(project_id: int, session_id: int):
        return {'scope': session_scope(get_session(project_id, session_id))}

    def execute_review(project: dict, base_id: int, head_id: int, selected_providers: list[str],
                       body: TurnRequest, session: dict | None = None):
        project_id = project['id']
        session_id = session['id'] if session else None
        base, head = selected_snapshots(project_id, base_id, head_id)
        if ('demo' in selected_providers and (project['kind'] != 'demo' or head.revision != DEMO_INPUT.head_sha)):
            raise HTTPException(400, 'The demo provider requires the seeded demo snapshot pair.')
        review_input, changed = make_diff(base, head)
        focus = body.focus.strip()
        review_input = replace(review_input, focus=focus)
        if body.follow_up_to:
            previous = store.review(project_id, body.follow_up_to)
            if not previous or previous['session_id'] != session_id or selected_providers != [previous['provider']] or (
                previous['result'].get('base_sha'), previous['result'].get('head_sha')
            ) != (base.revision, head.revision):
                raise HTTPException(400, 'Lượt trước phải thuộc cùng phiên, provider, base và head.')
            context = {key: previous['result'].get(key) for key in ('focus', 'findings', 'gaps')}
            review_input = replace(review_input, previous_review=json.dumps(context, ensure_ascii=False)[:6000])
        settings = providers.get()
        related_inputs = []
        related_targets = []
        scope = session_scope(session) if session else []
        if scope:
            scope[0]['changed_files'] = changed
        for item in scope[1:]:
            repo = get_project(item['project_id'])
            snapshot_id = item['head']['id']
            related_head = store.snapshot(repo['id'], snapshot_id)
            related_diff = ''
            base_sha = None
            item['changed_files'] = []
            if item['base']:
                related_base = store.snapshot(repo['id'], item['base']['id'])
                related_review, item['changed_files'] = make_diff(related_base, related_head)
                related_diff = related_review.diff
                base_sha = related_base.revision
            related_inputs.append(RelatedRepository(item['key'], repo['name'], related_head.revision, base_sha, related_diff))
            connection = None
            if item.get('graphify', {}).get('status') == 'ready':
                try:
                    connection = pilot.review_settings(settings, snapshot_id, related_head.revision)
                except ReviewUnavailable:
                    pass
            related_targets.append(RelatedTarget(item['key'], repo, related_head, connection))
        review_input = replace(review_input, related=tuple(related_inputs))
        records = []
        for provider in selected_providers:
            try:
                if provider == 'demo':
                    result = run_demo()
                else:
                    connection = pilot.review_settings(settings, head_id, head.revision) if provider == 'graphify' else settings
                    if related_targets:
                        result = live_review(connection, provider, project, head, review_input, related=tuple(related_targets))
                    else:
                        result = live_review(connection, provider, project, head, review_input)
            except (ReviewUnavailable, ValueError) as exc:
                result = {'status': 'incomplete', 'base_sha': base.revision, 'head_sha': head.revision,
                          'gaps': [str(exc)], 'findings': [], 'trace': [], 'model_calls': 0, 'tool_calls': 0}
            result['changed_files'] = changed
            if scope:
                result['scope'] = [
                    {key: item[key] for key in ('key', 'project_id', 'name', 'base', 'head', 'changed_files')}
                    for item in scope
                ]
            if focus:
                result['focus'] = focus
            if body.follow_up_to:
                result['follow_up_to'] = body.follow_up_to
            records.append(store.add_review(project_id, provider, result, session_id))
        return {'reviews': records}

    @app.post('/api/projects/{project_id}/review')
    def review(project_id: int, body: ReviewRequest):
        return execute_review(get_project(project_id), body.base_id, body.head_id,
                              list(dict.fromkeys(body.providers)), body)

    @app.post('/api/projects/{project_id}/sessions/{session_id}/turns')
    def session_turn(project_id: int, session_id: int, body: TurnRequest):
        session = get_session(project_id, session_id)
        return execute_review(get_project(project_id), session['base_id'], session['head_id'],
                              [session['provider']], body, session)

    return app


def serve(host: str, port: int, database: Path):
    password = os.environ.get('REVIEW_DASHBOARD_PASSWORD', '')
    if host not in ('127.0.0.1', 'localhost', '::1') and not password:
        raise SystemExit('Set REVIEW_DASHBOARD_PASSWORD to bind the dashboard outside loopback.')
    uvicorn.run(create_app(database, password=password), host=host, port=port)
