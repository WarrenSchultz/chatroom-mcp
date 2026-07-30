# Roadmap

## Shipped (v0.2)
- **File transfer** — `put_file` / `get_file` / `list_files` (SQLite BLOB, ~1 MB cap,
  `CHATROOM_MAX_FILE_BYTES`), plus `GET /v1/files/<id>` download and a dashboard Files panel.
- **Observer room-switching + room list** — dashboard Rooms column, `GET /v1/rooms`, and an
  `--all-rooms` token for a whole-instance observer.
- **Room description + onboarding notes** — `get_room_info` / `set_room_info`; onboarding is
  surfaced to an agent on its first `whats_new()` in a room (and via the hook).
- **Admin retention + room deletion** — `--admin` token role, `set_retention` (background
  prune of old chat/events/files; tasks never pruned), `delete_room`, and a dashboard gear
  modal that prompts for an admin token. REST: `POST /v1/rooms/<room>/retention`,
  `DELETE /v1/rooms/<room>`, `POST /v1/rooms/<room>/info`.
- **MQTT bridge** — set `CHATROOM_MQTT_HOST` and every room event publishes to
  `<prefix>/<room>/<kind>` as JSON, so home-automation can react to agent activity.

## Ideas not yet built
- **Inbound webhook:** `POST /v1/hooks/...` so an automation can create a task or post a
  message (a failed backup opens a task, etc.) — the reverse of the MQTT bridge.
- **Presence indicator:** dashboard "online now" dot from `last_seen`.
- **@mentions + priority surfacing:** `@agent` in a message flags it for that agent.
- **Stuck-task alerts:** tasks `in_progress` beyond a threshold get surfaced.
- **Markdown export:** dump a room's history to `.md`.
- **Per-file/attachment linkage:** attach a file directly to a task ("deploy this config").
