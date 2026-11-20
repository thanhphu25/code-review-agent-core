"""A deterministic cross-file review that needs neither servers nor credentials."""

import json

from .graph import ModelReply, ReviewInput, ReviewUnavailable, run_review

DEMO_SHA = 'b' * 40
SIGNATURE_FILE = 'domain/totals.py'
CALLER_FILE = 'reports/monthly.py'
DEMO_INPUT = ReviewInput('a' * 40, DEMO_SHA, '''\
diff --git a/domain/totals.py b/domain/totals.py
--- a/domain/totals.py
+++ b/domain/totals.py
@@ -1,2 +1,2 @@
-def total(items):
-    return sum(items)
+def total(items, fee):
+    return sum(items) + fee
''')


def demo_context(path: str) -> dict:
    if path != SIGNATURE_FILE:
        raise ReviewUnavailable('demo_context_not_found')
    return {'status': 'ok', 'sha': DEMO_SHA,
            'data': {'callers': [{'file': CALLER_FILE, 'name': 'monthly_total', 'start_line': 3}]}}


def demo_source(path: str) -> str:
    if path != CALLER_FILE:
        raise ReviewUnavailable('demo_file_not_found')
    return 'from domain.totals import total\n\n\ndef monthly_total(items):\n    return total(items)\n'


def demo_reviewer(messages: list[dict], _tools: list[dict]) -> ModelReply:
    observations = [message for message in messages if message['role'] == 'tool']
    if not observations:
        name, path, call_id = 'get_code_context', SIGNATURE_FILE, 'demo-context'
    elif len(observations) == 1:
        callers = json.loads(observations[0]['content'])['data']['callers']
        name, path, call_id = 'read_file', callers[0]['file'], 'demo-source'
    else:
        lines = json.loads(observations[1]['content'])['lines']
        return ModelReply({'role': 'assistant', 'content': json.dumps({
            'findings': [{'path': CALLER_FILE, 'line': 5, 'quote': lines['5'].strip(),
                          'claim': 'monthly_total omits the new required fee argument.'}],
            'gaps': [],
        })}, 'stop')

    return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps({'path': path}),
        },
    }]}, 'tool_calls')


def run_demo() -> dict:
    return run_review(DEMO_INPUT, demo_reviewer, demo_source, demo_context)
