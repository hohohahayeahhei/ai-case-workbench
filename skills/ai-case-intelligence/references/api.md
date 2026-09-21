# AI Case Intelligence API v1

The query layer is intentionally small and read-only.

```text
GET /api/v1/cases
GET /api/v1/hot
GET /api/v1/digests/latest
GET /api/v1/snapshot
GET /api/v1/selected/snapshot
GET /api/v1/selected/changes
GET /api/v1/health
```

Every case item includes an `id`, `title`, `summary`, `source_url`, `published_at`, `evidence` and `limitations`. Pagination uses an opaque `next_cursor`.

Pass `source_mode=live` to read actually collected and verified cases. `online_snapshot` is a separate saved demonstration. `selected/snapshot` exposes page and sync cursors. `selected/changes` returns `reset_required=true` if the selected content has changed; fetch a full snapshot then, including removals. It is not a complete incremental event feed. Live Search/Fetch MCP populate the catalog through `/api/v1/collection/jobs`.
