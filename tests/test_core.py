import json
import unittest
from unittest.mock import Mock

from review_agent_core.demo import CALLER_FILE, DEMO_INPUT, DEMO_SHA, demo_context, demo_reviewer, demo_source, run_demo
from review_agent_core.graph import ModelReply, run_review


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
