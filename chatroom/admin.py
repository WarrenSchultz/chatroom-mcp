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

"""chatroom admin CLI - manage rooms and per-box tokens.

    export CHATROOM_DB=/var/lib/chatroom/chatroom.db
    python -m chatroom.admin init
    python -m chatroom.admin add-room shalefire
    python -m chatroom.admin add-token --agent box1 --room shalefire
    python -m chatroom.admin list-tokens
    python -m chatroom.admin revoke --agent box1

Tokens are shown exactly once, at creation. Only the SHA-256 hash is stored, so
a lost token is reissued rather than recovered.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db


def cmd_init(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    print(f"initialised {db.db_path()}")
    conn.close()
    return 0


def cmd_add_room(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    db.ensure_room(conn, args.name)
    print(f"room ready: {args.name}")
    conn.close()
    return 0


def cmd_list_rooms(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    rooms = db.list_rooms(conn)
    if not rooms:
        print("(no rooms)")
    for r in rooms:
        n = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE room=? AND status IN "
            "('pending','in_progress','blocked')", (r,)
        ).fetchone()["c"]
        print(f"{r}\t{n} open task(s)")
    conn.close()
    return 0


def cmd_add_token(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    db.ensure_room(conn, args.room)
    for extra in args.also_room or []:
        db.ensure_room(conn, extra)
    token = db.new_token()
    conn.execute(
        "INSERT INTO tokens(token_hash,agent_name,default_room,allowed_rooms,created_ts) "
        "VALUES (?,?,?,?,?)",
        (db.hash_token(token), args.agent, args.room,
         json.dumps(sorted(set(args.also_room or []))), db.now()),
    )
    flags = {"readonly": int(bool(args.readonly)),
             "admin": int(bool(args.admin)),
             "all_rooms": int(bool(args.all_rooms))}
    conn.execute(
        "UPDATE tokens SET readonly=?, admin=?, all_rooms=? WHERE token_hash=?",
        (flags["readonly"], flags["admin"], flags["all_rooms"], db.hash_token(token)),
    )
    conn.close()
    rooms = "ALL" if args.all_rooms else ", ".join(sorted({args.room, *(args.also_room or [])}))
    parts = []
    if args.admin:
        parts.append("admin")
    parts.append("read-only observer" if args.readonly else "read-write agent")
    if args.all_rooms:
        parts.append("all-rooms")
    kind = ", ".join(parts)
    print(f"kind         {kind}")
    print(f"agent        {args.agent}")
    print(f"default room {args.room}")
    print(f"rooms        {rooms}")
    print()
    print("Token (shown once, store it now):")
    print(f"  {token}")
    print()
    print("On that box:")
    print(f"  export CHATROOM_TOKEN={token}")
    return 0


def cmd_list_tokens(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        "SELECT agent_name, default_room, allowed_rooms, created_ts, last_seen, revoked, "
        "readonly, admin, all_rooms FROM tokens ORDER BY agent_name"
    ).fetchall()
    if not rows:
        print("(no tokens)")
    for r in rows:
        try:
            extra = json.loads(r["allowed_rooms"]) or []
        except (TypeError, ValueError):
            extra = []
        rooms = "ALL" if r["all_rooms"] else ", ".join(sorted({r["default_room"], *extra}))
        tags = []
        if r["revoked"]:
            tags.append("REVOKED")
        if r["admin"]:
            tags.append("admin")
        if r["readonly"]:
            tags.append("observer")
        if r["all_rooms"]:
            tags.append("all-rooms")
        flag = (" [" + ", ".join(tags) + "]") if tags else ""
        print(f"{r['agent_name']:<16} rooms={rooms:<28} "
              f"last_seen={r['last_seen'] or 'never'}{flag}")
    conn.close()
    return 0


def cmd_set_retention(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    db.ensure_room(conn, args.room)
    days = int(args.days) if args.days else None
    db.set_retention(conn, args.room, days)
    pruned = db.prune_room(conn, args.room, days)
    conn.close()
    print(f"room {args.room}: retention_days={days if days else 'indefinite'} "
          f"(pruned {pruned} old rows)")
    return 0


def cmd_delete_room(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    if not args.yes:
        print(f"refusing to delete room {args.room!r} without --yes")
        conn.close()
        return 1
    counts = db.delete_room(conn, args.room)
    conn.close()
    print(f"deleted room {args.room}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def cmd_room_info(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    db.ensure_room(conn, args.room)
    if args.description is not None or args.repo_url is not None or args.onboarding_notes is not None:
        db.set_room_info(conn, args.room, args.description, args.repo_url, args.onboarding_notes)
    info = db.room_info_dict(db.get_room(conn, args.room))
    conn.close()
    for k, v in (info or {}).items():
        print(f"{k:<18} {v if v is not None else ''}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    cur = conn.execute("UPDATE tokens SET revoked=1 WHERE agent_name=?", (args.agent,))
    print(f"revoked {cur.rowcount} token(s) for agent {args.agent}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chatroom.admin", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database and schema").set_defaults(fn=cmd_init)

    ar = sub.add_parser("add-room", help="create a project room")
    ar.add_argument("name")
    ar.set_defaults(fn=cmd_add_room)

    sub.add_parser("list-rooms", help="list rooms").set_defaults(fn=cmd_list_rooms)

    at = sub.add_parser("add-token", help="mint a token for one box")
    at.add_argument("--agent", required=True, help="agent name, e.g. box1 or laptop-wschultz")
    at.add_argument("--room", required=True, help="default project room")
    at.add_argument("--also-room", action="append",
                    help="additional room this box may access (repeatable)")
    at.add_argument("--readonly", action="store_true",
                    help="observer token: can read and watch but never modify the board")
    at.add_argument("--admin", action="store_true",
                    help="admin token: may set retention and delete rooms")
    at.add_argument("--all-rooms", action="store_true",
                    help="token may access every room (useful for a whole-instance observer)")
    at.set_defaults(fn=cmd_add_token)

    sub.add_parser("list-tokens", help="list agents and tokens").set_defaults(fn=cmd_list_tokens)

    rv = sub.add_parser("revoke", help="revoke all tokens for an agent")
    rv.add_argument("--agent", required=True)
    rv.set_defaults(fn=cmd_revoke)

    sr = sub.add_parser("set-retention", help="set a room's chat/event/file retention in days")
    sr.add_argument("--room", required=True)
    sr.add_argument("--days", type=int, default=0, help="days to keep (0 = indefinite)")
    sr.set_defaults(fn=cmd_set_retention)

    dr = sub.add_parser("delete-room", help="delete a room and all its data")
    dr.add_argument("--room", required=True)
    dr.add_argument("--yes", action="store_true", help="required confirmation")
    dr.set_defaults(fn=cmd_delete_room)

    ri = sub.add_parser("room-info", help="show or set a room's description/repo/onboarding notes")
    ri.add_argument("--room", required=True)
    ri.add_argument("--description", default=None)
    ri.add_argument("--repo-url", dest="repo_url", default=None)
    ri.add_argument("--onboarding-notes", dest="onboarding_notes", default=None)
    ri.set_defaults(fn=cmd_room_info)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
