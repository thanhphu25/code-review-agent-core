"""Model and code graph adapters for live reviews."""

import json
import os
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from time import perf_counter

import httpx
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import OpenAI, OpenAIError

from .dashboard_sources import Snapshot
from .graph import ModelReply, RepositoryTools, ReviewInput, ReviewUnavailable, run_review


@dataclass(frozen=True)
class LiveSettings:
    model_url: str = ''
    model_key: str = ''
    model: str = ''
    codelens_url: str = ''
    codelens_token: str = ''
    graphify_url: str = ''
    graphify_token: str = ''
    graphify_sha: str = ''
    graphify_pilot_url: str = ''
    graphify_pilot_user: str = ''
    graphify_pilot_password: str = ''
    graphify_revision_evidence: str = 'graphify_operator_archive_sha'

    @classmethod
    def from_env(cls):
        def get(name: str) -> str:
            return os.environ.get(name, '').strip()

        return cls(
            model_url=get('REVIEW_AGENT_LLM_BASE_URL') or get('CODEDECK_AGENT_LLM_BASE_URL'),
            model_key=get('REVIEW_AGENT_LLM_API_KEY') or get('CODEDECK_AGENT_LLM_API_KEY'),
            model=get('REVIEW_AGENT_LLM_MODEL') or get('CODEDECK_AGENT_LLM_MODEL'),
            codelens_url=get('CODELENS_API_BASE'), codelens_token=get('CODELENS_API_TOKEN'),
            graphify_url=get('GRAPHIFY_MCP_URL'), graphify_token=get('GRAPHIFY_API_KEY'),
            graphify_sha=get('GRAPHIFY_INDEXED_SHA').lower(),
            graphify_pilot_url=get('GRAPHIFY_PILOT_URL'),
            graphify_pilot_user=get('GRAPHIFY_PILOT_USER'),
            graphify_pilot_password=os.environ.get('GRAPHIFY_PILOT_PASSWORD', ''),
        )

    def availability(self) -> dict:
        return {'model': bool(self.model_key and self.model_url and self.model),
                'codelens': bool(self.codelens_url and self.codelens_token),
                'graphify': bool(self.graphify_url and self.graphify_token),
                'graphify_pilot': bool(self.graphify_pilot_url and self.graphify_pilot_user and self.graphify_pilot_password)}


def _context_result(expected: str, indexed: str | None, content: str, evidence: str) -> dict:
    if indexed and indexed != expected:
        return {'status': 'error', 'reason': 'graph_revision_mismatch', 'sha': indexed}
    data = {'context': content[:8000], 'truncated': len(content) > 8000, 'revision_evidence': evidence}
    if not indexed:
        return {'status': 'unverified', 'reason': 'graph_revision_missing', 'data': data}
    return {'status': 'ok', 'sha': indexed, 'data': data}


@dataclass(frozen=True)
class CodeLensLookup:
    http: httpx.Client
    lookup_url: str
    project_id: int
    branch: str
    expected: str
    branch_id: int | None = None

    def branch_sha(self) -> str | None:
        if self.branch_id is None:
            return None
        response = self.http.get(f'{self.lookup_url}/branches/{self.branch_id}/status/')
        response.raise_for_status()
        payload = response.json()
        if payload.get('branch') != self.branch or payload.get('status') != 'ready':
            raise ReviewUnavailable('codelens_branch_not_ready')
        sha = payload.get('head_sha')
        if not isinstance(sha, str) or not sha:
            raise ReviewUnavailable('codelens_branch_sha_missing')
        return sha

    def __call__(self, path: str) -> dict:
        try:
            before = self.branch_sha()
            if before and before != self.expected:
                return _context_result(self.expected, before, '', 'codelens_branch_status')
            response = self.http.post(f'{self.lookup_url}/context/', json={
                'project_id': self.project_id, 'branch': self.branch, 'targets': [{'path': path}],
            })
            response.raise_for_status()
            payload = response.json()
            meta = payload.get('_meta') or {}
            if payload.get('error') or meta.get('error') or payload.get('degraded') or meta.get('degraded'):
                raise ReviewUnavailable('codelens_context_unavailable')
            namespace = payload.get('namespace')
            if namespace and namespace != f'project_{self.project_id}_{self.branch}':
                raise ReviewUnavailable('codelens_namespace_mismatch')
            after = self.branch_sha()
            if before != after:
                return {'status': 'error', 'reason': 'codelens_index_changed', 'sha': after}
            response_sha = meta.get('indexed_sha') or payload.get('indexed_sha')
            data = {}
            for key in ('targets_found', 'callers', 'callees', 'related_files'):
                records = payload.get(key)
                if not isinstance(records, list):
                    raise ReviewUnavailable('invalid_codelens_payload')
                data[key] = records[:12]
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ReviewUnavailable(f'codelens_{type(exc).__name__}') from exc
        version = response_sha or after
        evidence = 'codelens_response' if response_sha else ('codelens_branch_status' if after else 'none')
        return _context_result(self.expected, version, json.dumps(data, ensure_ascii=False), evidence)


@asynccontextmanager
async def _graphify_session(url: str, token: str):
    async with httpx.AsyncClient(headers={'Authorization': f'Bearer {token}'}, timeout=20) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, _session_id):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                yield session


@contextmanager
def graphify_lookup(settings: LiveSettings, expected: str):
    if settings.graphify_sha and settings.graphify_sha != expected:
        yield lambda _path: _context_result(expected, settings.graphify_sha, '', settings.graphify_revision_evidence)
        return
    with start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(_graphify_session(settings.graphify_url, settings.graphify_token)) as session:
            def lookup(path):
                try:
                    result = portal.call(partial(session.call_tool, 'query_graph', {
                        'question': f'Callers, dependencies and definitions for {path}', 'depth': 2, 'token_budget': 2000,
                    }))
                except Exception as exc:
                    raise ReviewUnavailable(f'graphify_{type(exc).__name__}') from exc
                if result.isError:
                    raise ReviewUnavailable('graphify_query_failed')
                data = result.structuredContent or {}
                text = '\n'.join(item.text for item in result.content if item.type == 'text')
                if not text and not data:
                    raise ReviewUnavailable('graphify_empty_response')
                remote_sha = data.get('indexed_sha')
                version = remote_sha or settings.graphify_sha or None
                evidence = 'graphify_response' if remote_sha else (settings.graphify_revision_evidence if version else 'none')
                return _context_result(expected, version, text or json.dumps(data), evidence)

            yield lookup


@dataclass(frozen=True)
class GatewayReviewer:
    client: OpenAI
    model: str

    def __call__(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        try:
            response = self.client.chat.completions.create(model=self.model, messages=messages, tools=tools)
        except OpenAIError as exc:
            raise ReviewUnavailable(f'model_{type(exc).__name__}') from exc
        if not response.choices:
            raise ReviewUnavailable('model_no_choices')
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        if any(call.type != 'function' for call in tool_calls):
            raise ReviewUnavailable('model_unsupported_tool_call')
        message = {'role': 'assistant', 'content': choice.message.content}
        if tool_calls:
            message['tool_calls'] = [call.model_dump(exclude_none=True) for call in tool_calls]
        usage = response.usage
        return ModelReply(message, choice.finish_reason, {'total_tokens': usage.total_tokens} if usage else None)


@dataclass(frozen=True)
class RelatedTarget:
    key: str
    project: dict
    head: Snapshot
    graphify: LiveSettings | None = None


def snapshot_tools(head: Snapshot, lookup) -> RepositoryTools:
    def read_source(path):
        if path not in head.files:
            raise ReviewUnavailable('file_absent_at_head')
        return head.files[path]

    def search_source(query):
        needle = query.casefold()
        matches = []
        for path, text in sorted(head.files.items()):
            for number, line in enumerate(text.splitlines(), 1):
                position = line.casefold().find(needle)
                if position >= 0:
                    start = max(0, position - 80)
                    matches.append({'path': path, 'line': number, 'text': line[start:start + 240]})
                    if len(matches) == 12:
                        return matches
        return matches

    return RepositoryTools(read_source, lookup, search_source)


def _codelens_lookup(http: httpx.Client, settings: LiveSettings, project: dict, revision: str):
    if not project['codelens_project_id'] or not project['codelens_branch']:
        def unavailable(_path):
            raise ReviewUnavailable('codelens_repository_not_configured')
        return unavailable
    return CodeLensLookup(http, settings.codelens_url.rstrip('/'), project['codelens_project_id'],
                          project['codelens_branch'], revision, project['codelens_branch_id'])


def _related_graphify_lookup(stack: ExitStack, target: RelatedTarget):
    lookup = None

    def query(path):
        nonlocal lookup
        if target.graphify is None:
            raise ReviewUnavailable('graphify_related_index_required')
        if lookup is None:
            try:
                lookup = stack.enter_context(graphify_lookup(target.graphify, target.head.revision))
            except (httpx.HTTPError, ExceptionGroup, TimeoutError) as exc:
                raise ReviewUnavailable(f'graphify_session_{type(exc).__name__}') from exc
        return lookup(path)
    return query


def live_review(settings: LiveSettings, provider: str, project: dict, head: Snapshot, review_input: ReviewInput,
                *, related: tuple[RelatedTarget, ...] = ()) -> dict:
    if not settings.availability()['model'] or not settings.availability().get(provider):
        raise ValueError('Configure the model and selected provider in the server environment first.')

    model_url = settings.model_url.rstrip('/')
    if not model_url.endswith('/v1'):
        model_url += '/v1'
    started = perf_counter()
    try:
        with ExitStack() as stack:
            model = stack.enter_context(OpenAI(api_key=settings.model_key, base_url=model_url, timeout=25, max_retries=0))
            if provider == 'codelens':
                if not project['codelens_project_id'] or not project['codelens_branch']:
                    raise ValueError('Set the CodeLens project ID and branch for this repository.')
                http = stack.enter_context(httpx.Client(
                    headers={'Authorization': f'Token {settings.codelens_token}'}, timeout=10,
                ))
                lookup = _codelens_lookup(http, settings, project, head.revision)
                related_tools = {target.key: snapshot_tools(target.head, _codelens_lookup(
                    http, settings, target.project, target.head.revision,
                )) for target in related}
            else:
                lookup = stack.enter_context(graphify_lookup(settings, head.revision))
                related_tools = {target.key: snapshot_tools(target.head, _related_graphify_lookup(stack, target))
                                 for target in related}
            primary = snapshot_tools(head, lookup)
            result = run_review(review_input, GatewayReviewer(model, settings.model), primary.read, primary.lookup,
                                search_source=primary.search, related_tools=related_tools)
    except (httpx.HTTPError, ExceptionGroup, TimeoutError) as exc:
        if provider != 'graphify':
            raise
        raise ReviewUnavailable(f'graphify_session_{type(exc).__name__}') from exc
    tokens = [step['usage']['total_tokens'] for step in result['trace'] if step.get('usage')]
    graph_steps = [step for step in result['trace'] if step.get('tool') == 'get_code_context']
    result.update(model=settings.model, elapsed_ms=round((perf_counter() - started) * 1000),
                  tokens=sum(tokens) if len(tokens) == result['model_calls'] else None,
                  graph_calls=len(graph_steps), revision_matched_calls=sum(step['status'] == 'ok' for step in graph_steps))
    return result
