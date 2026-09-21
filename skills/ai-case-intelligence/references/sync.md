# Snapshot synchronization

Use `/api/v1/selected/snapshot` for a complete selected snapshot. Finish all `page.nextPage` pages before checking its first-page `cursor` with `/api/v1/selected/changes`. Page and sync cursors are opaque and distinct.

Current mode is snapshot_reset: changes is empty and incremental_supported is false. If reset_required is true, fetch a fresh full snapshot into a temporary store and atomically replace the prior complete snapshot, including removals. Retain the prior snapshot on failure; do not interpret the empty changes array alone as no change. This service does not provide per-item upsert/remove change logs.
