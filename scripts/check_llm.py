"""Verify the configured OpenAI-compatible model without printing secrets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.agent_model import OpenAICompatibleModel


def main() -> int:
    model = OpenAICompatibleModel()
    print(f"provider={model.name}")
    print(f"base_url={model.base_url}")
    print(f"model={model.model}")
    try:
        result = model.structured(
            "verification",
            "只返回 JSON：{\"ok\":true}。不要添加 Markdown。",
            {"purpose": "connectivity_check"},
        )
    except Exception as exc:
        print(f"status=failed\nerror_type={type(exc).__name__}\nerror={exc}")
        return 1
    print("status=ok")
    print(f"response_keys={','.join(sorted(str(key) for key in result))}")
    print(f"response_json={json.dumps(result, ensure_ascii=False, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
