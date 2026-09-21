#!/usr/bin/env python3
"""Bind a WeCom smart-bot recipient after an exact one-time direct message."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import runtime_dir
from backend.app.wecom_aibot import AIBotError, binding_path, listen_for_binding, load_bot_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', action='store_true', required=True)
    parser.add_argument('--phrase', required=True, help='Exact one-time phrase, preferably with a random code')
    parser.add_argument('--timeout', type=int, default=90, choices=range(1, 121), metavar='SECONDS')
    args = parser.parse_args()
    try:
        directory = runtime_dir()
        config = load_bot_config(directory, require_user=False)
        output = listen_for_binding(config, binding_path(directory), args.phrase, timeout=args.timeout,
                                    on_ready=lambda: print('{"status":"listening"}', flush=True))
    except AIBotError as exc:
        output = {'status': 'failed', 'error': exc.code}
    except Exception:
        output = {'status': 'failed', 'error': 'binding_configuration_failed'}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output.get('status') == 'bound' else 1


if __name__ == '__main__':
    raise SystemExit(main())
