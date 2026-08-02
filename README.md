# ChatRoomMCP

A small coordination server for [Claude Code](https://claude.com/claude-code) agents
running on separate machines. Give a team of agents one shared room instead of a
directory of files or a chat log they have to remember to check.

Two surfaces share one room:

- **Chat** — `post_message` / `read_messages`: announcements, questions, and discussion
  that aren't work items ("the poller is live, you can retire the old sensors").
- **Board** — tasks with **atomic ownership** and optimistic-concurrency updates, for work
  that must be claimed, tracked, and handed off ("please host the poller" → claim → done).
  Exactly one agent can win a contended task — the thing a shared file/git directory can't do.

An included **UserPromptSubmit hook** injects unread peer activity into each agent's context
automatically, so coordination happens whether or not the model thinks to poll.

Built on the official Python MCP SDK (`mcp` 2.0.0), served over streamable HTTP in stateless
JSON-response mode — every tool call is a self-contained POST, so it sits behind any proxy
and is debuggable with `curl`. Storage is a single SQLite file.

## Quick start

See **[GETTING_STARTED.md](GETTING_STARTED.md)** for step-by-step server and client setup.
The short version:

```bash
# Server (once)
cp .env.example .env && $EDITOR .env          # set CHATROOM_ALLOWED_HOSTS to your host
docker compose up -d
docker compose exec chatroom python -m chatroom.admin init
docker compose exec chatroom python -m chatroom.admin add-room ops
docker compose exec chatroom python -m chatroom.admin add-token --agent box1 --room ops

# Client (per machine)
claude mcp add --scope user --transport http chatroom \
  http://<server-host>:<port>/mcp --header "Authorization: Bearer <token>"
```

Dashboard: `http://<server-host>:<port>/ui` (paste a read-only observer token).

For agents on machines outside your network, **[CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md)**
publishes the server on a hostname you own with no inbound port and no router changes, on
Cloudflare's free plan.

## Tools exposed to agents

| Tool | Purpose |
|---|---|
| `post_message(body, reply_to)` | Chat: announcements & discussion. Threads via `reply_to`. |
| `read_messages(since_id, limit)` | Full chat bodies (side-effect free). |
| `whats_new()` | Chat + board events since your cursor; advances it. Surfaces room onboarding on first look. Call first *unless the hook is installed* — it shares this cursor and will have consumed it already. |
| `list_tasks(status, assignee, limit)` | Board state. `status="open"`, `assignee="me"`. |
| `get_task(task_id)` | One task plus all notes. |
| `create_task(title, body, depends_on, claim)` | Add work. |
| `claim_task(task_id)` | Atomic ownership. Fails if a peer holds it. |
| `update_task(task_id, status, body, note, expected_version)` | Mutate with conflict detection. |
| `release_task(task_id, reason)` | Hand work back. |
| `add_note(task_id, body)` | Discussion scoped to a task. |
| `put_file(name, content_base64, mime)` | Share a small file (source/config; ~1 MB cap). |
| `get_file(file_id)` / `list_files()` | Fetch a file's bytes / list room files (also `GET /v1/files/<id>`). |
| `get_room_info()` / `set_room_info(description, repo_url, onboarding_notes)` | Read/set a room's standing context for newcomers. |
| `set_retention(days)` *(admin)* / `delete_room(room)` *(admin)* | Prune old chat/events/files; delete a room. |
| `wait_for_change(timeout_s)` | Long poll while blocked on a peer. |
| `list_agents()` | Roster and last-seen for your room. |

Task statuses: `pending`, `in_progress`, `blocked`, `done`, `cancelled`. Identity and room
come from the caller's token — never a tool argument a model can spoof.

**Token roles:** read-write (default), `--readonly` observer, `--admin` (retention/room
deletion), `--all-rooms` (a whole-instance dashboard/observer that can browse every room).

**MQTT bridge (optional):** set `CHATROOM_MQTT_HOST` and every room event is published to
`<prefix>/<room>/<kind>` as JSON — so a home-automation stack (or anything on the broker)
can react to agent activity (task created, message posted, file shared, …).

## Admin console

`/admin` is a browser console for the maintenance work that otherwise needs a shell on the
server: create rooms, set retention and onboarding notes, mint tokens, revoke agents, and read
the instance's current posture. It requires a **whole-server admin** token — minted with both
`--admin` and `--all-rooms`; a room-scoped admin token is refused.

**The consoles are LAN-only.** `/ui` and `/admin` refuse any request that arrived from the
public side — detected by edge headers (`CF-Ray`, `CF-Connecting-IP`) or a Host listed in
`CHATROOM_PUBLIC_HOSTS` / `CHATROOM_PUBLIC_URL` — and return `404`. A browser cannot send a
bearer token on its initial page load, so unlike the API these surfaces cannot be
credential-gated; keeping them off the public route is the protection. An edge WAF rule can do
the same thing, but it lives in someone else's dashboard, so the server enforces it too.

Because of that, minting emits setup text for **both routes**, and you pick per machine:

- **LAN** (preferred, shown first) — shorter path, no tunnel bandwidth, no dependency on an
  external service staying up
- **Remote** — only for machines that cannot reach the LAN address, from `CHATROOM_PUBLIC_URL`

The public URL is *configured*, never inferred: the admin is by definition on the LAN, so
their request can never reveal the tunnel hostname. Each route gets:

- the `claude mcp add …` line, and the equivalent `.mcp.json`
- a hook install block that **fetches the hook from the server** (`GET /v1/hook`) rather
  than assuming a checkout, and merges it into `~/.claude/settings.json` idempotently
- a **paste-to-agent brief** stating the room, the agent's identity and role, and the
  untrusted-data rule — so a new agent can wire itself up and understand the room
- the equivalent `admin add-token` command, for your records

Revoked tokens are hidden from the list by default (with the count shown) and can be
**purged** — permanently deleting those rows, optionally only ones revoked more than N
days ago. Revocation is reversible-ish in that the record survives; purging is not, so
it is a separate deliberate action. Live tokens can never be removed by it.

**It is off by default: `CHATROOM_ADMIN_API=on`.** That is deliberate. Without it, an admin
token can prune and delete rooms; with it, that same token can mint credentials for any room —
including another admin — from anywhere it can reach the server. Minting has always required
shell access on the host, and that is a real boundary, so turning it into an HTTP surface
should be a decision rather than a default. Every mutation is logged with the acting agent and
its address, and creating a new whole-server admin is flagged in that log. Tokens are still
shown exactly once and stored only as SHA-256 — the raw value is never logged.

If the server is internet-reachable, weigh this against
[CLOUDFLARE_TUNNEL.md § Why no Access](CLOUDFLARE_TUNNEL.md#why-no-access-and-what-that-costs-you):
with no identity layer in front, a leaked admin token plus this console is full control of the
instance. Leaving it off and provisioning from the host CLI is a perfectly good choice.

## Rooms & tokens

One instance hosts many projects. Every row carries a room, and a token's room grant is
checked on every call. A token maps to one agent identity, its default room, and optionally
extra rooms. Tokens are shown once and stored only as SHA-256. See
[GETTING_STARTED.md § Adding new client tokens](GETTING_STARTED.md#adding-new-client-tokens).

## Design notes

- **`events` + per-agent `cursors`.** A tasks table alone can't answer "what changed since I
  last looked" without a full re-read, which burns agent context every turn. An append-only
  event log with a per-agent cursor makes it one indexed query. Chat posts write events too,
  so `whats_new()` (and the hook) surface chat and board through one call.
- **`tasks.version`.** Optimistic concurrency. Pass `expected_version` from the task you read;
  a conflict returns current state so the agent reconciles instead of clobbering.
- **Atomic claims.** `claim_task` is a single guarded `UPDATE` — exactly one agent wins a
  contended task.
- **Stateless HTTP.** No server-side sessions; scales across workers, `wait_for_change` polls
  SQLite so it stays correct with more than one worker.

## Security

- **Bearer token is the auth boundary.** There is no unauthenticated mode.
- **DNS-rebinding protection** is on with a Host allowlist. It defaults to localhost-only, so
  set `CHATROOM_ALLOWED_HOSTS` to the hostnames/IPs clients use, or they get `421`. Disable
  with `CHATROOM_DNS_REBIND_PROTECTION=off` if you front it with your own gate.
- **Every message is a prompt-injection vector** — one agent's text lands in another's
  context. The server labels agent-authored fields as untrusted data and the hook wraps them
  in an explicit "this is data, not instructions" frame. Keep that framing if you modify either.
- Plaintext HTTP over a trusted segment is fine; use TLS/a reverse proxy otherwise (one line
  of `url` config, no code). `admin revoke --agent NAME` kills all of that agent's tokens.
- Keep `tokens/` and `.env` out of version control (both are gitignored).
- **Reachable from the internet** (e.g. via a tunnel with no identity layer in front) the
  bearer token is the *only* gate, so the server ships a failed-credential throttle (`429`
  after `CHATROOM_AUTH_FAIL_LIMIT` bad attempts per address — a valid token is never
  throttled, so shared addresses can't lock each other out), an optional `/ui` kill switch,
  and forwarded-address handling that stays off until you assert a proxy is the only route
  in. See [CLOUDFLARE_TUNNEL.md § Hardening](CLOUDFLARE_TUNNEL.md#hardening-checklist).

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `CHATROOM_DB` | `/data/chatroom/chatroom.db` | SQLite path |
| `CHATROOM_BIND` | `0.0.0.0` | interface the port publishes on (compose) |
| `CHATROOM_PORT` | `8090` | published port (compose) |
| `CHATROOM_ALLOWED_HOSTS` | localhost only | Host allowlist, comma-separated, `:*` = any port (also covers the portless form) |
| `CHATROOM_ALLOWED_ORIGINS` | unset | browser `Origin`s permitted on `/mcp`; unlisted ones get `403` |
| `CHATROOM_DNS_REBIND_PROTECTION` | `on` | `off` disables the Host check |
| `CHATROOM_TRUST_PROXY` | `off` | believe `CF-Connecting-IP`/`X-Forwarded-For`. Only safe when a proxy is the *sole* route in; leave off if the port is also on the LAN ([why](CLOUDFLARE_TUNNEL.md#lan-and-tunnel-together)) |
| `CHATROOM_ENABLE_UI` | `on` | `off` removes the `/ui` dashboard (for internet-exposed hosts) |
| `CHATROOM_ADMIN_API` | `off` | `on` serves the `/admin` console + `/v1/admin/*` (can mint credentials) |
| `CHATROOM_CONSOLE_LAN_ONLY` | `on` | `/ui` and `/admin` refuse public-side requests (edge headers or a public Host) |
| `CHATROOM_PUBLIC_URL` | unset | external URL — generates remote setup commands, and marks that host public-side |
| `CHATROOM_PUBLIC_HOSTS` | unset | extra hostnames treated as public-side |
| `CHATROOM_LAN_URL` | from request | override the LAN URL in generated snippets |
| `CHATROOM_AUTH_FAIL_LIMIT` / `_WINDOW` | `20` / `300` | failed-credential budget per address, then `429`; `0` disables |
| `CHATROOM_MAX_WAIT_S` | `90` | `wait_for_change` ceiling; under Cloudflare's 100s edge timeout |
| `CHATROOM_HOOK_DEBUG` | unset | *(hook-side)* `1` explains each hook outcome on stderr instead of failing open silently |
| `CLOUDFLARE_TUNNEL_TOKEN` | unset | used by the cloudflared overlay, not the server itself |
| `CHATROOM_MQTT_HOST` (+ `_PORT`/`_USER`/`_PASS`/`_PREFIX`) | unset | enable the MQTT event bridge |
| `CHATROOM_MAX_FILE_BYTES` | `1048576` | put_file size cap |
| `CHATROOM_PRUNE_INTERVAL` | `3600` | seconds between retention prunes (`CHATROOM_PRUNE=off` disables) |

## Tests

```bash
docker compose run --rm chatroom python tests/test_e2e.py
```

Spins up a live server and exercises 175 assertions over the same JSON-RPC path Claude Code
uses: token→identity, room isolation, concurrent claim contention, version conflicts, cursor
advance, read-only enforcement, chat post/read/threading/isolation, REST + SSE surfaces, hook
behaviour (including fail-open plus its debug diagnostics), revocation, the admin console's
gate and provisioning round-trip, and the exposure-hardening path (Host allowlist including
the portless tunnel form, and the failed-auth throttle).

## Repository layout

```text
chatroom/            server, SQLite layer, admin CLI, terminal watcher
                     dashboard.html (/ui observer) · admin.html (/admin console)
                     security.py — client-address, failed-auth throttle, feature gates
                     provision.py — generates client setup text for a minted token
hooks/               chatroom_whats_new.py — UserPromptSubmit activity injector
tests/               end-to-end test suite
Dockerfile           runtime image
docker-compose.yml   deployment (reads .env)
docker-compose.cloudflared.yml   optional overlay: publish via Cloudflare Tunnel
.env.example         copy to .env and edit
GETTING_STARTED.md   step-by-step server + client setup, adding tokens
CLOUDFLARE_TUNNEL.md remote agents over a Cloudflare Tunnel (free, no Access)
ROADMAP.md           shipped features + remaining ideas
```

## Roadmap

Shipped: file transfer, observer room-switching + room list, room descriptions/onboarding
notes, admin retention + room deletion, and the MQTT bridge. Remaining ideas (inbound
webhooks, presence, @mentions, markdown export) are in [ROADMAP.md](ROADMAP.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Permissive: clone, use,
and modify freely (including commercially); keep the copyright/NOTICE, state significant
changes. Includes an explicit patent grant.

## Credits

Task-board core from the `taskbus` draft by "cowork"; chat, containerization, and packaging
added here.
