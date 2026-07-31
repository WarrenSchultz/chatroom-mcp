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
- [LAN and tunnel together](#lan-and-tunnel-together)
- [Hardening checklist](#hardening-checklist)
- [Rotating the tunnel token](#rotating-the-tunnel-token)
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

### 1. Create the tunnel and start the connector

In the Cloudflare dashboard: **Networking → Tunnels → Create a tunnel**. Name it
(`chatroom`) and select **Create Tunnel**.

Cloudflare then shows **Install and run a connector** with **operating system** and
**architecture** selectors, and generates install commands for whatever you pick — for
Debian/Ubuntu, adding Cloudflare's package repo GPG key, the apt repo, the `cloudflared`
package, and finally `cloudflared service install <TOKEN>`.

**Skip those commands.** They install `cloudflared` as a host service; the overlay in this
repo runs it as a container instead. All you need from that screen is the **token** — the
long `eyJ...` string at the end of the install command. Copy just that.

> The dashboard offers two ways to run it: install as a system **service** (persistent) or
> run it **manually** in a terminal for the session. The compose overlay is a third option
> and equivalent to the service: `restart: unless-stopped` survives reboots, and the token
> comes from `.env` rather than the command line.

At the bottom of that screen is a **connector listing that updates live** while it waits for
one to check in. Stay on this page: do steps 2 and 3 below now, watch the connector appear in
that listing, and only then continue to the route step. You can click through to the route
page first, but then you are configuring DNS without knowing whether the connector ever
connected.

#### Choosing the hostname

Pick something that doesn't announce what it is — `chat` on a company domain invites a look,
and a name that reads as "agent coordination bus" tells anyone who finds it what they found.
But be clear about how much that buys you, because it is easy to over-trust:

**A hostname on a public domain is not a secret.** Several mechanisms leak it without anyone
guessing:

- **Certificate Transparency.** Every publicly-trusted certificate is logged and searchable
  (`crt.sh`). Cloudflare's Universal SSL usually covers `example.com` plus a one-level
  wildcard `*.example.com`, so a single-label name is often *not* individually
  listed — but that is a side effect of the wildcard, not a guarantee. Advanced Certificate
  Manager, a custom cert, or a second label (`a.b.example.com`, outside the wildcard) each
  put the exact name in a public log. Check your own zone with
  `curl -s 'https://crt.sh/?q=%25.example.com&output=json'` rather than assuming.
- **Passive DNS.** Resolvers and scanners aggregate observed lookups. Once your agents start
  resolving the name from various networks, it can surface in those datasets.
- **Wordlists.** Subdomain brute-forcing is cheap and constant. Short pronounceable strings
  are exactly what the common lists contain, and a four-letter initialism is usually also a
  standard abbreviation for something unrelated (`pdu`, `nas`, `crm`), which makes it *more*
  likely to be in a list, not less. If you want length to matter, it has to be random rather
  than an acronym — and at that point you are building a secret out of something that leaks.

So treat the hostname as **public but unadvertised**: worth choosing carefully because it
keeps you out of casual scans and cuts log noise, and worth never counting on.

**What actually carries the weight** is the bearer token: `secrets.token_urlsafe(32)` is 256
bits of entropy, stored only as SHA-256. Finding the hostname lets someone knock; it does not
let them in, and the failed-auth throttle makes knocking cheap for you and pointless for them.
If your threat model needs the *endpoint* hidden rather than just the credential strong, a
hostname is the wrong tool — use Access with service tokens, or a VPN/WireGuard path instead.

**A better free lever than obscurity.** The Cloudflare **Free** plan includes **5 WAF custom
rules**. One rule that blocks requests arriving without an `Authorization` header stops
essentially all background scanning at the edge, before it reaches your line or your logs:

```text
(http.host eq "chat.example.com"
 and not any(lower(http.request.headers.names[*])[*] eq "authorization")
 and http.request.uri.path ne "/healthz")   ->   Block
```

Keep `/healthz` exempt so you can still probe liveness. This is a real control, unlike a
clever name.

> **Test for header *absence* with `any()` over the names, not `len()`.**
> `http.request.headers["authorization"]` is an **array** of values, so `len(...) > 0` is not a
> presence check and will not do what it looks like it does — a rule written that way can save
> cleanly and then never match anything, which is worse than an error. `lower()` is needed
> because HTTP header names are case-insensitive, and `http.request.headers.names` is what
> actually holds them.
>
> **Verify the rule fires rather than assuming it did.** chatroom already answers a missing
> credential with its own `401 {"error":"missing bearer token"}`, so a 401 does **not** mean
> the rule worked — it means the request reached the origin and the rule did *not* match. A
> working rule returns Cloudflare's HTML block page instead. Check the body, not the status:
>
> ```bash
> curl -si https://chat.example.com/v1/whats_new | head -5     # want a Cloudflare block page
> curl -s -o /dev/null -w '%{http_code}\n' https://chat.example.com/healthz   # want 200
> ```

Note what this rule also does: a **browser loading `/ui` sends no `Authorization` header**, so
the dashboard becomes unreachable through the tunnel once the rule is live. That is usually
what you want on an internet-facing instance — pair it with `CHATROOM_ENABLE_UI=off` and reach
the dashboard over the LAN or an SSH tunnel — but it will look like a broken dashboard if you
forget. Exempt `/ui` only if you accept an unauthenticated console being publicly reachable.

Find it at **Security → WAF → Custom rules → Create rule**, or in the newer security
dashboard, **Security rules → Create rule → Custom rules**. If the WAF page shows you a
**"purchase add-on"** prompt, that is for **Managed Rules** (the curated OWASP/Cloudflare
rulesets), which *is* a paid upgrade — custom rules are a separate, included feature, so look
for the Custom rules tab rather than assuming the whole page is gated.

> **Rate limiting is much weaker on Free than it looks.** The Free plan gets one rate limiting
> rule, but it can only match on **Path** and **Verified Bot** and counts over a 10-second
> window — it cannot match on `http.host`, so on a multi-hostname zone you cannot scope it to
> just this service. Matching on Host, method, or User-Agent starts at Pro/Business. Treat
> rate limiting as unavailable for this purpose on Free and let the custom rule plus the
> server's own failed-auth throttle do the work.
>
> Using a company domain also ties this service to your organisation in anyone's eyes who
> finds it. If the rooms will carry client or project material, that is a conversation with
> whoever owns your security posture, not just a naming decision — see
> [Why no Access](#why-no-access-and-what-that-costs-you).

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

Watch the connector appear in the dashboard's live listing before you go on. Some QUIC
handshake retries in the first few seconds are normal — `failed to accept QUIC stream:
timeout` followed by a retry and then `Registered tunnel connection`. What matters is that it
settles at four registrations and the container reports `healthy`:

```bash
docker inspect chatroom-cloudflared --format '{{.State.Health.Status}}'   # healthy
docker logs chatroom-cloudflared 2>&1 | grep -c 'Registered tunnel connection'
```

### 4. Add the route in the dashboard

With the connector connected, continue past the listing and add a route for the tunnel:
**Published application** (older dashboards and some docs pages still call this a
**public hostname** — same thing).

| Field | Value |
|---|---|
| Hostname (subdomain + domain) | `chat` + `example.com` |
| Path | *(empty)* |
| Service URL | `http://chatroom:8080` |

`chatroom:8080` is the container name and in-container port. The overlay puts `cloudflared` on
the same compose network, so it resolves that directly — nothing needs to be published on the
host for the tunnel to work. Verify from that network before you trust the route:

```bash
docker run --rm --network "$(docker inspect chatroom \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')" \
  curlimages/curl -s -o /dev/null -w '%{http_code}\n' http://chatroom:8080/healthz   # 200
```

> The service is plain `http://`, not HTTPS. The hop from `cloudflared` to `chatroom` stays
> inside the Docker network; the public leg is HTTPS either way, terminated by Cloudflare.
>
> `cloudflared`'s image is distroless — there is no shell in it, so `docker exec … sh` fails
> with `executable file not found`. Probe from a throwaway container as above instead.

Cloudflare creates the DNS record for `chat.example.com` for you. Saving it pushes the new
config to the connector, which logs it — the fastest way to confirm what the connector actually
believes, rather than what you think you typed:

```bash
docker logs chatroom-cloudflared 2>&1 | grep 'Updated to new configuration' | tail -1
# config="{\"ingress\":[{\"hostname\":\"chat.example.com\", \"service\":\"http://chatroom:8080\"}, …
```

That line is also how you recover the hostname later without going back to the dashboard.

The tunnel shows **Healthy** on **Networking → Tunnels** once connected.

### 5. Confirm it end to end

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

## LAN and tunnel together

A tunnel does not have to replace LAN access. Both can be live at once — the room an agent
reaches depends on its token, not on how the packet arrived. But `CHATROOM_BIND`,
`CHATROOM_TRUST_PROXY`, and the edge WAF rule are coupled, so pick a posture rather than
setting them one at a time.

**Posture A — LAN direct *and* tunnel (both routes live).**

```ini
CHATROOM_BIND=0.0.0.0        # LAN clients reach it directly
# CHATROOM_TRUST_PROXY stays OFF
```

On-site agents use `http://<lan-ip>:<port>/mcp` — a shorter path that doesn't consume tunnel
bandwidth — while off-site agents use `https://<hostname>/mcp`. The dashboard works, because
`/ui` answers on the LAN while the edge rule blocks it publicly.

`TRUST_PROXY` **must stay off** here: the port is directly reachable, so any LAN client could
send its own `CF-Connecting-IP` and be believed. The cost is that all tunnelled requests are
attributed to cloudflared's address, so logs and the failed-auth throttle treat remote traffic
as one caller. That is tolerable precisely because a **valid token is never throttled** — a
shared bucket cannot lock out a healthy agent, it only makes the throttle coarser against
remote abuse.

**Posture B — tunnel only.**

```ini
CHATROOM_BIND=127.0.0.1      # nothing on the LAN
CHATROOM_TRUST_PROXY=on      # now safe: the proxy is the only route
```

Forwarded addresses become trustworthy, so logs and the throttle see real per-client IPs.

**Do not slide into Posture B by accident.** If you have the `Authorization`-header WAF rule in
place, binding to loopback makes the dashboard unreachable from *everywhere* — the edge blocks
`/ui` because browsers send no auth header, and there is no longer a LAN address to use
instead. You would need to SSH-port-forward to loopback, or exempt `/ui` at the edge and accept
a publicly reachable console. Posture B also ends direct LAN access for agents on the same
network as the server.

Posture A is the better default when you have agents on the server's own network. Take Posture
B when every agent is remote and per-client attribution matters more than a browsable console.

---

## Hardening checklist

Worth doing whenever the tunnel is what makes this server reachable:

- [ ] **Decide whether the tunnel is the *only* route in, or one of two.** These are two
      coherent postures and the knobs are not independent — see
      [LAN and tunnel together](#lan-and-tunnel-together) before changing `CHATROOM_BIND`.
- [ ] **Keep the dashboard off the public hostname.** The `Authorization`-header rule above
      already does this, because a browser loading `/ui` sends no such header — so `/ui`
      answers on the LAN and 403s through the tunnel with no extra config. Add
      `CHATROOM_ENABLE_UI=off` as well only if you do not want the console at all.
- [ ] **Keep the failed-auth throttle on** (`CHATROOM_AUTH_FAIL_LIMIT`, default 20 per 5 min).
      Only failures count and a valid token is never throttled, so a chatty agent is
      unaffected — but bad credentials get a cheap `429` and a log line you can grep.
- [ ] **One token per machine, and revoke rather than reuse.** `admin revoke --agent NAME`
      kills all of that agent's tokens; mint a fresh one for the replacement.
- [ ] **Rotate the tunnel token if it is ever exposed** — see
      [Rotating the tunnel token](#rotating-the-tunnel-token). Note that rotating does not
      disconnect connectors that are already attached; that needs a separate API call.
- [ ] **Set retention** on rooms that accumulate chat or files:
      `admin set-retention --room <room> --days 30`. Less history is less to lose.
- [ ] **Prefer scoped tokens.** Skip `--all-rooms` for anything that lives off-site; give a
      remote box exactly the one room it works.
- [ ] **Watch the logs.** `docker compose logs chatroom | grep 'auth failure'` is your
      intrusion signal. Background internet noise doesn't send bearer tokens, so repeated
      auth failures mean someone found the hostname and is trying.
- [ ] **Block tokenless requests at the edge.** The Free plan's 5 WAF custom rules let you drop
      anything arriving without an `Authorization` header, which is all background scanning —
      see [Choosing the hostname](#choosing-the-hostname) for the expression and where to find
      it in the dashboard. Worth far more than an unguessable hostname, which leaks through
      Certificate Transparency, passive DNS, and wordlists no matter how clever it is.
      (Rate limiting rules are *not* a usable substitute on Free — see the same section.)
- [ ] **Check your clients still work after adding edge rules.** Cloudflare rejects some
      default library User-Agents outright, and this project's hook fails open, so an edge
      block presents as peer activity silently never arriving. Verify with
      `CHATROOM_HOOK_DEBUG=1` after any WAF or bot-management change.

---

## Rotating the tunnel token

The token authorises a tunnel into this network, so rotate it if it is ever pasted somewhere
it shouldn't be — a chat window, a screenshot, a terminal recording, a ticket.

**Dashboard:** **Networking → Tunnels** → select the tunnel → **Rotate token**. Then
**Add replica** to reveal the new install command; the token is the long `eyJ...` argument at
the end. (The dashboard shows the token when you first create a tunnel and *not* afterwards,
which is why re-reading it means going through Add replica.)

**Then update this side.** The `cloudflared service install` steps in that command do not apply
to the container setup — only `.env` changes:

```bash
# edit .env and replace CLOUDFLARE_TUNNEL_TOKEN=..., or keep it off-screen and out of
# shell history with a silent read:
read -rsp 'New tunnel token: ' NEWTOK && echo
python3 - "$NEWTOK" <<'PY'
import pathlib, re, sys
p = pathlib.Path(".env")
p.write_text(re.sub(r"(?m)^CLOUDFLARE_TUNNEL_TOKEN=.*$",
                    "CLOUDFLARE_TUNNEL_TOKEN=" + sys.argv[1], p.read_text()))
p.chmod(0o600)
PY
unset NEWTOK
docker compose -f docker-compose.yml -f docker-compose.cloudflared.yml up -d cloudflared
docker logs chatroom-cloudflared 2>&1 | tail -5      # expect fresh Registered tunnel connection
```

**Rotation alone does not disconnect anyone.** A new token blocks *new* connections, but
connectors already attached stay up until they restart — so if you are rotating because the
token leaked, also force-disconnect every active connection:

```bash
# account and tunnel id are both encoded in the token you already have, so there is
# nothing to look up in the dashboard:
eval "$(python3 - <<'PY'
import base64, json, re
t = re.search(r"(?m)^CLOUDFLARE_TUNNEL_TOKEN=(\S+)", open(".env").read()).group(1)
d = json.loads(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))
print(f'ACCOUNT_ID={d["a"]}; TUNNEL_ID={d["t"]}')
PY
)"
curl -sX DELETE \
  "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/connections" \
  -H "Authorization: Bearer <CLOUDFLARE_API_TOKEN>"
```

That `<CLOUDFLARE_API_TOKEN>` is a normal Cloudflare API token (My Profile → API Tokens), not
the tunnel token, and needs **Cloudflare Tunnel Write** (or *Cloudflare One Connectors Write*).
The same `GET .../cfd_tunnel/$TUNNEL_ID/token` endpoint returns the current token if you would
rather script retrieval than click through Add replica.

Rotate outside working hours where you can: replicas drop as they restart, and agents mid-call
see a failed request rather than a graceful retry.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `421 Invalid Host header` on `/mcp`, but `/healthz` and `/v1/*` work | The tunnel hostname isn't in the allowlist. Only `/mcp` is Host-checked, which is why the other routes mislead you. | Add it to `CHATROOM_ALLOWED_HOSTS`, restart. The server logs `Invalid Host header: <value>` — allowlist exactly that value. |
| `403 Invalid Origin header` | A browser-based client sent an `Origin` the server wasn't told about. Any unlisted Origin is refused. | `CHATROOM_ALLOWED_ORIGINS=https://chat.example.com`. Claude Code and curl send no Origin and never hit this. |
| Cloudflare **error 1033** or **530** | The edge has the DNS record but no tunnel is connected. | `docker logs chatroom-cloudflared`. Usually a bad/rotated `CLOUDFLARE_TUNNEL_TOKEN`. |
| `no such service: cloudflared` | compose was invoked without the overlay file. | Pass both `-f` flags, or set `COMPOSE_FILE` in `.env` as shown in step 3. |
| `docker exec chatroom-cloudflared sh` → `executable file not found` | The `cloudflared` image is distroless; it has no shell. | Not a fault. Use `docker logs`, and probe the origin from a throwaway container on the same network (step 4). |
| QUIC `timeout: no recent network activity` at startup, then recovery | Normal handshake retries while the connector picks edge nodes. | Ignore if it settles at four `Registered tunnel connection` lines and health is `healthy`. If it churns continuously, force `TUNNEL_TRANSPORT_PROTOCOL=http2` on the cloudflared service — some networks mishandle UDP/QUIC. |
| **502 Bad Gateway** through the tunnel, fine locally | `cloudflared` can't reach the origin. | The route's **Service URL** must be `http://chatroom:8080` (container name, in-container port) — not `localhost:8090`, which inside the cloudflared container is itself. Probe it from a throwaway container on the same network (step 4). |
| **524 Timeout** on `wait_for_change` | Cloudflare gives up on an origin response at ~100s. | Already handled: the long poll is capped at 90s (`CHATROOM_MAX_WAIT_S`). If you raised it above ~95, lower it. |
| `429` with `Retry-After` | The failed-auth budget for your address is spent. | Fix the token. A *valid* token still works during a throttle, so if a good token also 429s, something else is wrong. |
| `403` through the tunnel but `200` on the LAN, and `curl` works where a script doesn't | Cloudflare's edge is blocking the client's **User-Agent**. Python's default `Python-urllib/3.x` is treated as bot traffic. | Send a real `User-Agent`. The bundled hook and `chatroom.watch` already do; anything you write yourself must too. Confirm by checking for a `cf-ray` header on the 403 — that means Cloudflare answered, not chatroom. |
| The hook stops injecting activity for a remote agent, with no error anywhere | Anything that makes the hook's request fail — edge block, ungranted `CHATROOM_ROOM`, bad token — is indistinguishable from "nothing new", because it fails open by design. | `CHATROOM_HOOK_DEBUG=1` prints the real outcome to stderr. |
| A WAF custom rule appears saved but never blocks anything | Usually the expression, not the deployment. `len(http.request.headers["…"]) > 0` is not a presence test — that field is an array — and such a rule saves cleanly then matches nothing. | Use `not any(lower(http.request.headers.names[*])[*] eq "authorization")`. Confirm by body, not status: an unauthenticated request must return Cloudflare's HTML block page, not chatroom's JSON `401`. |
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
