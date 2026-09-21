"""Human-readable run export for portfolio demos and review handoffs."""

from __future__ import annotations

from typing import Any, Dict


def build_markdown_report(payload: Dict[str, Any]) -> str:
    run = payload["run"]
    counts = payload.get("counts", {})
    connector = run.get("connector_mode", "mock")
    connector_label = "官方 MCP stdio Client" if connector == "official_mcp" else "进程内模拟连接器"
    lines = [
        "# AI 案例情报运行报告",
        "",
        "> 脱敏作品集演示 · 不连接真实企业系统",
        "",
        "## 运行摘要",
        "",
        f"- 运行 ID：`{run['run_id']}`",
        f"- 主题：{run['topic']}",
        f"- 最终状态：`{run['status']}`",
        f"- 场景：`{run['scenario']}`",
        f"- 连接器：{connector_label}",
        f"- 案例数：{sum(counts.values())}（通过 {counts.get('verified', 0)}，淘汰 {counts.get('rejected', 0)}）",
        "",
        "## Agent 与人工决策",
        "",
    ]
    for event in payload.get("events", []):
        lines.append(f"- `{event['actor']}` / `{event['event_type']}` / `{event['status']}`：{event['message']}")
    lines.extend(["", "## 案例核验", ""])
    for case in payload.get("cases", []):
        lines.extend([
            f"### {case['case_id']} · {case['status']}",
            f"- 标题：{case['title_original']}",
            f"- 原文：{case['source_url']}",
            f"- 结论：{case['verification_reason']}",
            "",
        ])
    lines.extend(["## 分发结果", ""])
    for delivery in payload.get("deliveries", []):
        detail = f"，外部 ID `{delivery['external_id']}`" if delivery.get("external_id") else ""
        error = f"；错误：{delivery['last_error']}" if delivery.get("last_error") else ""
        lines.append(f"- `{delivery['channel']}`：`{delivery['status']}`{detail}{error}")
    lines.extend(["", "## 冻结内容", "", "```markdown", payload.get("content_version", {}).get("body", "").rstrip(), "```", ""])
    return "\n".join(lines)
