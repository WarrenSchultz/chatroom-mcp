#!/usr/bin/env python3
# Copyright 2026 Warren Schultz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Long-lived chatroom watcher: turn server-pushed events into agent wake-ups.

The companion hook (chatroom_whats_new.py) is *pull*. It can only run when the
agent runs, so an agent parked at the prompt learns nothing until its human comes
back, and a working agent learns nothing for up to CHATROOM_HOOK_MIN_INTERVAL
seconds. This is the *push* half: it holds the server's SSE stream open and prints
one line per notification. Point Claude Code's Monitor tool at it and each line
becomes a wake-up, including while the agent is idle.

    Monitor(command="python3 ~/.claude/hooks/chatroom_watch.py",
            description="chatroom <your-room>", persistent=True)

Every line printed here costs a model turn, so what NOT to print is the whole
design. Three modes, and the mode can change while it runs:

    hook-only   print nothing. At launch this exits immediately (nothing to arm);
                set at runtime it mutes a running watcher so it can be unmuted.
    mentions    print only when this agent is named by someone else (default)
    all         print every chat message and board event

Volume control:

  * In "all", non-mention traffic is COALESCED, not dropped. Events accumulate and
    go out as one summary line at most every CHATROOM_WATCH_MIN_INTERVAL seconds
    (default 60), so a busy room costs one turn a minute, not one turn a message.
  * In "mentions", non-mention traffic is DROPPED. That is the point of the mode,
    but note what a mention is: this agent's NAME in the text, bare or @-prefixed,
    word-bounded. If peers write "you", or use a nickname that is not the agent id,
    nothing matches and this watcher stays silent while looking perfectly healthy —
    connected, no errors, just nothing it considers addressed here. Set
    CHATROOM_WATCH_MENTIONS (or --set-mentions) to the names people actually use,
    or run "all".
    Two things exist because that failure is invisible rather than loud:
      - the resolved pattern is printed to stderr at startup and by --set-mentions,
        unconditionally, so a mis-targeted watcher is one glance to spot instead of
        indistinguishable from a quiet room;
      - a bare numeric run in the agent name is matched too, so "the 4821" reaches
        agent srv4821 without every operator rediscovering the gap. Off with
        CHATROOM_WATCH_ALIAS=off.
  * Mentions BYPASS the window and flush anything pending with them. That is what
    lets two agents hold a real conversation at full speed while the same settings
    keep an unrelated flood down to a trickle — without anyone switching modes.

Self-authored events never notify: an agent does not need waking for its own work.

Environment:
    CHATROOM_URL     base URL of the bus         (default http://127.0.0.1:8080)
    CHATROOM_TOKEN   this box's token            (required)
    CHATROOM_ROOM    override room               (optional; must be one the token grants)
    CHATROOM_WATCH_MODE              hook-only | mentions | all   (default mentions)
    CHATROOM_WATCH_MIN_INTERVAL      coalescing window, seconds   (default 60; 0 = off)
    CHATROOM_WATCH_MENTION_PRIORITY  let mentions skip the window (default on)
    CHATROOM_WATCH_MENTIONS          extra names that count as a mention of you,
                                     comma-separated (e.g. all-hands,everyone)
    CHATROOM_WATCH_ALIAS             also match the bare numeric run in the agent
                                     name, e.g. "4821" for srv4821 (default on)
    CHATROOM_WATCH_DEBUG=1           narrate decisions on stderr

Mode precedence: the state file (written by --set-mode) beats --mode, which beats
CHATROOM_WATCH_MODE, which defaults to "mentions". Extra mentions work the same way:
--set-mentions beats CHATROOM_WATCH_MENTIONS. The file is re-read as it runs, so
either can retarget a RUNNING watcher from any shell — or from the agent itself —
without a restart. That matters most for mentions, because the operator who needs a
new alias is by definition one whose watcher is currently missing messages.

stdout is reserved for notifications, one per line: it is the event stream Monitor
reads. Everything diagnostic goes to stderr.

Unlike the hook, this is a long-lived process, so import cost is irrelevant here
and nothing is deferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.2.0"

MODES = ("hook-only", "mentions", "all")
DEFAULT_MODE = "mentions"

#: Socket read timeout. Must exceed the server's keepalive interval (15s), because a
#: timeout here is NOT recoverable: http.client latches _timeout_occurred on the socket
#: file object and every later read raises "cannot read from timed out object". Treating
#: it as a tick therefore reconnects the stream every tick — which passes a short test
#: and quietly rebuilds a TLS connection every few seconds in production. So a timeout
#: means the connection is genuinely dead, and the only answer is to reconnect.
READ_TIMEOUT = 45.0

#: The server's keepalive comments are what wake an otherwise idle reader, so they —
#: not a socket timeout — set how promptly a coalesced batch can flush. A 60s window
#: therefore flushes within about 60-75s, which is the intended "at most once a minute".
KEEPALIVE_S = 15.0

#: Do not report a blip. Report an outage — but only once, so a flapping link does not
#: become the notification storm the watcher exists to prevent.
OUTAGE_REPORT_AFTER = 120.0

MAX_LINE = 400
MAX_BODY = 70
CONNECT_TIMEOUT = 10.0

# See the note in chatroom_whats_new.py: urllib's default User-Agent reads as bot
# traffic and gets 403'd at a Cloudflare edge, which would present here as a room
# that simply never says anything.
USER_AGENT = "chatroom-watch/1.0 (+https://github.com/WarrenSchultz/chatroom-mcp)"


def _debug(msg: str) -> None:
    # Timestamped: the questions this answers are almost always "how long did the
    # connection last" and "how often is it redialling", which need the clock.
    if os.environ.get("CHATROOM_WATCH_DEBUG", "").strip().lower() in ("1", "true", "on", "yes"):
        print(f"[chatroom-watch {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "on", "yes")


def _emit(line: str) -> None:
    """One notification. Single line, bounded, flushed.

    Monitor turns each stdout line into a wake-up, so a stray newline inside a chat
    body would split one notification into several. Collapse whitespace and truncate.
    """
    flat = " ".join(line.split())
    if len(flat) > MAX_LINE:
        flat = flat[:MAX_LINE - 1] + "…"
    print(flat, flush=True)


# ------------------------------------------------------------------ mode file

def _state_path(base: str, token: str, room: str) -> str:
    """Per-(server, credential, room) mode file, in the same spirit as the hook's
    throttle marker: two agents on one box must not share state."""
    key = hashlib.sha256(f"{base}|{token}|{room}".encode()).hexdigest()[:16]
    return os.path.join(os.environ.get("TMPDIR") or "/tmp", f"chatroom-watch-{key}")


def _read_state(path: str) -> dict[str, str]:
    """key=value lines. A bare mode word is the pre-1.2 format and still reads."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
        elif line in MODES:
            out["mode"] = line
    return out


def _write_state(path: str, **kw: str | None) -> None:
    """Write the state file, preferring a format OLD readers can still parse.

    The compatibility that matters here runs old-reads-new, not new-reads-old. This
    file exists to steer a watcher that has been running for days, so the reader is
    *by construction* older than any format change — and a pre-1.2 reader consumes the
    whole file and compares it to MODES, so `mode=hook-only` parses as None and it
    silently falls back to its launch mode. That is a control that reports success,
    writes verifiably correct state, and does nothing.

    So when mode is the only thing set, write the bare word both versions accept.
    """
    cur = _read_state(path)
    cur.update({k: v for k, v in kw.items() if v is not None})
    cur = {k: v for k, v in cur.items() if v != ""}
    with open(path, "w", encoding="utf-8") as fh:
        if set(cur) == {"mode"}:
            fh.write(cur["mode"] + "\n")
        else:
            for k, v in sorted(cur.items()):
                fh.write(f"{k}={v}\n")


def _read_mode(path: str) -> str | None:
    val = _read_state(path).get("mode")
    return val if val in MODES else None


# ------------------------------------------------------------------ identity

def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


#: Answers that mean "this will never work" — a wrong credential or a hostname the
#: server will not accept. Retrying those is pointless noise. Anything else at connect
#: time (502/503/504 from an edge, a refused socket) is transient by default.
FATAL_CODES = (401, 403, 421)

#: How long to keep trying to establish identity before giving up. A watcher armed
#: during a transient edge 502 must not die of it — observed in the field, from a second
#: tunnel client, on the very first connect attempt. Bounded rather than infinite so a
#: genuinely dead bus still surfaces as a Monitor exit instead of a silent no-op.
IDENTITY_RETRY_S = 60.0


def _identity(base: str, token: str, room: str | None,
              retry_s: float = IDENTITY_RETRY_S) -> tuple[str, str]:
    """Ask the server who this credential is, retrying transient failures.

    /v1/rooms is side-effect free — it does not touch the whats_new cursor the hook
    depends on.

    retry_s=0 for one attempt: the retry budget exists so a watcher ARMING during an
    edge blip survives it, but --selfcheck is a diagnostic a human is waiting on, and
    making them wait a minute to be told the bus is down is its own defect.
    """
    deadline = time.time() + retry_s
    delay = 1.0
    while True:
        try:
            info = _get_json(f"{base}/v1/rooms", token)
            agent = str(info.get("agent") or "")
            return agent, room or str(info.get("default_room") or "")
        except urllib.error.HTTPError as exc:
            if exc.code in FATAL_CODES or time.time() >= deadline:
                raise
            _debug(f"identity: HTTP {exc.code}, retrying in {delay:.0f}s")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if time.time() >= deadline:
                raise
            _debug(f"identity: {exc}, retrying in {delay:.0f}s")
        time.sleep(delay)
        delay = min(delay * 2, 15.0)


def _aliases(agent: str) -> list[str]:
    """Short forms peers actually use: the bare numeric run in a name like srv4821.

    Measured against a real room before defaulting this on: across 234k characters,
    the bare form produced zero false matches and one genuine reference the full-name
    pattern silently dropped ("your cpuset-120 vs 4821's 172"). The cost was nil and
    the miss was real, so this is on by default — but it is only ever a heuristic about
    how humans abbreviate, so 4+ digits (a `box-80` must not collide with port numbers)
    and CHATROOM_WATCH_ALIAS=off to disable.
    """
    if not _flag("CHATROOM_WATCH_ALIAS", True):
        return []
    if not re.search(r"[A-Za-z]", agent):        # an all-digit name aliases to itself
        return []
    m = re.search(r"(?<!\d)(\d{4,})(?!\d)", agent)
    return [m.group(1)] if m else []


def _mention_re(agent: str, extra: str) -> "re.Pattern[str] | None":
    """Match the agent's own name, with or without a leading @.

    Bare-name matching rather than @-only: agents mention each other in prose at
    least as often as they use the sigil, and agent names are hostname-shaped and
    distinctive enough that a boundary check avoids most false hits. A very short or
    dictionary-word agent name would match too eagerly — name agents accordingly.
    """
    names = [n.strip() for n in ([agent] + _aliases(agent) + extra.split(",")) if n.strip()]
    if not names:
        return None
    seen: list[str] = []
    for n in names:
        if n.lower() not in {s.lower() for s in seen}:
            seen.append(n)
    alts = "|".join(re.escape(n) for n in sorted(seen, key=len, reverse=True))
    # (?: ) is load-bearing, not tidiness. "|" binds looser than concatenation, so
    # `(?<!..)a|b(?!..)` means "(boundary,a) OR (b,boundary)" — the lookbehind guards
    # only the first alternative and the lookahead only the last. With one name there is
    # no "|" and it was accidentally correct; the moment a second name existed (i.e. the
    # instant anyone set CHATROOM_WATCH_MENTIONS) it matched "srv4821x" and "x7740".
    return re.compile(rf"(?<![0-9A-Za-z_])(?:{alts})(?![0-9A-Za-z_])", re.IGNORECASE)


# ------------------------------------------------------------------ rendering

def _clip(text: str, limit: int = MAX_BODY) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _describe(kind: str, payload: dict) -> tuple[str, str, str]:
    """(actor, one-line description, searchable text) for one SSE frame."""
    if kind == "chat":
        actor = str(payload.get("author") or "?")
        body = str(payload.get("body") or "")
        return actor, f'msg#{payload.get("id")} "{_clip(body)}"', body
    actor = str(payload.get("actor") or "?")
    ekind = str(payload.get("kind") or "event")
    detail = str(payload.get("detail") or "")
    task = payload.get("task_id")
    label = f"{ekind} #{task}" if task else ekind
    if detail:
        label = f"{label} {_clip(detail, 50)}"
    return actor, label, f"{detail} {ekind}"


def _summary(room: str, items: list[tuple[str, str]]) -> str:
    parts = [f"{a}: {d}" for a, d in items]
    return f"[chatroom] {room}: {len(items)} new — " + "; ".join(parts)


# ------------------------------------------------------------------ SSE

def _frames(base: str, token: str, room: str, after: int, after_msg: int):
    """Yield ("chat"|"activity"|"mark", payload), and (None, None) on each keepalive.

    The keepalive yield is what lets the caller flush a coalesced batch without waiting
    for the next real frame. A "mark" carries the server's resolved high-water marks
    from the greeting comment.

    after < 0 means "start from now": on a cold start there is no backlog worth
    replaying, because every replayed event would cost a model turn.
    """
    url = f"{base}/v1/stream?room={urllib.parse.quote(room)}"
    url += f"&after={after}" if after >= 0 else "&after=now"
    # Sending after_msg=0 on a cold start would defeat after=now: an explicit value wins
    # over the server's resolution, and 0 means "replay every message ever posted".
    if after_msg >= 0:
        url += f"&after_msg={after_msg}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
        "Accept": "text/event-stream", "Cache-Control": "no-cache",
    })
    resp = urllib.request.urlopen(req, timeout=READ_TIMEOUT)
    _debug(f"connected: {url}")
    event = None
    try:
        while True:
            try:
                raw = resp.readline()
            except (socket.timeout, TimeoutError, OSError) as exc:
                # Nothing for READ_TIMEOUT despite keepalives every KEEPALIVE_S: the
                # connection is dead. It is also unusable now, so return and redial.
                _debug(f"read timed out after {READ_TIMEOUT:.0f}s ({exc}); reconnecting")
                return
            if not raw:
                _debug("stream closed by peer")
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(":"):
                marks = dict(re.findall(r"(after|after_msg)=(\d+)", line))
                if marks:
                    yield "mark", {k: int(v) for k, v in marks.items()}
                else:
                    yield None, None      # keepalive: proof of life, no payload
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event in ("chat", "activity"):
                try:
                    yield event, json.loads(line[5:].strip())
                except ValueError:
                    _debug(f"unparseable data frame for event {event}")
            elif not line:
                event = None
    finally:
        resp.close()


# ------------------------------------------------------------------ main loop

def watch(base: str, token: str, room: str, agent: str, launch_mode: str) -> int:
    state = _state_path(base, token, room)
    window = float(os.environ.get("CHATROOM_WATCH_MIN_INTERVAL", "60"))
    priority = _flag("CHATROOM_WATCH_MENTION_PRIORITY", True)

    # Re-read like the mode, not fixed at entry. Building this once meant --set-mentions
    # could not take effect without a restart, while --set-mode could — an inconsistency
    # that mattered because the operator who needs a new alias is, by definition, one
    # whose watcher is currently missing messages.
    extra = ""
    mentions = None

    def retarget(src: str) -> None:
        """Rebuild the pattern and SAY what it is.

        A mis-targeted watcher and a healthy one are indistinguishable — both sit
        connected and silent. Printing the resolved pattern unconditionally (not behind
        DEBUG) is what turns "never fires" from invisible into one glance.
        """
        nonlocal mentions
        mentions = _mention_re(agent, src)
        shown = mentions.pattern if mentions else "(none — nothing will ever match)"
        print(f"[chatroom-watch] matching: /{shown}/i", file=sys.stderr, flush=True)

    # High-water marks, so a reconnect resumes instead of replaying. -1 means "not yet
    # established" and asks the server for "now". The server also backfills recent
    # history for the dashboard's benefit, and an older one ignores these parameters
    # entirely, so the marks are enforced client-side as well as sent.
    seen_event = -1
    seen_msg = -1
    pending: list[tuple[str, str]] = []
    last_emit = 0.0
    down_since = 0.0
    reported_outage = False
    backoff = 1.0

    def flush(now: float) -> None:
        nonlocal pending, last_emit
        if pending:
            _emit(_summary(room, pending))
            pending = []
            last_emit = now

    def beat() -> None:
        """Touch a heartbeat file so something else can tell this watcher is alive.

        A watcher dies silently: it is a background process whose whole job is to be quiet,
        so "no notifications" looks identical whether it is idle or gone. Its lifetime is
        also the host session's, so it does not survive a restart of that session. The
        companion hook reads this file (opt-in, CHATROOM_WATCH_EXPECTED=1) and says so.

        Touched on every frame INCLUDING idle keepalives, so a healthy-but-quiet room still
        beats. Failure here is ignored — liveness reporting must never take down the thing
        whose liveness it reports.
        """
        try:
            tmp = os.environ.get("TMPDIR") or "/tmp"
            with open(os.path.join(tmp, "chatroom-watch-heartbeat"), "w") as fh:
                fh.write(f"{int(time.time())} {agent} {room} {__version__}\n")
        except OSError:
            pass

    beat()                              # report alive at startup, before the first frame
    while True:
        try:
            for kind, payload in _frames(base, token, room, seen_event, seen_msg):
                now = time.time()
                beat()
                if down_since:
                    if reported_outage:
                        _emit(f"[chatroom] {room}: stream recovered after "
                              f"{int(now - down_since)}s")
                    down_since = 0.0
                    reported_outage = False
                    backoff = 1.0

                st = _read_state(state)
                mode = st.get("mode") if st.get("mode") in MODES else launch_mode
                want = st.get("mentions", os.environ.get("CHATROOM_WATCH_MENTIONS", ""))
                if mentions is None or want != extra:
                    extra = want
                    retarget(extra)
                if kind is None:                       # idle tick or keepalive
                    if mode != "hook-only" and window > 0 and now - last_emit >= window:
                        flush(now)
                    continue
                if kind == "mark":
                    # Where the server says "now" was. Only ever moves forward, so a
                    # reconnect cannot rewind past events already notified.
                    seen_event = max(seen_event, payload.get("after", 0))
                    seen_msg = max(seen_msg, payload.get("after_msg", 0))
                    _debug(f"marks seeded: event={seen_event} msg={seen_msg}")
                    continue

                ident = int(payload.get("id") or 0)
                if kind == "chat":
                    if ident <= seen_msg:
                        continue
                    seen_msg = ident
                else:
                    if ident <= seen_event:
                        continue
                    seen_event = ident
                    # Every posted message also produces an activity event of kind
                    # "message". Notifying on both would double every chat line; the
                    # chat frame is the better one (real body, citable message id).
                    if str(payload.get("kind")) == "message":
                        continue

                actor, desc, text = _describe(kind, payload)
                if actor == agent:
                    continue                            # never wake me for my own work
                if mode == "hook-only":
                    pending = []                        # muted: do not bank a backlog
                    continue

                hit = bool(mentions and mentions.search(text))
                if mode == "mentions" and not hit:
                    continue
                if hit and priority:
                    pending.append((actor, f"@you {desc}"))
                    flush(now)
                    continue
                pending.append((actor, desc))
                if window <= 0 or now - last_emit >= window:
                    flush(now)

        except urllib.error.HTTPError as exc:
            # A 502/503/504 from the edge is transient and falls through to the backoff
            # below; only a credential or hostname the server will never accept is fatal.
            _debug(f"HTTP {exc.code} — {exc.reason}")
            if exc.code in FATAL_CODES:
                _emit(f"[chatroom] {room}: watcher stopping — HTTP {exc.code} "
                      f"(credential or hostname rejected). Fix it and re-arm.")
                return 1
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            _debug(f"stream error: {exc}")
        except ValueError as exc:
            _debug(f"malformed stream: {exc}")

        now = time.time()
        if not down_since:
            down_since = now
        elif not reported_outage and now - down_since >= OUTAGE_REPORT_AFTER:
            # Silence must not read as calm: say so once, then go quiet again.
            _emit(f"[chatroom] {room}: stream down {int(now - down_since)}s, "
                  f"still retrying — peer activity is NOT reaching you")
            reported_outage = True
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stream chatroom events as Monitor notifications.")
    ap.add_argument("--mode", choices=MODES, help="launch mode (default $CHATROOM_WATCH_MODE or mentions)")
    ap.add_argument("--set-mode", choices=MODES, metavar="MODE",
                    help="retarget a running watcher and exit")
    ap.add_argument("--set-mentions", metavar="NAMES",
                    help="extra names that count as a mention of you, comma-separated; "
                         "takes effect on a RUNNING watcher (empty string clears)")
    ap.add_argument("--selfcheck", action="store_true", help="print version, digest, settings")
    args = ap.parse_args()

    base = os.environ.get("CHATROOM_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.environ.get("CHATROOM_TOKEN", "")
    room_env = os.environ.get("CHATROOM_ROOM") or None

    if args.selfcheck:
        try:
            with open(os.path.abspath(__file__), "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            digest = "unreadable"
        print(f"chatroom_watch.py {__version__}")
        print(f"sha256:        {digest}")
        print(f"url:           {base}")
        print(f"token:         {'set' if token else 'MISSING'}")
        launch_mode = os.environ.get("CHATROOM_WATCH_MODE") or DEFAULT_MODE

        # "What is my watcher doing right now" is a LIVE question, so answer it live.
        # This used to print the env/default only — honestly labelled, but it would say
        # "mentions" while a running watcher was in "all", forever, and this is the
        # command an operator runs precisely to check that. Same invisible-state defect
        # as an unprintable match pattern; the room caught it one line over from the fix.
        # Needs the room name to locate the per-(server, credential, room) state file,
        # and the room comes from the server unless CHATROOM_ROOM pins it — so this is
        # attempted, not assumed, and says so when it cannot.
        agent = room = None
        why = ""
        if token:
            try:
                agent, room = _identity(base, token, room_env, retry_s=0)
            except Exception as exc:                                  # noqa: BLE001
                why = f"({type(exc).__name__}: {exc})"
        print(f"agent:         {agent or '(unresolved)'}")
        print(f"room:          {room or room_env or '(token default)'}")
        if room:
            sp = _state_path(base, token, room)
            st = _read_state(sp)
            live = st.get("mode") if st.get("mode") in MODES else None
            print(f"mode:          {live or launch_mode}"
                  f"  ({'state file' if live else 'env/default'}"
                  f"{'' if live else ''}; launch default {launch_mode})")
            print(f"state file:    {sp}{'' if st else '  (none yet)'}")
            extra = st.get("mentions", os.environ.get("CHATROOM_WATCH_MENTIONS", ""))
            mre = _mention_re(agent or "", extra)
            print(f"extra mentions:       {extra or '(none)'}"
                  f"{'  (from state file)' if 'mentions' in st else ''}")
            print(f"matching:      /{mre.pattern if mre else '(none)'}/i")
        else:
            print(f"mode:          {launch_mode} (env/default) "
                  f"— could NOT read live mode {why}")
            print(f"extra mentions:       "
                  f"{os.environ.get('CHATROOM_WATCH_MENTIONS', '') or '(none)'}")
            print("matching:      (unresolved — needs the agent name from the server)")
        print(f"window:        {os.environ.get('CHATROOM_WATCH_MIN_INTERVAL', '60')}s"
              f"  (CHATROOM_WATCH_MIN_INTERVAL)")
        print(f"mention skips window: {_flag('CHATROOM_WATCH_MENTION_PRIORITY', True)}")
        print(f"alias derivation:     {_flag('CHATROOM_WATCH_ALIAS', True)}"
              f"  (CHATROOM_WATCH_ALIAS; bare 4+ digit run in the agent name)")
        return 0

    if not token:
        print("CHATROOM_TOKEN is not set", file=sys.stderr)
        return 2

    try:
        agent, room = _identity(base, token, room_env)
    except urllib.error.HTTPError as exc:
        # Distinguished from "unreachable" deliberately: a 401 here means the server
        # answered and rejected the credential, which is a different fix entirely.
        hint = {401: "token not recognised", 403: "token lacks access to that room",
                421: "hostname missing from CHATROOM_ALLOWED_HOSTS"}.get(exc.code, exc.reason)
        print(f"cannot identify this token against {base}: HTTP {exc.code} ({hint})",
              file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cannot reach {base}: {exc}", file=sys.stderr)
        return 2
    if not agent or not room:
        print(f"server did not identify this token (agent={agent!r} room={room!r})", file=sys.stderr)
        return 2

    state = _state_path(base, token, room)
    if args.set_mode is not None or args.set_mentions is not None:
        _write_state(state, mode=args.set_mode, mentions=args.set_mentions)
        st = _read_state(state)
        print(f"{agent} in {room}: mode={st.get('mode', '(launch default)')} "
              f"mentions={st.get('mentions', '') or '(none)'}", file=sys.stderr)
        # Say what will actually match, so a wrong alias is caught here rather than by
        # noticing weeks later that the watcher has never fired.
        mre = _mention_re(agent, st.get("mentions", ""))
        print(f"matching: /{mre.pattern if mre else '(none)'}/i", file=sys.stderr)
        return 0

    launch = args.mode or os.environ.get("CHATROOM_WATCH_MODE") or DEFAULT_MODE
    if launch not in MODES:
        print(f"unknown mode {launch!r}; expected one of {', '.join(MODES)}", file=sys.stderr)
        return 2

    # A mode file left over from a previous run outranks the launch flag by design, so
    # that --set-mode survives a restart. Clear it to make --mode authoritative again.
    effective = _read_mode(state) or launch
    if effective == "hook-only" and launch == "hook-only":
        print("mode is hook-only; nothing to watch (the hook is the only path)", file=sys.stderr)
        return 0

    print(f"watching {room} as {agent}, mode={effective}", file=sys.stderr)
    try:
        return watch(base, token, room, agent, launch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
