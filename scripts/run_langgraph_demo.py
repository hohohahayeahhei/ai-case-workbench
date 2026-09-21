"""Run or resume the real LangGraph demo workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.langgraph_workflow import LangGraphWorkbench


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="langgraph-demo-001")
    parser.add_argument("--scenario", choices=["happy_path", "sync_conflict", "publish_unknown"], default="happy_path")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--approval", choices=["approve", "reject"], default="approve")
    parser.add_argument("--business-db", default="data/langgraph-business.db")
    parser.add_argument("--checkpoint-db", default="data/langgraph-checkpoints.db")
    parser.add_argument("--connector-mode", choices=["mock", "official_mcp"], default="mock")
    parser.add_argument("--llm-mode", choices=["mock", "openai_compatible"], default="mock")
    args = parser.parse_args()

    workbench = LangGraphWorkbench(ROOT, args.business_db, args.checkpoint_db, args.scenario, connector_mode=args.connector_mode, llm_mode=args.llm_mode)
    try:
        result = workbench.resume(args.run_id, args.approval) if args.resume else workbench.start(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("\n运行事件：")
        for event in workbench.db.events(args.run_id):
            print(f"- {event['actor']}: {event['message']}")
    finally:
        workbench.close()


if __name__ == "__main__":
    main()
