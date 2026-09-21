"""Local evidence report and optional Tencent Docs handoff.

The SQLite catalog is the canonical store. Tencent Docs is an export target,
so a local collection is complete even when no remote document is configured.
"""
import csv
import hashlib
import io
import json
from pathlib import Path
from .db import Database, utc_now
from .scoring import score_case
from .publication_dates import date_status, with_publication_dates

FIELDS = ["案例ID", "原文发布日期", "日期依据", "收录日期", "标题", "问题", "方法", "结果", "证据局限", "质量分", "来源链接", "核验依据"]


def live_records(db: Database) -> list[dict]:
    rows = db.connection.execute("SELECT payload_json FROM source_items WHERE status IN ('selected','featured') ORDER BY discovered_at DESC")
    return [with_publication_dates(item) for row in rows if (item := json.loads(row[0])).get("discovery_mode") in {"web_search", "rss", "douyin_browser"}
            and date_status(item.get("published_at")) == "recent"
            and item.get("verification", {}).get("decision") == "pass" and score_case(item)["selected"]]


def tencent_rows(db: Database) -> list[dict]:
    return [dict(zip(FIELDS, [item["case_id"], item.get("published_at", ""), item.get("published_at_source", "来源记录"), item.get("collected_at", ""), item["title_original"],
                item["problem"]["claim"], item["approach"]["claim"], item["outcome"]["claim"],
                "；".join(item.get("limitations", [])), score_case(item)["quality_score"], item["source_url"],
                item["verification"]["reason"]])) for item in live_records(db)]


def build_report(db: Database, result: dict) -> str:
    lines = ["# AI 真实用法采集报告", "", f"运行：{result['run_id']}", f"状态：{result['status']}",
             f"采集时间：{result.get('finished_at', result.get('started_at', ''))}",
             f"发现 {result.get('discovered', 0)} 条；已处理 {len(result.get('items', []))} 条；跳过 {result.get('skipped', 0)} 条已处理来源。", "",
             "核验表示原文支持该自述，不等于已经独立复现效果。原文发布日期、采集日期分开记录；发布时间不等于实际发生时间。", ""]
    if result.get('diagnostics'):
        lines += ['## 本轮结果原因', '', result['diagnostics']['summary'], '']
    for entry in result.get("items", []):
        row = db.source_item(entry["id"])
        if not row:
            continue
        item = json.loads(row["payload_json"])
        lines += [f"## {item['title_original']}", "", f"状态：{row['status']}", f"来源：{item['source_url']}", f"原文发布日期：{with_publication_dates(item)['published_at'] or '待确认'}", f"日期依据：{item.get('published_at_source') or '待确认'}", f"收录时间：{item.get('collected_at', '')}", ""]
        if entry.get('diagnostic'):
            lines += [f"处理说明：{entry['diagnostic']['label']}；{entry['diagnostic']['reason']}", '']
        if row["status"] in {"selected", "featured", "candidate", "needs_scoring"}:
            for key, label in [("problem", "问题"), ("approach", "方法"), ("outcome", "结果")]:
                block = item.get(key, {})
                lines += [f"**{label}**：{block.get('claim', '')}", "", f"> 原文：{block.get('evidence', '')}", ""]
            card = score_case(item)
            lines += [f"质量分：{card['quality_score'] if card['quality_score'] is not None else '待评分'} · {card['score_version']}", "", card["reason"], ""]
            for key, judgment in card["judgments"].items():
                lines += [f"- {card['dimension_labels'][key]}：{card['dimensions'][key]}/{card['dimension_max'][key]}；{judgment['reason']}"]
            lines += ["", "待补信息：" + "；".join(x for gap in card["gaps"] for x in gap["missing"]), ""]
        lines += [f"核验/待办原因：{item.get('verification', {}).get('reason') or item.get('extraction', {}).get('reason', '')}",
                  f"局限：{'；'.join(item.get('limitations', []))}", ""]
    if result.get("feed_errors"):
        lines += ["## 来源错误", ""] + [f"- {e['url']}：{e['error']}" for e in result["feed_errors"]]
    lines += ["", "知识库：本地 SQLite 已保存；腾讯文档仅作为可选导出目标。", ""]
    return "\n".join(lines)


def export_collection(db: Database, result: dict, directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{result['run_id']}.md"
    report.write_text(build_report(db, result), encoding="utf-8")
    rows = tencent_rows(db)
    manifest = {"schema_version": 2, "generated_at": utc_now(),
                "destination_url": "sqlite://data/runtime/catalog.db",
                "delivery_status": "stored_locally", "canonical_store": "local_sqlite",
                "remote_export_status": "optional", "idempotency_field": "案例ID", "fields": FIELDS,
                "records": rows, "records_sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    outbox = directory / "tencent-docs-pending.json"
    outbox.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter="\t")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("'" + str(v) if str(v).lstrip().startswith(("=", "+", "-", "@")) else v) for k, v in row.items()})
    table = directory / "tencent-docs-import.tsv"
    table.write_text(stream.getvalue(), encoding="utf-8-sig")
    (directory / f"{result['run_id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report": str(report), "tencent_manifest": str(outbox), "tencent_import": str(table)}
