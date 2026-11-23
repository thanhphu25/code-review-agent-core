import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from review_agent_core.demo import CALLER_FILE, DEMO_INPUT, DEMO_SHA, demo_context, demo_reviewer, demo_source, run_demo
from review_agent_core.graph import ModelReply, RelatedRepository, RepositoryTools, run_review


class ReviewCoreTests(unittest.TestCase):
    def test_demo_follows_graph_result_to_caller_outside_diff(self):
        result = run_demo()

        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['model_calls'], 3)
        self.assertEqual([step['tool'] for step in result['trace'] if 'tool' in step],
                         ['get_code_context', 'read_file'])
        self.assertNotIn(CALLER_FILE, DEMO_INPUT.diff)
        self.assertEqual(result['findings'][0]['path'], CALLER_FILE)
        self.assertEqual(result['findings'][0]['sha'], DEMO_SHA)

    def test_model_limit_stops_before_unanswerable_tool_call(self):
        lookup_context = Mock()

        result = run_review(DEMO_INPUT, demo_reviewer, demo_source, lookup_context, max_model_calls=1)

        lookup_context.assert_not_called()
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['gaps'], ['review_budget_exhausted'])

    def test_finding_requires_a_quote_from_the_cited_line(self):
        def model(messages, tools):
            reply = demo_reviewer(messages, tools)
            if reply.finish_reason != 'stop':
                return reply
            finding = json.loads(reply.message['content'])
            finding['findings'][0]['quote'] = 'invented code'
            return ModelReply({'role': 'assistant', 'content': json.dumps(finding)}, 'stop')

        result = run_review(DEMO_INPUT, model, demo_source, demo_context)

        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['gaps'], [f'unverified_quote:{CALLER_FILE}:5'])

    def test_tool_arguments_are_validated_and_observations_match_the_call(self):
        def model(messages, _tools):
            if messages[-1]['role'] == 'tool':
                self.assertEqual(messages[-1]['tool_call_id'], 'scope-check')
                observation = json.loads(messages[-1]['content'])
                self.assertEqual(observation['reason'], 'invalid_tool_arguments')
                return ModelReply({'role': 'assistant', 'content': '{"findings": [], "gaps": []}'}, 'stop')

            return ModelReply({'role': 'assistant', 'tool_calls': [{
                'id': 'scope-check', 'type': 'function', 'function': {
                    'name': 'get_code_context',
                    'arguments': json.dumps({'path': 'domain/totals.py', 'project_id': 9}),
                },
            }]}, 'tool_calls')

        lookup_context = Mock()
        result = run_review(DEMO_INPUT, model, demo_source, lookup_context)

        lookup_context.assert_not_called()
        self.assertEqual(result['status'], 'incomplete')

    def test_invalid_final_json_is_incomplete(self):
        def model(_messages, _tools):
            return ModelReply({'role': 'assistant', 'content': 'No problems found.'}, 'stop')

        result = run_review(DEMO_INPUT, model, demo_source, demo_context)

        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['gaps'], ['invalid_model_output'])

    def test_related_search_and_findings_are_scoped_to_their_repository(self):
        related = RelatedRepository('repo_2', 'Frontend', 'f' * 40)
        repo_tools = RepositoryTools(lambda _path: 'return total([1])', Mock(),
                                     lambda query: [{'path': CALLER_FILE, 'line': 1, 'text': query}])

        def model(messages, tools):
            if messages[-1]['role'] != 'tool':
                self.assertIn('search_code', [tool['function']['name'] for tool in tools])
                self.assertIn(related.head_sha, messages[-1]['content'])
                calls = [
                    ('search_code', {'query': 'total', 'repository': 'repo_2'}),
                    ('read_file', {'path': CALLER_FILE, 'repository': 'repo_2'}),
                    ('read_file', {'path': CALLER_FILE, 'repository': 'repo_99'}),
                ]
                return ModelReply({'role': 'assistant', 'tool_calls': [
                    {'id': str(index), 'function': {'name': name, 'arguments': json.dumps(arguments)}}
                    for index, (name, arguments) in enumerate(calls)
                ]}, 'tool_calls')
            observations = [json.loads(entry['content']) for entry in messages if entry['role'] == 'tool']
            self.assertEqual(observations[0]['matches'][0]['path'], CALLER_FILE)
            self.assertEqual(observations[1]['sha'], related.head_sha)
            self.assertEqual(observations[2]['reason'], 'unknown_repository')
            finding = {'path': CALLER_FILE, 'line': 1, 'claim': 'Stale caller', 'quote': 'return total([1])'}
            findings = [{**finding, 'repository': 'primary'}, {**finding, 'repository': 'repo_2'}]
            return ModelReply({'role': 'assistant', 'content': json.dumps({'findings': findings, 'gaps': []})}, 'stop')

        result = run_review(replace(DEMO_INPUT, related=(related,)), model, demo_source, demo_context,
                            related_tools={'repo_2': repo_tools})
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual((result['findings'][0]['repository'], result['findings'][0]['sha']), ('repo_2', related.head_sha))
        self.assertIn(f'unverified_quote:primary:{CALLER_FILE}:1', result['gaps'])
