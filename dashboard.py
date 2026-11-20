"""Local dashboard for repository snapshots, reviews, and provider comparison."""

import base64
import binascii
import os
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .dashboard_providers import LiveSettings, live_review
from .dashboard_settings import ProviderPatch, ProviderSettings, public_settings
from .dashboard_sources import MAX_ARCHIVE_BYTES, Snapshot, github_snapshot, inventory, read_archive, snapshot_diff
from .dashboard_store import Store
from .demo import CALLER_FILE, DEMO_INPUT, SIGNATURE_FILE, demo_source, run_demo
from .graph import ReviewUnavailable

STATIC = Path(__file__).parent / 'static'


class ProjectRequest(BaseModel):
    name: str = Field(default='', max_length=100)
    kind: Literal['github', 'zip']
    repo: str = Field(default='', max_length=170)
    ref: str = Field(default='main', min_length=1, max_length=180)
    base_ref: str = Field(default='', max_length=180)


class CodeLensRequest(BaseModel):
    project_id: int | None = Field(default=None, gt=0)
    branch: str = Field(default='', max_length=180)
    branch_id: int | None = Field(default=None, gt=0)


class ReviewRequest(BaseModel):
    base_id: int = Field(gt=0)
    head_id: int = Field(gt=0)
    providers: list[Literal['codelens', 'graphify', 'demo']] = Field(min_length=1, max_length=2)


def _demo_snapshots() -> list[Snapshot]:
    common = {CALLER_FILE: demo_source(CALLER_FILE)}
    return [
        Snapshot(DEMO_INPUT.base_sha, {**common, SIGNATURE_FILE: 'def total(items):\n    return sum(items)\n'}, 'fixture'),
        Snapshot(DEMO_INPUT.head_sha, {**common, SIGNATURE_FILE: 'def total(items, fee):\n    return sum(items) + fee\n'}, 'fixture'),
    ]


def create_app(db_path: Path, settings: LiveSettings | None = None, password: str = '') -> FastAPI:
    store = Store(db_path)
    providers = ProviderSettings(store, settings or LiveSettings.from_env())
    if not store.projects():
        store.create_project('Pricing demo', 'demo', _demo_snapshots())
    app = FastAPI(title='Code Review Agent', docs_url=None, redoc_url=None, openapi_url=None)
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

    def fetch_github(repo: str, ref: str) -> Snapshot:
        try:
            with httpx.Client(timeout=18, headers={'User-Agent': 'code-review-agent-core'}) as http:
                return github_snapshot(http, repo, ref)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f'GitHub returned HTTP {exc.response.status_code}. Check the public repo, ref and rate limit.') from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, 'GitHub could not be reached.') from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

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
            return public_settings(await run_in_threadpool(providers.save, patch))
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
        return {'id': store.create_project(name, body.kind, revisions, repo, ref)}

    @app.get('/api/projects/{project_id}')
    def project_detail(project_id: int):
        project = get_project(project_id)
        snapshots = store.snapshots(project_id)
        latest = store.snapshot(project_id, snapshots[0]['id']) if snapshots else None
        paths = inventory(latest.files) if latest else []
        return {'project': project, 'snapshots': snapshots, 'reviews': store.reviews(project_id),
                'inventory': {'file_count': len(paths), 'line_count': sum(path['lines'] for path in paths),
                              'paths': paths[:100]}}

    @app.post('/api/projects/{project_id}/refresh')
    def refresh(project_id: int):
        project = get_project(project_id)
        if project['kind'] != 'github':
            raise HTTPException(400, 'Refresh applies to GitHub repositories.')
        latest = fetch_github(project['github_repo'], project['ref'])
        return {'changed': store.add_snapshot(project_id, latest), 'revision': latest.revision}

    @app.post('/api/projects/{project_id}/snapshots')
    async def upload(project_id: int, file: UploadFile):
        if get_project(project_id)['kind'] != 'zip':
            raise HTTPException(400, 'Upload snapshots to a ZIP repository.')
        try:
            snapshot = read_archive(await file.read(MAX_ARCHIVE_BYTES + 1))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()
        return {'changed': store.add_snapshot(project_id, snapshot), 'revision': snapshot.revision}

    @app.patch('/api/projects/{project_id}/codelens')
    def configure_codelens(project_id: int, options: CodeLensRequest):
        get_project(project_id)
        store.update_codelens(project_id, options.project_id, options.branch.strip(), options.branch_id)
        return {'saved': True}

    @app.post('/api/projects/{project_id}/review')
    def review(project_id: int, body: ReviewRequest):
        project = get_project(project_id)
        base = store.snapshot(project_id, body.base_id)
        head = store.snapshot(project_id, body.head_id)
        if base is None or head is None or body.base_id == body.head_id:
            raise HTTPException(400, 'Choose two different snapshots of this repository.')
        selected_providers = list(dict.fromkeys(body.providers))
        if ('demo' in selected_providers and (project['kind'] != 'demo' or head.revision != DEMO_INPUT.head_sha)):
            raise HTTPException(400, 'The demo provider requires the seeded demo snapshot pair.')
        try:
            review_input, changed = snapshot_diff(base, head)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        settings = providers.get()
        records = []
        for provider in selected_providers:
            try:
                result = run_demo() if provider == 'demo' else live_review(settings, provider, project, head, review_input)
            except (ReviewUnavailable, ValueError) as exc:
                result = {'status': 'incomplete', 'base_sha': base.revision, 'head_sha': head.revision,
                          'gaps': [str(exc)], 'findings': [], 'trace': [], 'model_calls': 0, 'tool_calls': 0}
            result['changed_files'] = changed
            records.append(store.add_review(project_id, provider, result))
        return {'reviews': records}

    return app


def serve(host: str, port: int, database: Path):
    password = os.environ.get('REVIEW_DASHBOARD_PASSWORD', '')
    if host not in ('127.0.0.1', 'localhost', '::1') and not password:
        raise SystemExit('Set REVIEW_DASHBOARD_PASSWORD to bind the dashboard outside loopback.')
    uvicorn.run(create_app(database, password=password), host=host, port=port)
