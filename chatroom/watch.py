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

"""chatroom watch - tail a project room's activity in the terminal.

    python -m chatroom.watch --url https://bus.example.com --token cr_...
    python -m chatroom.watch --room shalefire --all        # replay full history
    CHATROOM_URL=... CHATROOM_TOKEN=... python -m chatroom.watch

Reads the server's SSE stream. Watching is side-effect free: it never advances
any agent's cursor, so you can leave this running without changing what the
agents themselves see as unread.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

C = {
    "dim": "\033[90m", "acc": "\033[94m", "ok": "\033[92m", "warn": "\033[93m",
    "bad": "\033[91m", "b": "\033[1m", "r": "\033[0m",
}
KIND_COLOUR = {
    "task_created": "acc", "task_claimed": "warn", "task_updated": "ok",
    "task_released": "bad", "note_added": "dim", "message": "b",
}


def paint(text: str, colour: str, enabled: bool) -> str:
    return f"{C[colour]}{text}{C['r']}" if enabled else text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chatroom.watch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("CHATROOM_URL", "http://127.0.0.1:8080"),
                   help="server base URL (env CHATROOM_URL)")
    p.add_argument("--token", default=os.environ.get("CHATROOM_TOKEN"),
                   help="bearer token, ideally an observer token (env CHATROOM_TOKEN)")
    p.add_argument("--room", default=None, help="room name (default: your token's room)")
    p.add_argument("--all", action="store_true", help="replay all history, not just recent")
    p.add_argument("--board", action="store_true", help="also print the board on every change")
    p.add_argument("--no-colour", action="store_true")
    args = p.parse_args(argv)

    if not args.token:
        p.error("no token: pass --token or set CHATROOM_TOKEN")
    colour = not args.no_colour and sys.stdout.isatty()

    url = args.url.rstrip("/") + "/v1/stream?after=" + ("all" if args.all else "recent")
    if args.room:
        url += "&room=" + urllib.parse.quote(args.room)

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {args.token}", "Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=None)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1

    print(paint(f"watching {args.url} room={args.room or '(token default)'}  ctrl-c to stop",
                "dim", colour))

    event, data = "message", []
    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "":
                if data:
                    try:
                        payload = json.loads("".join(data))
                    except ValueError:
                        payload = None
                    if payload is not None:
                        if event == "activity":
                            k = payload["kind"]
                            print(
                                paint(payload["ts"][11:19], "dim", colour) + "  "
                                + paint(f"{payload['actor']:<14}", "acc", colour) + " "
                                + paint(f"{k:<14}", KIND_COLOUR.get(k, "warn"), colour) + " "
                                + (f"#{payload['task_id']} " if payload["task_id"] else "")
                                + (payload["detail"] or ""),
                                flush=True,
                            )
                        elif event == "chat":
                            print(
                                paint(payload["ts"][11:19], "dim", colour) + "  "
                                + paint(f"{payload['author']:<14}", "acc", colour) + " "
                                + paint("chat", "b", colour)
                                + (f" ↳#{payload['reply_to']}" if payload.get("reply_to") else "")
                                + "  " + (payload["body"] or "").replace("\n", " "),
                                flush=True,
                            )
                        elif event == "board" and args.board:
                            print(paint("--- board ---", "dim", colour))
                            for t in payload:
                                print("  " + paint(f"#{t['id']:<4}", "dim", colour)
                                      + f"{t['status']:<12} {str(t['assignee'] or '-'):<14}"
                                      + t["title"])
                            print(paint("-------------", "dim", colour), flush=True)
                event, data = "message", []
    except KeyboardInterrupt:
        print(paint("\nstopped", "dim", colour))
    return 0


if __name__ == "__main__":
    sys.exit(main())
