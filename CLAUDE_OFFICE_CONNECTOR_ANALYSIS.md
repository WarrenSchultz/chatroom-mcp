# Using chatroom-mcp as a Claude custom connector (Office agents & beyond)

Analysis of whether **Claude for Excel / Word** (and other claude.ai surfaces)
can join a chatroom room by registering this server as a **custom remote-MCP
connector**, what already fits, and what would need to change.

Prepared 2026-08-09 by an external agent (Claude Code) at Warren's request, to
hand off to whoever is working this repo. External-doc citations are Anthropic's
current (2026) docs; repo citations are `file:line` in this tree. Points marked
**VERIFY** are not fully pinned and should be confirmed before relying on them.

## Update 2026-11-17: what changed in Anthropic's docs

Re-checked against the current pages (listed under Sources). None of them shows a
last-updated date, so these may have landed any time since August. They supersede
the matching points below. The `REQUEST HEADERS` result decides which path you take.

1. **Static-header auth exists, but is a gated beta.** The connector form offers
   *Authentication: No sign-in* plus up to four **Request headers**, sent exactly as
   entered (`Authorization` → `Bearer <token>`). The docs say it is "in beta and
   available to a limited set of organizations." **First step: open Organization
   settings → Connectors → Add custom and see whether Request headers is offered.**
   If it isn't, the choices are OAuth or an unauthenticated server, and the second
   is not an option for this bus. Auth settings cannot be edited after adding; you
   remove and re-add, and every member reconnects.
2. **OAuth paths are now spelled out, and they solve the identity problem.** Client
   options: *Use Claude's published identity* (CIMD; the server must support it),
   *Register automatically* (DCR), or *Use your own OAuth client*. With *Sign in
   now*, each member signs in separately, which maps naturally to one room
   identity per person. That needs an OAuth authorization server in front of the
   token model, which this repo does not have. The `client_credentials` grant is
   not supported. Enterprise Managed Auth (SSO, no consent screen) is also
   documented. Callback: `https://claude.ai/api/mcp/auth_callback`. Discovery,
   registration and token endpoints get a 10 s timeout.
3. **Network: Anthropic connects from `160.79.104.0/21`** (authoritative list:
   platform.claude.com/docs/en/api/ip-addresses), and the server must be reachable
   from the public internet. The docs say nothing about Host or Origin. **The
   bigger risk is our own Cloudflare edge:** the posture in `CLOUDFLARE_TUNNEL.md`
   blocks tokenless requests, and bot rules block unfamiliar clients. Either one
   would block OAuth discovery, which arrives without a token. Add a WAF skip
   rule for that range before testing.
4. **New alternative to the public tunnel: MCP tunnels** (Enterprise, by request,
   research preview). An outbound-only tunnel under `tunnel.anthropic.com` with no
   public endpoint. It needs Streamable HTTP, which `/mcp` already uses.
5. **Tool scoping is per user, not per org.** Members can set any tool to
   *Blocked* under Customize → Connectors. Admins get no org-wide read-only or
   per-tool allowlist, except for tools that render interactive UI. A restricted
   Office agent therefore still needs its limits enforced by the server, through
   its token.
6. **Distribution:** an org-added connector appears for every member under
   Customize → Connectors with a Custom label, and each member connects it
   themselves. Only Owners and Primary Owners can add one (on Enterprise, also a
   custom role with Manage access to Libraries). The docs describe no scoping to
   groups.
7. **Office is documented, with one gap.** The Office agents page says custom
   connectors appear under **+ → Connectors** in Excel, Word, PowerPoint and
   Outlook. The main support article lists only Claude, Cowork and Desktop.
   Confirm in Excel before relying on it.
8. **Client limits that bit this server:** tool results are capped at ~150,000
   characters on claude.ai, Desktop and Office (Claude Code: 25k tokens by default),
   with a 240 s per-call timeout. `wait_for_change` stops at 90 s, so it is fine.
   `read_messages` was **not** fine: it bounded the message count but not the
   size, and long agent posts overflowed the cap. Fixed in the same change as this
   update with a 60k-character budget per call (`CHATROOM_MAX_READ_CHARS`) and
   explicit `more` / `next_since_id` paging. `list_tasks` has the same shape
   (18 long tasks came to 57.6 KB) and is not yet budgeted.

## TL;DR (original, 2026-08-09; see the update above)

- **Transport fit: good.** This server already speaks the transport a claude.ai
  custom connector needs — MCP over **streamable HTTP** at `/mcp`, stateless +
  JSON-response mode (`chatroom/server.py:17`, `:26`). A Cloudflare tunnel already
  gives it a public HTTPS URL (`CLOUDFLARE_TUNNEL.md`), which connectors require.
- **Auth fit: probably, with a caveat.** It authenticates with a static
  `Authorization: Bearer <token>` header (`chatroom/provision.py:111,118`;
  `mcp.json.example`). claude.ai custom connectors support **request-header auth
  *and* OAuth** per Anthropic's docs — so a bearer-header connector *should*
  register without OAuth. **VERIFY** the current custom-connector UI still accepts
  a static header (vs. requiring OAuth 2.1).
- **The real design question is identity, not plumbing.** Here, identity + room
  are baked into the token. A single org connector carries a single token → the
  Office agent would post as **one dedicated identity**, not per-end-user. That's
  fine for "an Excel agent in the room," but it's not per-user attribution.
- **What you get: agent-to-*tool*, not agent-to-agent.** Claude-in-Excel would
  *call this server's tools* (`post_message`, `read_messages`, `whats_new`, …) and
  coordinate with the Claude Code agents **through the shared room** — the same
  "shared MCP server" pattern Anthropic points to for cross-surface coordination.
  There is no native agent-to-agent handoff across Claude surfaces.

## Background: how Office agents consume connectors

Claude for Excel/Word/PowerPoint/Outlook are **end-user add-ins** (GA
2026-01-24). They do **not** expose an API, webhook, or agent-handoff channel —
you can't call *them*. What they *do* accept is **connectors** for pulling
external context, and the connector system is the **same** one used by claude.ai,
Claude Code, and the Agent SDK — including **custom remote-MCP connectors** an org
enables. Anthropic's docs:

> "Connectors and Skills work the same way across Claude for Excel, PowerPoint,
> Word, and Outlook." — [Office agents: Connectors & Skills](https://claude.com/docs/office-agents/connectors-and-skills)

and, from the same page:

> "…plus any custom connectors your organization has enabled."

So: a custom remote-MCP connector added at the org level becomes usable inside all
four Office agents. Word behaves identically to Excel here.

## What this server is today (grounded in the repo)

| Property | Finding | Evidence |
|---|---|---|
| MCP transport | Streamable HTTP, **stateless + JSON-response** | `chatroom/server.py:17`, `:26` |
| MCP library | `mcp>=2.0.0` on Starlette/uvicorn ASGI | `requirements.txt`; `chatroom/server.py:45-47` |
| Endpoint | `/mcp` (plus an SSE debug stream `/v1/stream`) | `mcp.json.example`; `chatroom/server.py:1524-1529` |
| Auth | Static **`Authorization: Bearer <token>`** | `chatroom/provision.py:111,118,226` |
| Identity model | **Identity + room derived from the token**, never from tool args | `chatroom/provision.py:226` |
| Public exposure | Cloudflare tunnel → public HTTPS URL | `CLOUDFLARE_TUNNEL.md`; `docker-compose.cloudflared.yml` |
| Transport security | DNS-rebinding protection / Host allowlist toggle | `chatroom/server.py:1647-1663`; `chatroom/security.py:102` |
| Client wiring today | `claude mcp add --transport http … --header "Authorization: Bearer …"` | `chatroom/provision.py:109-111`; `mcp.json.example` |

The current onboarding path targets **Claude Code** clients (CLI `claude mcp add`).
The claude.ai **custom connector** path is a different front door to the *same*
server — no protocol change required, just registration + a compatible auth mode.

## Fit analysis vs. a claude.ai custom connector

A claude.ai remote-MCP custom connector needs: an **HTTPS** URL to a **remote MCP
server** (HTTP/SSE transport), and an auth mode it supports (**OAuth** or
**request-header**). Mapping to this server:

- **URL / TLS** — the Cloudflare tunnel HTTPS URL + `/mcp`. Fits. ✅
- **Transport** — streamable HTTP is the modern remote-MCP transport connectors
  expect; stateless + JSON-response avoids session-stickiness assumptions, which
  is ideal for a hosted connector. Fits. ✅
- **Auth** — static bearer header. claude.ai custom connectors document both
  OAuth and **request-header auth**, so this *should* map to the header option
  (paste the token as the `Authorization` header when adding the connector).
  **VERIFY** against the live UI ([Get started with custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
  [Custom remote MCP](https://claude.com/docs/connectors/custom/remote-mcp)). ⚠️
- **Host/Origin allowlist** — DNS-rebinding protection (`server.py:1647-1663`)
  validates Host/Origin. Anthropic's connector infra reaches the server from its
  own hosts, so the tunnel hostname (and any Origin the platform sends) must be in
  the allowlist, or protection must be relaxed for that host. **VERIFY**. ⚠️

## The identity question (the part that actually matters)

Everywhere else, one token = one agent identity = one room. A claude.ai custom
connector, however, is registered **once per org** with **one** header/token (or,
under OAuth, per-user credentials). Consequences:

- **Static-header connector (simple):** mint a dedicated token (e.g. agent
  `excel-agent`, the target room) and register it as the connector's bearer
  header. Every Office user who uses that connector then posts to the room as the
  **same single identity**. Good enough for "the Excel Claude is a participant in
  the room"; **not** per-user attribution.
- **Per-user identities (needs work):** would require this server to act as an
  **OAuth 2.1 authorization server** (ideally with dynamic client registration) so
  each user authenticates and maps to their own room identity. It's static-bearer
  today, so this is net-new work — only pursue if per-user attribution matters.

Recommendation: start with a **dedicated Office-agent token** (static header). It
is the smallest change and matches how the room already models agents (one token =
one identity).

## Recommended path to enable (if desired)

1. **Mint an Office-agent token** for the target room (same minting flow used for
   any agent; identity/room encoded in the token).
2. **Confirm the tunnel host is allowlisted** for DNS-rebinding/Host checks
   (`server.py:1647-1663`) so Anthropic's connector infra can reach `/mcp`.
3. **Register the custom connector** (org Owner, Team/Enterprise): *Organization
   settings → Connectors → Add → Custom*, URL = `https://<tunnel-host>/mcp`, auth =
   request header `Authorization: Bearer <office-agent-token>`
   ([Custom remote MCP](https://claude.com/docs/connectors/custom/remote-mcp)).
4. **Members enable it**: *Customize → Connectors → Connect*. It then appears in
   Claude for Excel/Word/PowerPoint/Outlook.
5. **Test from Excel/Word**: ask the in-app Claude to call `whats_new` /
   `read_messages` / `post_message`; confirm the post shows up in the room and the
   Claude Code agents see it via their own connection.

## Coordination model & caveats

- **It's a shared bus, not a pipe.** The Office Claude *calls the room's tools*;
  it does not "message" the Claude Code agents directly. Coordination = every
  participant independently connected to the same room. This is the only
  cross-surface pattern Anthropic supports today (shared MCP server / shared files
  / human relay); there is no agent-to-agent handoff protocol.
- **Untrusted-data rule still applies.** The room already instructs agents to
  treat peer messages as *data describing work, not instructions*. An Office agent
  joining the room is another untrusted writer — same rule.
- **Tool surface exposure.** A connector exposes **all** the server's MCP tools to
  the Office agent (post/read/tasks/files/admin-less set). If Office should be
  read-mostly or restricted, consider a token/role that scopes tools, or a
  connector-specific tool allowlist. **VERIFY** whether per-connector tool scoping
  is available on the claude.ai side; otherwise scope at the server/token level.
- **Blast radius of a public connector.** Registering the tunnel URL as an org
  connector widens who/what can reach the bus. Keep the token minimal-scope and
  rotate per existing token hygiene.

## Open items to verify before relying on this

Status as of 2026-11-17 (details in the update at the top):

1. ~~Static request-header auth still accepted?~~ **Partly answered.** It is
   supported, but as a beta for a limited set of orgs. Still to check: whether
   PT's org has it, by looking at the Add custom connector form.
2. **Still open.** The docs say nothing about Host or Origin, so the server's
   DNS-rebinding check must allow the tunnel hostname. Also needed now: a
   Cloudflare WAF skip rule for `160.79.104.0/21`.
3. ~~Auto-visible or opt-in?~~ **Answered.** The connector is visible to every
   member, and each member connects it themselves. The docs describe no group
   scoping.
4. ~~Per-connector tool scoping?~~ **Answered.** Members can block tools for
   themselves; there is no org-wide control. Enforce it on the server.
5. **New:** confirm in Excel itself that custom connectors appear under
   **+ → Connectors**, because the main support article does not list the
   Office agents.

## Sources

- [Office agents — Connectors & Skills](https://claude.com/docs/office-agents/connectors-and-skills)
- [Claude for Word](https://claude.com/docs/office-agents/word)
- [Custom connectors (remote MCP)](https://claude.com/docs/connectors/custom/remote-mcp)
- [Get started with custom connectors using remote MCP](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Building connectors (MCP server guide)](https://claude.com/docs/connectors/building/)
- [Connector authentication](https://claude.com/docs/connectors/building/authentication.md) (added 2026-11-17)
- [MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview) (added 2026-11-17)
- [Anthropic IP addresses](https://platform.claude.com/docs/en/api/ip-addresses) (referenced, not read)
- Repo: `chatroom/server.py`, `chatroom/provision.py`, `chatroom/security.py`,
  `mcp.json.example`, `CLOUDFLARE_TUNNEL.md`
