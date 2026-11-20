"""Entry point for exercising the standalone review core."""

import argparse
import json

from .demo import run_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run the standalone code review agent demo.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('demo', help='Run a deterministic cross-file review and print its trace as JSON.')
    parser.parse_args(argv)

    result = run_demo()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'complete' else 1
