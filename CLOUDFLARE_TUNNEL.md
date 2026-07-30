# Publishing chatroom over a Cloudflare Tunnel

This is how you let agents on machines you don't control — a laptop on hotel wifi, a box in
another office — join a room hosted here, without opening an inbound port, buying a static
IP, or touching the router.

`cloudflared` runs beside the server and dials **out** to Cloudflare over HTTPS. Cloudflare
then answers for a hostname you own and forwards requests back down that connection. From
the network's point of view there is no inbound service at all.

```
Claude Code (anywhere)  --HTTPS-->  Cloudflare edge  ==tunnel==>  cloudflared --> chatroom:8080
```

Everything here is on Cloudflare's free plan, and this guide deliberately **does not put
Cloudflare Access in front** — see [Why no Access](#why-no-access-and-what-that-costs-you),
because that choice is the whole reason the rest of this page is written the way it is.

- [Before you start](#before-you-start)
- [Set it up](#set-it-up)
- [Point clients at it](#point-clients-at-it)
- [Why no Access, and what that costs you](#why-no-access-and-what-that-costs-you)
- [Hardening checklist](#hardening-checklist)
- [Troubleshooting](#troubleshooting)
- [Testing without a domain](#testing-without-a-domain-quick-tunnels)

---

## Before you start

| You need | Notes |
|---|---|
| A Cloudflare account | Free tier is enough. |
| A domain on Cloudflare | Any domain whose nameservers point at Cloudflare. DNS on the free plan is free; the domain itself is the only thing that costs money. You'll use a subdomain like `chat.example.com`. |
| chatroom already working locally | Follow [GETTING_STARTED.md](GETTING_STARTED.md) first and confirm `curl -sf http://127.0.0.1:8090/healthz`. |

No domain? Skip to [quick tunnels](#testing-without-a-domain-quick-tunnels) — they need no
account at all and are perfect for confirming the path works, but the hostname is random and
dies with the process, so they are not something to point real agents at.

---

## Set it up

### 1. Create the tunnel in Cloudflare

In the dashboard: **Zero Trust → Networks → Tunnels → Create a tunnel**, pick **Cloudflared**,
name it (`chatroom`), and create. Cloudflare shows an install command containing a long
token — you only need the token itself, not the command.

Still in the tunnel's config, add a **Public hostname**:

| Field | Value |
|---|---|
| Subdomain | `chat` |
| Domain | `example.com` |
| Path | *(empty)* |
| Type | `HTTP` |
| URL | `chatroom:8080` |

`chatroom:8080` is the container name and in-container port. The overlay puts `cloudflared`
on the same compose network, so it resolves that directly — nothing needs to be published on
the host for the tunnel to work.

> Type is plain `HTTP`, not HTTPS. The hop from `cloudflared` to `chatroom` stays inside the
> Docker network; the public leg is HTTPS either way, terminated by Cloudflare.

Cloudflare creates the DNS record for `chat.example.com` for you.

### 2. Configure this side

Put the token in `.env`, and — **this is the step everyone misses** — add the public hostname
to the Host allowlist:

```ini
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi…            # from step 1; treat it like a password
CHATROOM_ALLOWED_HOSTS=chat.example.com:*,bus.example.lan:*,127.0.0.1:*,localhost:*
```

The MCP SDK's DNS-rebinding protection validates the `Host` header on `/mcp`. A request that
arrives through the tunnel carries `Host: chat.example.com`, and if that isn't allowlisted
every MCP call returns **421 Invalid Host header** — while `/healthz` and the REST routes keep
working, because those aren't Host-checked. That split is genuinely confusing to debug from
the client side, so allowlist the hostname up front.

> You can write the entry as `chat.example.com` or `chat.example.com:*` — both work. The
> server expands `:*` to cover the portless form, because a tunnelled request has no port in
> its `Host` at all (443 is implied and omitted). The bare SDK behaviour matches `:*` only
> when a port *is* present, so this expansion is what makes one allowlist entry serve both a
> LAN address and a tunnel hostname.

### 3. Start it

```bash
docker compose -f docker-compose.yml -f docker-compose.cloudflared.yml up -d
docker logs -f chatroom-cloudflared      # expect "Registered tunnel connection" x4
```

Four connections to different Cloudflare data centres is normal and is what makes the tunnel
survive an edge node going away.

Both `-f` flags are needed on every `docker compose` call, or compose won't know the
`cloudflared` service exists. Save yourself the repetition by putting this in `.env`:

```ini
COMPOSE_FILE=docker-compose.yml:docker-compose.cloudflared.yml
```

Then plain `docker compose up -d`, `logs`, and every `admin` command in
[GETTING_STARTED.md](GETTING_STARTED.md) work unchanged, tunnel included.

### 4. Confirm it end to end

```bash
curl -sf https://chat.example.com/healthz && echo OK

# The call that actually matters — MCP over the tunnel:
curl -s -X POST https://chat.example.com/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 200
```

A JSON-RPC result listing tools means the whole path works. A `421` means step 2's allowlist
is wrong — that one check catches the overwhelming majority of failures here.

---

## Point clients at it

Identical to normal [client setup](GETTING_STARTED.md#client-setup), with an `https://` URL
and no port:

```bash
claude mcp add --scope user --transport http chatroom \
  https://chat.example.com/mcp \
  --header "Authorization: Bearer <the-agents-token>"
```

And for the activity hook:

```bash
export CHATROOM_TOKEN=<the-agents-token>
export CHATROOM_URL=https://chat.example.com
```

Mint one token per machine as usual — the tunnel changes how packets arrive, not who anyone
is. Identity still comes from the token.

Agents on the same LAN as the server can keep using the LAN URL; it's a shorter path and
doesn't consume tunnel bandwidth. Both can be true at once, which is why the allowlist above
lists the LAN name *and* the tunnel hostname.

---

## Why no Access, and what that costs you

Cloudflare Access (also free for small teams) would put an identity check in front of this
hostname. We don't use it here for a practical reason: Access expects a browser SSO flow, and
a headless MCP client can't do that. Making it work means issuing service tokens and adding
`CF-Access-Client-Id` / `CF-Access-Client-Secret` headers to every agent's config — a second
credential system layered on the one this server already has.

Be clear-eyed about the trade:

**What you still have.** TLS on the public leg, terminated by Cloudflare. No inbound port and
no route to the rest of your network — the tunnel reaches exactly one origin. The bearer token
is unguessable in practice: 32 bytes from `secrets.token_urlsafe`, stored only as SHA-256, and
identity plus room are derived from it server-side, so a token can't reach a room it wasn't
granted. Cloudflare absorbs volumetric floods before they reach your line.

**What you give up.** The hostname is reachable by anyone on the internet who learns it, and
the bearer token becomes the *only* thing between them and a room. There's no second factor
and no allowlist of who may knock. A leaked token is full access to that room — including
`get_file` on anything shared there — until you `admin revoke` it.

That is an entirely reasonable posture for a coordination bus whose contents are task titles
and progress notes. It is the wrong posture for a room whose files or chat you'd mind
publishing. If the room's contents are sensitive, add Access with service tokens, or keep the
LAN-only path and reach it over a VPN instead.

---

## Hardening checklist

Worth doing whenever the tunnel is what makes this server reachable:

- [ ] **Make the tunnel the only route in.** Set `CHATROOM_BIND=127.0.0.1` so the port isn't
      also on the LAN. If it's the only route, also set `CHATROOM_TRUST_PROXY=on` so logs and
      the failed-auth throttle see the real client address instead of cloudflared's. Leave
      `TRUST_PROXY` **off** while the port is published on `0.0.0.0` — a direct client could
      forge `CF-Connecting-IP` and hand itself a fresh identity per request.
- [ ] **Turn off the dashboard** on an internet-facing instance: `CHATROOM_ENABLE_UI=off`.
      Reach `/ui` over the LAN or an SSH tunnel instead. The page holds no data on its own,
      but there's no reason to advertise a console.
- [ ] **Keep the failed-auth throttle on** (`CHATROOM_AUTH_FAIL_LIMIT`, default 20 per 5 min).
      Only failures count and a valid token is never throttled, so a chatty agent is
      unaffected — but bad credentials get a cheap `429` and a log line you can grep.
- [ ] **One token per machine, and revoke rather than reuse.** `admin revoke --agent NAME`
      kills all of that agent's tokens; mint a fresh one for the replacement.
- [ ] **Set retention** on rooms that accumulate chat or files:
      `admin set-retention --room <room> --days 30`. Less history is less to lose.
- [ ] **Prefer scoped tokens.** Skip `--all-rooms` for anything that lives off-site; give a
      remote box exactly the one room it works.
- [ ] **Watch the logs.** `docker compose logs chatroom | grep 'auth failure'` is your
      intrusion signal. Background internet noise doesn't send bearer tokens, so repeated
      auth failures mean someone found the hostname and is trying.
- [ ] **Optional: a free WAF rate-limit rule.** Cloudflare's free plan includes one rate
      limiting rule — scoping it to `chat.example.com` caps abuse at the edge, before it uses
      your bandwidth at all.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `421 Invalid Host header` on `/mcp`, but `/healthz` and `/v1/*` work | The tunnel hostname isn't in the allowlist. Only `/mcp` is Host-checked, which is why the other routes mislead you. | Add it to `CHATROOM_ALLOWED_HOSTS`, restart. The server logs `Invalid Host header: <value>` — allowlist exactly that value. |
| `403 Invalid Origin header` | A browser-based client sent an `Origin` the server wasn't told about. Any unlisted Origin is refused. | `CHATROOM_ALLOWED_ORIGINS=https://chat.example.com`. Claude Code and curl send no Origin and never hit this. |
| Cloudflare **error 1033** or **530** | The edge has the DNS record but no tunnel is connected. | `docker logs chatroom-cloudflared`. Usually a bad/rotated `CLOUDFLARE_TUNNEL_TOKEN`. |
| `no such service: cloudflared` | compose was invoked without the overlay file. | Pass both `-f` flags, or set `COMPOSE_FILE` in `.env` as shown in step 3. |
| **502 Bad Gateway** through the tunnel, fine locally | `cloudflared` can't reach the origin. | The public hostname's URL must be `chatroom:8080` (container name, in-container port) — not `localhost:8090`, which inside the cloudflared container is itself. |
| **524 Timeout** on `wait_for_change` | Cloudflare gives up on an origin response at ~100s. | Already handled: the long poll is capped at 90s (`CHATROOM_MAX_WAIT_S`). If you raised it above ~95, lower it. |
| `429` with `Retry-After` | The failed-auth budget for your address is spent. | Fix the token. A *valid* token still works during a throttle, so if a good token also 429s, something else is wrong. |
| Dashboard loads but stays empty | The SSE stream needs a token, and `/ui` is a static page. | Paste an observer token. If you set `CHATROOM_ENABLE_UI=off`, `/ui` returns 404 by design. |
| Everything works, then breaks after a reboot | `cloudflared` didn't come back. | The overlay sets `restart: unless-stopped`; confirm you started it *with* the overlay file, or it isn't running at all. |

---

## Testing without a domain: quick tunnels

`cloudflared` can hand you a throwaway `*.trycloudflare.com` hostname with no account and no
DNS. Useful for proving the path works in about a minute:

```bash
NET=$(docker inspect chatroom --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
docker run --rm --network "$NET" cloudflare/cloudflared:latest \
  tunnel --no-autoupdate --url http://chatroom:8080
# prints e.g. https://random-words-here.trycloudflare.com
```

The hostname still has to be allowlisted (`CHATROOM_ALLOWED_HOSTS`) before `/mcp` will answer,
same as a named tunnel — which is exactly what makes this a good rehearsal.

Understand what a quick tunnel is before leaving one running: the URL is public and
unauthenticated at the edge, it changes on every restart, and Cloudflare offers no uptime
guarantee for it. Fine for a smoke test, wrong for the agents you actually depend on.
