import io
import importlib.util
import json
import stat
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
from review_agent_core.dashboard_graphify import PilotIndexer
from review_agent_core.dashboard_providers import CodeLensLookup, LiveSettings
from review_agent_core.dashboard_settings import ProviderSettings
from review_agent_core.dashboard_sources import Snapshot, github_commits, github_snapshot, read_archive
from review_agent_core.dashboard_store import Store


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

    def test_provider_settings_require_password_and_reject_cross_origin(self):
        path = '/api/provider-settings'
        self.assertEqual(self.browser.get(path).status_code, 403)
        self.assertEqual(self.browser.patch(path, json={'model_key': 'sample'}).status_code, 403)
        with TestClient(create_app(self.database, LiveSettings(), 'local-password')) as protected:
            self.assertEqual(protected.get(path).status_code, 401)
            self.assertEqual(protected.patch(path, json={'model_key': 'sample'}).status_code, 401)
            response = protected.patch(path, auth=('review', 'local-password'),
                                       headers={'Origin': 'http://another.test'}, json={'model_key': 'sample'})
            self.assertEqual(response.status_code, 403)

    def test_provider_settings_persist_and_apply_to_reviews(self):
        project_id = self.create_zip_project()
        prefix = f'/api/projects/{project_id}'
        head, base = self.browser.get(prefix).json()['snapshots']
        auth = ('review', 'local-password')
        path = '/api/provider-settings'
        changes = {'model_url': 'https://gateway.test/v1', 'model': 'review-model', 'model_key': 'model-secret',
                   'graphify_url': 'http://127.0.0.1:18081/mcp/project', 'graphify_token': 'graph-secret',
                   'graphify_sha': 'D' * 40, 'codelens_url': 'https://codelens.test/api/v1/rag',
                   'codelens_token': 'lens-secret'}
        with TestClient(create_app(self.database, LiveSettings(), auth[1])) as protected:
            response = protected.patch(path, auth=auth, json=changes)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['available'],
                             {'model': True, 'graphify': True, 'codelens': True, 'graphify_pilot': False})
            self.assertEqual(response.json()['fields']['graphify_sha'], 'd' * 40)
            for key in ('model_key', 'graphify_token', 'codelens_token'):
                self.assertNotIn(changes[key], response.text)
            self.assertEqual(protected.patch(path, auth=auth, json={'model': 'updated-model'}).status_code, 200)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        with TestClient(create_app(self.database, LiveSettings(model_key='environment-key'), auth[1])) as restarted:
            self.assertTrue(restarted.get(path, auth=auth).json()['has_key']['model_key'])
            with patch('review_agent_core.dashboard.live_review', return_value={}) as run:
                response = restarted.post(prefix + '/review', auth=auth, json={
                    'base_id': base['id'], 'head_id': head['id'], 'providers': ['graphify'],
                })
            self.assertEqual(response.status_code, 200, response.text)
            settings = run.call_args.args[0]
            self.assertEqual((settings.model, settings.model_key, settings.graphify_token),
                             ('updated-model', 'model-secret', 'graph-secret'))

    def test_changes_to_endpoint_cannot_reuse_saved_key_implicitly(self):
        path = '/api/provider-settings'
        auth = ('review', 'local-password')
        defaults = LiveSettings(graphify_url='http://127.0.0.1:18081/mcp/first', graphify_token='env-token',
                                graphify_sha='a' * 40)
        new_url = 'http://127.0.0.1:18081/mcp/second'
        with TestClient(create_app(self.database, defaults, auth[1])) as protected:
            response = protected.patch(path, auth=auth, json={'graphify_url': new_url})
            self.assertEqual(response.status_code, 400)
            response = protected.patch(path, auth=auth, json={'graphify_url': new_url, 'graphify_token': 'invalid secret'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('invalid secret', response.text)
            response = protected.patch(path, auth=auth, json={'graphify_url': new_url, 'graphify_token': ''})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['has_key']['graphify_token'])
            self.assertEqual(response.json()['fields']['graphify_sha'], '')
            self.assertEqual(response.json()['fields']['graphify_url'], new_url)

    def test_graphify_auto_indexes_only_head_and_reuses_project_after_restart(self):
        settings = LiveSettings(graphify_pilot_url='http://pilot.test', graphify_pilot_user='tester',
                                graphify_pilot_password='pilot-password', model='test-model')
        store = Store(self.database)
        indexer = PilotIndexer(store, ProviderSettings(store, settings))
        account = (settings.graphify_pilot_url, settings.graphify_pilot_user)
        browser = TestClient(create_app(self.database, settings))
        self.addCleanup(browser.close)
        info = browser.post('/api/projects', json={'name': 'Auto index', 'kind': 'zip'})
        prefix = f"/api/projects/{info.json()['id']}"
        base_sha, head_sha = 'a' * 40, 'b' * 40
        for sha, path, code, enabled in [
            (base_sha, 'src/main.py', 'value = 1', 'false'),
            (head_sha, 'src/main.py', 'value = 2', 'true'),
        ]:
            archive = zipped({path: code, 'src/helper.py': 'def read(): return 1', '.env': 'HIDDEN=secret'}, sha)
            response = browser.post(prefix + f'/snapshots?index={enabled}', files={'file': ('repo.zip', archive)})
            self.assertEqual(response.status_code, 200, response.text)
        head, base = browser.get(prefix).json()['snapshots']
        self.assertEqual((head['graphify']['status'], base['graphify']['status']), ('queued', 'not_indexed'))

        projects = []
        uploads = []
        def pilot(request):
            path = request.url.path
            if path == '/api/login':
                return httpx.Response(200, headers={'Set-Cookie': 'pilot=session; Path=/'}, json={'ok': True})
            if request.method == 'GET':
                return httpx.Response(200, json=projects)
            if path == '/api/projects':
                with zipfile.ZipFile(io.BytesIO(request.content)) as sent:
                    uploads.append(sent.namelist())
                projects.append({'id': 'pilot-project', 'title': request.url.params['title'], 'status': 'ready'})
                return httpx.Response(202, json={'id': 'pilot-project'})
            self.assertEqual(path, '/api/projects/pilot-project/token')
            return httpx.Response(200, json={'token': 'private-mcp-token'})

        with httpx.Client(base_url=account[0], transport=httpx.MockTransport(pilot)) as client:
            job = store.claim_graphify(*account)
            self.assertEqual(job['snapshot_id'], head['id'])
            indexer.advance(client, account[1], settings.graphify_pilot_password, job)
            store.update_graphify(job['id'], 'indexing', remote_id='pilot-project')
            indexer.advance(client, account[1], settings.graphify_pilot_password, store.claim_graphify(*account))
        self.assertEqual(uploads, [['repository/src/helper.py', 'repository/src/main.py']])

        with TestClient(create_app(self.database, settings)) as restarted:
            status_url = prefix + f"/snapshots/{head['id']}/graphify"
            status = restarted.post(status_url)
            self.assertEqual(status.json()['status'], 'ready')
            self.assertNotIn('private-mcp-token', status.text)
            self.assertIsNone(store.claim_graphify(*account))
            payload = {'base_id': base['id'], 'head_id': head['id'], 'providers': ['graphify'],
                       'focus': 'Kiểm tra caller'}
            with patch('review_agent_core.dashboard.live_review',
                       return_value={'base_sha': base_sha, 'head_sha': head_sha, 'findings': [], 'gaps': []}) as run:
                review = restarted.post(prefix + '/review', json=payload).json()['reviews'][0]
                connection = run.call_args.args[0]
                self.assertEqual(connection.graphify_url, 'http://pilot.test/mcp/pilot-project')
                self.assertEqual(connection.graphify_token, 'private-mcp-token')
                self.assertEqual(connection.graphify_sha, head_sha)
                self.assertEqual(connection.graphify_revision_evidence, 'graphify_agent_upload')
                self.assertEqual(run.call_args.args[4].focus, payload['focus'])
                follow_up = restarted.post(prefix + '/review', json={**payload, 'follow_up_to': review['id']})
                self.assertEqual(follow_up.status_code, 200, follow_up.text)
                self.assertIn('Kiểm tra caller', run.call_args.args[4].previous_review)

    def test_codelens_mode_does_not_send_zip_to_graphify(self):
        browser = TestClient(create_app(self.database, LiveSettings(graphify_pilot_url='http://pilot.test',
                                   graphify_pilot_user='tester', graphify_pilot_password='pilot-password')))
        self.addCleanup(browser.close)
        project = browser.post('/api/projects', json={
            'name': 'CodeLens project', 'kind': 'zip', 'review_provider': 'codelens',
        }).json()
        prefix = f"/api/projects/{project['id']}"
        for code in ('value = 1', 'value = 2'):
            response = browser.post(prefix + '/snapshots', files={'file': ('repo.zip', zipped({'main.py': code}))})
            self.assertEqual(response.status_code, 200)
        detail = browser.get(prefix).json()
        self.assertEqual(detail['project']['review_provider'], 'codelens')
        head, base = detail['snapshots']
        self.assertEqual(head['graphify']['status'], 'not_indexed')
        diff = browser.get(prefix + '/diff', params={'base_id': base['id'], 'head_id': head['id']})
        self.assertEqual(diff.json()['changed_files'], ['main.py'])
        self.assertIn('+value = 2', diff.json()['diff'])

    def test_session_pins_related_diffs_and_isolates_followups_across_restart(self):
        primary = self.create_zip_project()
        related = self.create_zip_project()
        prefix = f'/api/projects/{primary}'
        nested = f'/api/projects/{related}'
        primary_head, primary_base = self.browser.get(prefix).json()['snapshots']
        related_head, related_base = self.browser.get(nested).json()['snapshots']
        payload = {'title': 'API change', 'provider': 'codelens', 'base_id': primary_base['id'],
                   'head_id': primary_head['id'], 'related': [
                       {'project_id': related, 'base_id': related_base['id'], 'head_id': related_head['id']},
                   ]}
        bad = self.browser.post(prefix + '/sessions', json={
            **payload, 'related': [{'project_id': related, 'head_id': primary_head['id']}],
        })
        self.assertEqual(bad.status_code, 400)
        first = self.browser.post(prefix + '/sessions', json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        session = first.json()['session']
        self.assertEqual(first.json()['scope'][1]['head']['id'], related_head['id'])
        first_path = prefix + f"/sessions/{session['id']}"
        second = self.browser.post(prefix + '/sessions', json={**payload, 'title': 'Fresh review', 'related': []})
        second_path = prefix + f"/sessions/{second.json()['session']['id']}"

        with patch('review_agent_core.dashboard.live_review') as run:
            run.return_value = {'base_sha': primary_base['revision'], 'head_sha': primary_head['revision'],
                                'findings': [], 'gaps': []}
            response = self.browser.post(first_path + '/turns', json={'focus': 'Check callers'})
            self.assertEqual(response.status_code, 200, response.text)
            review = response.json()['reviews'][0]
            self.assertEqual(review['session_id'], session['id'])
            self.assertIn('+fee = 1', run.call_args.args[4].related[0].diff)
            self.assertEqual(run.call_args.kwargs['related'][0].project['id'], related)

        newer = self.browser.post(prefix + '/snapshots', files={
            'file': ('new-head.zip', zipped({'new_file.py': 'pass\n'}, 'e' * 40)),
        })
        self.assertEqual(newer.status_code, 200)
        with TestClient(create_app(self.database, LiveSettings())) as restarted:
            reopened = restarted.get(first_path).json()
            self.assertEqual([record['id'] for record in reopened['reviews']], [review['id']])
            self.assertEqual(reopened['scope'][1]['base']['id'], related_base['id'])
            self.assertEqual([item['path'] for item in reopened['inventory']['paths']], ['pricing.py'])
            self.assertEqual(restarted.get(second_path).json()['reviews'], [])
            response = restarted.post(second_path + '/turns', json={'follow_up_to': review['id']})
            self.assertEqual(response.status_code, 400)

    def test_github_session_can_import_a_commit_pair_independently_of_tracked_branch(self):
        tracked = Snapshot('f' * 40, {'pricing.py': 'fee = 9\n'}, 'github')
        base = Snapshot('a' * 40, {'pricing.py': 'fee = 1\n'}, 'github')
        head = Snapshot('d' * 40, {'pricing.py': 'fee = 3\n'}, 'github')
        settings = LiveSettings(graphify_pilot_url='http://pilot.test', graphify_pilot_user='tester',
                                graphify_pilot_password='pilot-password')
        browser = TestClient(create_app(self.database, settings))
        self.addCleanup(browser.close)
        with patch('review_agent_core.dashboard.github_snapshot', side_effect=[tracked, base, head]) as fetch:
            created = browser.post('/api/projects', json={
                'kind': 'github', 'repo': 'owner/repo', 'ref': 'main', 'review_provider': 'codelens',
            })
            prefix = f"/api/projects/{created.json()['id']}"
            prefix_ref = prefix + '/sessions'
            response = browser.post(prefix_ref, json={
                'provider': 'graphify', 'base_ref': ' v1.0.0 ', 'head_ref': 'd123456',
            })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual([call.args[1:] for call in fetch.call_args_list],
                         [('owner/repo', 'main'), ('owner/repo', 'v1.0.0'), ('owner/repo', 'd123456')])
        data = response.json()
        self.assertEqual(data['scope'][0]['base']['revision'], base.revision)
        self.assertEqual(data['scope'][0]['head']['revision'], head.revision)
        self.assertEqual(data['scope'][0]['graphify']['status'], 'queued')
        detail = browser.get(prefix).json()
        self.assertEqual(detail['project']['ref'], 'main')
        self.assertEqual(detail['snapshots'][0]['revision'], tracked.revision)
        self.assertEqual({snapshot['revision'] for snapshot in detail['snapshots']},
                         {tracked.revision, base.revision, head.revision})
        params = {key: data['session'][key] for key in ('base_id', 'head_id')}
        diff = browser.get(prefix + '/diff', params=params).json()['diff']
        self.assertIn('-fee = 1\n+fee = 3', diff)

        with patch('review_agent_core.dashboard.github_snapshot', return_value=base):
            equal = browser.post(prefix_ref, json={'provider': 'graphify', 'base_ref': 'tag', 'head_ref': 'branch'})
        self.assertEqual(equal.status_code, 400)
        self.assertEqual(len(browser.get(prefix).json()['sessions']), 1)

    def test_commit_history_uses_metadata_and_reuses_the_saved_head(self):
        head = Snapshot('f' * 40, {'pricing.py': 'fee = 9\n'}, 'github')
        base = Snapshot('d' * 40, {'pricing.py': 'fee = 3\n'}, 'github')
        listing = {'commits': [{'sha': head.revision, 'parent_sha': base.revision}], 'has_more': True}
        with patch('review_agent_core.dashboard.github_snapshot', side_effect=[head, base]) as fetch:
            created = self.browser.post('/api/projects', json={'kind': 'github', 'repo': 'owner/repo', 'ref': 'main'})
            prefix = f"/api/projects/{created.json()['id']}"
            with patch('review_agent_core.dashboard.github_commits', return_value=listing) as history:
                first = self.browser.get(prefix + '/commits')
                self.assertEqual(first.json()['anchor'], head.revision)
                self.browser.get(prefix + '/commits', params={'page': 2, 'anchor': head.revision})
            self.assertEqual([call.args[1:] for call in history.call_args_list],
                             [('owner/repo', head.revision, 1), ('owner/repo', head.revision, 2)])
            self.assertEqual(len(self.browser.get(prefix).json()['snapshots']), 1)
            session = self.browser.post(prefix + '/sessions', json={
                'provider': 'graphify', 'base_ref': base.revision, 'head_ref': head.revision,
            })
        self.assertEqual(session.status_code, 201, session.text)
        self.assertEqual([call.args[2] for call in fetch.call_args_list], ['main', base.revision])


class ProviderContractTests(unittest.TestCase):
    def test_github_commit_listing_follows_requested_page_and_reports_first_parent(self):
        head, parent = 'f' * 40, 'd' * 40

        def request(req):
            self.assertEqual(req.url.path, '/repos/owner/repo/commits')
            self.assertEqual(dict(req.url.params), {'sha': head, 'per_page': '20', 'page': '2'})
            return httpx.Response(200, headers={'Link': '<https://api.github.com/repos/owner/repo/commits?page=3>; rel="next"'},
                                  json=[{'sha': head, 'parents': [{'sha': parent}, {'sha': 'a' * 40}],
                                         'commit': {'message': 'Adjust pricing\n\nLong description',
                                                    'committer': {'date': '2026-11-23T04:30:00Z'}}}])

        with httpx.Client(transport=httpx.MockTransport(request)) as http:
            result = github_commits(http, 'owner/repo', head, 2)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['commits'][0]['subject'], 'Adjust pricing')
        self.assertEqual(result['commits'][0]['parent_sha'], parent)

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
