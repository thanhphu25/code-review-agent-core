"""Entry point for exercising the standalone review core."""

import argparse
import json
from pathlib import Path

from .demo import run_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run the standalone code review agent.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('demo', help='Run a deterministic cross-file review and print its trace as JSON.')
    server = commands.add_parser('serve', help='Serve the repository and review dashboard.')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', default=8765, type=int)
    server.add_argument('--database', default=Path('review-data/reviews.sqlite3'), type=Path)
    args = parser.parse_args(argv)

    if args.command == 'serve':
        try:
            from .dashboard import serve
        except ModuleNotFoundError as exc:
            parser.exit(2, f"Install the dashboard dependencies with `pip install '.[web]'` ({exc.name}).\n")
        serve(args.host, args.port, args.database)
        return 0

    result = run_demo()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'complete' else 1
