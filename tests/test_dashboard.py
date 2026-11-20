import io
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

if importlib.util.find_spec('fastapi') is None:
    raise unittest.SkipTest('Install the web extra to test the dashboard.')

import httpx
from fastapi.testclient import TestClient

from review_agent_core.dashboard import create_app
from review_agent_core.dashboard_providers import CodeLensLookup, LiveSettings
from review_agent_core.dashboard_sources import Snapshot, github_snapshot, read_archive


def zipped(files, sha=''):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        if sha:
            archive.comment = sha.encode()
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'reviews.sqlite3'
        self.browser = TestClient(create_app(self.database, LiveSettings()))
        self.addCleanup(self.browser.close)

    def create_zip_project(self):
        response = self.browser.post('/api/projects', json={'kind': 'zip', 'name': 'Pricing'})
        self.assertEqual(response.status_code, 201)
        project_id = response.json()['id']
        for sha, code in [('c' * 40, 'fee = 0\n'), ('d' * 40, 'fee = 1\n')]:
            response = self.browser.post(f'/api/projects/{project_id}/snapshots', files={
                'file': ('project.zip', zipped({'pricing.py': code, '.env': 'EXCLUDED=x'}, sha)),
            })
            self.assertEqual(response.json(), {'changed': True, 'revision': sha})
        return project_id

    def test_dashboard_demo_returns_findings_and_saves_history(self):
        home = self.browser.get('/')
        self.assertIn('Kết quả review', home.text)
        self.assertEqual(self.browser.get('/static/app.js').status_code, 200)
        project = self.browser.get('/api/projects/1').json()
        head, base = project['snapshots']
        response = self.browser.post('/api/projects/1/review', json={
            'base_id': base['id'], 'head_id': head['id'], 'providers': ['demo'],
        })
        result = response.json()['reviews'][0]['result']
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['findings'][0]['path'], 'reports/monthly.py')
        self.assertEqual(len(self.browser.get('/api/projects/1').json()['reviews']), 1)

    def test_pair_uses_same_diff_and_snapshot_for_both_providers(self):
        project_id = self.create_zip_project()
        detail = self.browser.get(f'/api/projects/{project_id}').json()
        self.assertEqual(detail['inventory']['file_count'], 1)
        head, base = detail['snapshots']
        result = {'status': 'complete', 'findings': [], 'gaps': [], 'trace': [], 'model_calls': 1, 'tool_calls': 1}
        with patch('review_agent_core.dashboard.live_review', side_effect=lambda *_: dict(result)) as run:
            response = self.browser.post(f'/api/projects/{project_id}/review', json={
                'base_id': base['id'], 'head_id': head['id'], 'providers': ['codelens', 'graphify'],
            })
        self.assertEqual([item['provider'] for item in response.json()['reviews']], ['codelens', 'graphify'])
        first, second = [call.args for call in run.call_args_list]
        self.assertEqual(first[3:], second[3:])
        self.assertIn('+fee = 1', first[4].diff)
        self.assertEqual(first[4].head_sha, 'd' * 40)

    def test_upload_rejects_zip_paths_outside_repository(self):
        response = self.browser.post('/api/projects', json={'kind': 'zip', 'name': 'Pricing'})
        project_id = response.json()['id']
        response = self.browser.post(f'/api/projects/{project_id}/snapshots', files={
            'file': ('invalid.zip', zipped({'../../escape.py': 'x'})),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.browser.get(f'/api/projects/{project_id}').json()['snapshots'], [])

    def test_refresh_saves_only_new_github_heads(self):
        base = Snapshot('a' * 40, {'pricing.py': 'fee = 0\n'}, 'github')
        head = Snapshot('b' * 40, {'pricing.py': 'fee = 1\n'}, 'github')
        with patch('review_agent_core.dashboard.github_snapshot', side_effect=[base, head, head]):
            response = self.browser.post('/api/projects', json={'kind': 'github', 'repo': 'owner/repo', 'ref': 'main'})
            self.assertEqual(response.status_code, 201)
            project_id = response.json()['id']
            prefix = f'/api/projects/{project_id}'
            self.assertTrue(self.browser.post(prefix + '/refresh').json()['changed'])
            self.assertFalse(self.browser.post(prefix + '/refresh').json()['changed'])
        self.assertEqual([item['revision'] for item in self.browser.get(prefix).json()['snapshots']],
                         [head.revision, base.revision])

    def test_rejects_cross_origin_mutations(self):
        response = self.browser.post('/api/projects', headers={'Origin': 'https://other.example'},
                                     json={'kind': 'zip', 'name': 'cross origin'})
        self.assertEqual(response.status_code, 403)

    def test_password_gates_dashboard_when_configured(self):
        with TestClient(create_app(self.database, LiveSettings(), 'local-password')) as protected:
            self.assertEqual(protected.get('/api/projects').status_code, 401)
            self.assertEqual(protected.get('/api/projects', auth=('review', 'local-password')).status_code, 200)


class ProviderContractTests(unittest.TestCase):
    def test_github_reads_archive_at_resolved_sha(self):
        sha = 'e' * 40

        def request(req):
            if req.url.host == 'api.github.com':
                self.assertIn('feature%2Fprice', str(req.url))
                return httpx.Response(200, json={'sha': sha})
            self.assertEqual(str(req.url), f'https://codeload.github.com/owner/repo/zip/{sha}')
            return httpx.Response(200, content=zipped({'repo-wrapper/src/pricing.py': 'fee = 1\n'}))

        with httpx.Client(transport=httpx.MockTransport(request)) as http:
            snapshot = github_snapshot(http, 'owner/repo', 'feature/price')
        self.assertEqual(snapshot.revision, sha)
        self.assertEqual(snapshot.files, {'src/pricing.py': 'fee = 1\n'})

    def test_codelens_legacy_context_checks_branch_status_at_both_ends(self):
        sha = 'f' * 40
        calls = []

        def request(req):
            calls.append(req.method)
            if req.method == 'GET':
                self.assertTrue(req.url.path.endswith('/branches/8/status/'))
                return httpx.Response(200, json={'head_sha': sha, 'status': 'ready', 'branch': 'main'})
            self.assertEqual(json.loads(req.content), {'project_id': 24, 'branch': 'main', 'targets': [{'path': 'pricing.py'}]})
            return httpx.Response(200, json={'namespace': 'project_24_main', '_meta': {},
                                            'targets_found': [], 'callees': [], 'related_files': [],
                                            'callers': [{'file': 'report.py'}]})

        with httpx.Client(transport=httpx.MockTransport(request)) as http:
            lookup = CodeLensLookup(http, 'http://local.test/api/v1/rag', 24, 'main', sha, 8)
            result = lookup('pricing.py')
        self.assertEqual(calls, ['GET', 'POST', 'GET'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['revision_evidence'], 'codelens_branch_status')

    def test_codelens_avoids_context_from_wrong_index(self):
        def request(req):
            self.assertEqual(req.method, 'GET')
            return httpx.Response(200, json={'head_sha': 'a' * 40, 'status': 'ready', 'branch': 'main'})

        with httpx.Client(transport=httpx.MockTransport(request)) as http:
            lookup = CodeLensLookup(http, 'http://local.test/api/v1/rag', 24, 'main', 'b' * 40, 8)
            result = lookup('pricing.py')
        self.assertEqual(result['reason'], 'graph_revision_mismatch')
        self.assertNotIn('data', result)

    def test_git_archive_revision_and_source_are_preserved(self):
        snapshot = read_archive(zipped({'file.py': 'x = 1\n'}, 'd' * 40))
        self.assertEqual(snapshot.provenance, 'git_archive')
        self.assertEqual(snapshot.revision, 'd' * 40)
