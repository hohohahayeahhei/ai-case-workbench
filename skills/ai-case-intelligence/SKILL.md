---
name: ai-case-intelligence
description: Query the curated AI use-case catalog by topic, time window, source and cursor.
---

# AI Case Intelligence Skill

Use this skill when the user wants real examples of how people or organizations use AI to solve work or life problems.

## What this skill does

- Search verified case summaries by keyword.
- Return recent cases, recent curated cases and the latest digest.
- Preserve source URLs, evidence summaries and limitations.
- Page through the catalog with `next_cursor`.

The skill is read-only. It does not write, publish, or change the catalog.

## Query behavior

Call `GET /api/v1/cases` with:

- `source_mode=live` for actually collected cases. Use `online_snapshot` only when explicitly demonstrating saved examples.
- `query`: keyword or short phrase, such as `客服`, `销售`, `知识库` or `多 Agent`.
- `window`: `24h`, `7d`, `30d` or `all`.
- `category`: optional category or scenario filter.
- `source_type`: optional source type filter.
- `limit`: 1 to 100.
- `cursor`: the previous response's `next_cursor`.
- `sort`: `relevance` or `latest`.

Use `GET /api/v1/hot` for a recent curated shortlist (not a heat ranking) and `GET /api/v1/digests/latest` for a date-window digest.

## Answer rules

1. Preserve the source URL for every case.
2. Separate source facts from your own interpretation.
3. Do not turn a limitation into a success claim.
4. Do not invent a metric, workflow step, user identity or outcome.
5. If the result says `data_status=curated_snapshot`, explain that this is a verified snapshot and may not include the whole Internet.
6. Follow `next_cursor` when the user asks for more results.

## Discovery boundary

This skill queries the local knowledge base. To actively collect new articles, the workbench runs Search/Fetch MCP through `POST /api/v1/collection/jobs` with `source_mode=live`. Do not describe a database keyword query as a fresh Internet search. The source text supports an author's account; it does not establish independent replication.

## Scores and selection

`quality_score` may be null: say pending assessment, never substitute zero or 72. Numeric scores are project-specific model judgements with independent review, not independently reproduced results. Preserve API order and source attribution. A keyword query with no selected match falls back to reviewed candidates; if `meta.fallback_to_candidates` is true, explicitly label them 未入精选. Broad queries do not widen automatically. Use mode=all only when the user requests the reviewed candidate pool as well.
