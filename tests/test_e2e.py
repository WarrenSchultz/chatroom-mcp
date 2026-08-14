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

"""End-to-end test against a live chatroom server.

Verifies the parts that are easy to get wrong:
  * bearer token -> identity actually reaches the tool (ctx.headers)
  * room isolation is enforced server-side, not by convention
  * claim_task is atomic under concurrent contention
  * update_task detects version conflicts
  * whats_new cursors advance per agent and do not leak across rooms
  * read-only observer tokens cannot write
  * the REST + SSE surfaces work for hooks and the dashboard

Run:  python tests/test_e2e.py
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = "/tmp/chatroom_e2e.db"
PORT = 8137
BASE = f"http://127.0.0.1:{PORT}"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  <- {extra}" if extra and not cond else ""))


class Agent:
    """Talks raw JSON-RPC over HTTP, exactly like Claude Code's http transport."""

    def __init__(self, token: str):
        self.token = token
        self.c = httpx2.Client(trust_env=False, timeout=30)
        self.rid = 0

    def call(self, tool: str, args: dict | None = None) -> dict:
        self.rid += 1
        r = self.c.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": self.rid, "method": "tools/call",
                  "params": {"name": tool, "arguments": args or {}}},
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        body = r.json()
        if "error" in body:
            return {"_rpc_error": body["error"]}
        res = body["result"]
        if res.get("isError"):
            return {"_tool_error": res["content"][0]["text"]}
        return json.loads(res["content"][0]["text"])

    def tools(self) -> list[str]:
        self.rid += 1
        r = self.c.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": self.rid, "method": "tools/list"},
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        return [t["name"] for t in r.json()["result"]["tools"]]


def mint(agent: str, room: str, *, readonly: bool = False, also: list[str] | None = None,
         admin: bool = False, all_rooms: bool = False) -> str:
    cmd = [sys.executable, "-m", "chatroom.admin", "add-token", "--agent", agent, "--room", room]
    for a in also or []:
        cmd += ["--also-room", a]
    if readonly:
        cmd.append("--readonly")
    if admin:
        cmd.append("--admin")
    if all_rooms:
        cmd.append("--all-rooms")
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "CHATROOM_DB": DB}).stdout
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("cr_"):
            return s
    raise RuntimeError(f"could not mint token:\n{out}")


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(DB + suffix).unlink(missing_ok=True)

    # The suite calls provision/security helpers in-process, and those read os.environ.
    # Clear the deployment's own values so the assertions do not depend on how this
    # particular box is configured (same lesson as the pinned subprocess env below).
    for k in ("CHATROOM_PUBLIC_URL", "CHATROOM_PUBLIC_HOSTS", "CHATROOM_LAN_URL",
              "CHATROOM_ADMIN_API", "CHATROOM_CONSOLE_LAN_ONLY", "CHATROOM_ROOM",
              "CHATROOM_TOKEN", "CHATROOM_URL"):
        os.environ.pop(k, None)

    # Pin every setting the assertions depend on. The suite runs inside the same image
    # as the server, so without this it inherits the deployment's own configuration and
    # asserts against whatever the operator happens to have enabled — which is how the
    # "admin API is disabled" checks first failed, on a box where it was switched on.
    env = {
        **{k: v for k, v in os.environ.items()
           if k not in ("CHATROOM_ROOM", "CHATROOM_TOKEN", "CHATROOM_URL")},
        "CHATROOM_DB": DB,
        "PYTHONPATH": str(ROOT),
        "CHATROOM_ADMIN_API": "off",
        "CHATROOM_ENABLE_UI": "on",
        "CHATROOM_TRUST_PROXY": "off",
        "CHATROOM_AUTH_FAIL_LIMIT": "20",
        "CHATROOM_AUTH_FAIL_WINDOW": "300",
        "CHATROOM_PUBLIC_URL": "",
    }
    subprocess.run([sys.executable, "-m", "chatroom.admin", "init"],
                   cwd=ROOT, env=env, capture_output=True)

    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "chatroom.server:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(60):
            try:
                if httpx2.get(f"{BASE}/healthz", trust_env=False, timeout=2).status_code == 200:
                    break
            except Exception:
                time.sleep(0.4)
        else:
            print("server never came up:", (srv.stderr.read() or b"").decode()[-1500:])
            return 1

        t_box1 = mint("box1", "projA")
        t_box2 = mint("box2", "projA")
        t_box3 = mint("box3", "projB")
        t_obs = mint("observer", "projA", readonly=True)
        t_multi = mint("roamer", "projA", also=["projB"])

        box1, box2, box3 = Agent(t_box1), Agent(t_box2), Agent(t_box3)
        obs, roamer = Agent(t_obs), Agent(t_multi)

        print("\n--- auth and identity ---")
        check("tools are exposed", "claim_task" in box1.tools())
        who = box1.call("list_tasks")
        check("bearer token resolves to identity inside the tool",
              who.get("you_are") == "box1", str(who))
        check("token maps to its default room", who.get("room") == "projA", str(who))
        bad = Agent("cr_not_a_real_token")
        check("unknown token is rejected", "_tool_error" in bad.call("list_tasks"),
              str(bad.call("list_tasks")))
        noauth = httpx2.post(f"{BASE}/mcp",
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "list_tasks", "arguments": {}}},
                             headers={"Content-Type": "application/json",
                                      "Accept": "application/json, text/event-stream"},
                             trust_env=False, timeout=10).json()
        check("missing token is rejected",
              noauth.get("result", {}).get("isError") is True or "error" in noauth, str(noauth))

        print("\n--- rooms are enforced server-side ---")
        t1 = box1.call("create_task", {"title": "migrate schema", "body": "on projA"})
        tid = t1["created"]["id"]
        check("task created in projA", t1["created"]["room"] == "projA", str(t1))
        check("box3 (projB) cannot see projA's task",
              box3.call("list_tasks")["count"] == 0, str(box3.call("list_tasks")))
        denied = box1.call("list_tasks", {"room": "projB"})
        check("token cannot address a room it was not granted",
              "_tool_error" in denied and "does not grant" in denied["_tool_error"], str(denied))
        check("multi-room token may address its second room",
              roamer.call("list_tasks", {"room": "projB"}).get("room") == "projB")
        check("cross-room task id is invisible even when guessed",
              "_tool_error" in box3.call("get_task", {"task_id": tid}))

        print("\n--- claim_task atomicity ---")
        race = box1.call("create_task", {"title": "contended work"})["created"]["id"]
        contenders = [Agent(t_box1), Agent(t_box2), Agent(t_box1), Agent(t_box2)]
        barrier = threading.Barrier(len(contenders))

        def grab(a: Agent) -> dict:
            barrier.wait()
            return a.call("claim_task", {"task_id": race})

        with ThreadPoolExecutor(max_workers=len(contenders)) as ex:
            results = list(ex.map(grab, contenders))
        winners = [r for r in results if r.get("claimed")]
        holders = {r["task"]["assignee"] for r in results if r.get("task")}
        check("exactly one agent wins a contended claim",
              len(winners) == 1, f"{len(winners)} winners: {results}")
        check("losers are told who holds it",
              all("held by" in r.get("reason", "") or r.get("claimed") for r in results),
              str(results))
        check("all agents agree on the single holder", len(holders) == 1, str(holders))

        print("\n--- optimistic concurrency on update ---")
        cur = box1.call("get_task", {"task_id": tid})["task"]
        v = cur["version"]
        ok = box1.call("update_task", {"task_id": tid, "status": "in_progress",
                                       "expected_version": v, "note": "starting"})
        check("update with correct version succeeds", ok.get("updated") is True, str(ok))
        stale = box2.call("update_task", {"task_id": tid, "status": "done",
                                          "expected_version": v})
        check("update with stale version is refused", stale.get("updated") is False, str(stale))
        check("conflict response returns current state for reconciliation",
              stale.get("current", {}).get("version") == v + 1, str(stale))
        blind = box2.call("update_task", {"task_id": tid, "body": "no version passed"})
        check("blind write succeeds but is flagged",
              blind.get("updated") is True and "_warning" in blind, str(blind))

        print("\n--- dependencies ---")
        a = box1.call("create_task", {"title": "step A"})["created"]["id"]
        b = box1.call("create_task", {"title": "step B", "depends_on": [a]})["created"]["id"]
        bt = box1.call("get_task", {"task_id": b})["task"]
        check("unfinished dependency is reported", bt["blocked_by_unfinished"] == [a], str(bt))
        av = box1.call("get_task", {"task_id": a})["task"]["version"]
        box1.call("update_task", {"task_id": a, "status": "done", "expected_version": av})
        bt2 = box1.call("get_task", {"task_id": b})["task"]
        check("dependency clears once done", bt2["blocked_by_unfinished"] == [], str(bt2))
        bad_dep = box1.call("create_task", {"title": "x", "depends_on": [99999]})
        check("dependency on a non-existent task is rejected", "_tool_error" in bad_dep)

        print("\n--- whats_new cursors ---")
        first = box2.call("whats_new")
        check("whats_new returns backlog on first call", first["count"] > 0, str(first["count"]))
        second = box2.call("whats_new")
        check("cursor advances so events are not re-reported",
              second["count"] == 0, str(second))
        box1.call("add_note", {"task_id": tid, "body": "peer note for box2"})
        third = box2.call("whats_new")
        check("new peer activity appears after cursor advance",
              third["count"] == 1 and third["events"][0]["kind"] == "note_added", str(third))
        check("box3 in projB sees none of projA's events",
              box3.call("whats_new")["count"] == 0)
        check("events are attributed and self-flagged",
              third["events"][0]["actor"] == "box1" and third["events"][0]["by_you"] is False)

        print("\n--- read-only observer token ---")
        check("observer can read the board", obs.call("list_tasks").get("room") == "projA")
        for tool, args in (("create_task", {"title": "nope"}),
                           ("claim_task", {"task_id": tid}),
                           ("update_task", {"task_id": tid, "status": "done"}),
                           ("add_note", {"task_id": tid, "body": "nope"}),
                           ("release_task", {"task_id": tid})):
            res = obs.call(tool, args)
            check(f"observer cannot {tool}",
                  "_tool_error" in res and "read-only" in res["_tool_error"], str(res))

        print("\n--- event history (read_events) ---")
        ev_task = box1.call("create_task", {"title": "history probe"})["created"]["id"]
        box2.call("claim_task", {"task_id": ev_task})
        box2.call("update_task", {"task_id": ev_task, "status": "done"})
        hist = box1.call("read_events", {"task_id": ev_task})
        kinds = [e["kind"] for e in hist["events"]]
        check("read_events reconstructs a task's transition sequence, in order",
              kinds == ["task_created", "task_claimed", "task_updated"], str(kinds))
        check("read_events reports the cursor was untouched",
              hist.get("cursor_unchanged") is True)
        # The whole point: list_tasks shows the end state, not how it got there.
        cur_state = [t for t in box1.call("list_tasks")["tasks"] if t["id"] == ev_task][0]
        check("...which list_tasks cannot do (it has only the final status)",
              cur_state["status"] == "done" and "task_claimed" not in json.dumps(cur_state))
        check("read_events filters by kind", all(
            e["kind"] == "message"
            for e in box1.call("read_events", {"kind": "message"})["events"]))

        # Side-effect freedom is the property that makes it safe next to the hook.
        box3.call("list_tasks")  # ensure box3 has a cursor row
        pm = box1.call("post_message", {"body": "citation and cursor probe"})
        before = box2.call("whats_new")          # advances box2 to now
        for _ in range(3):
            box2.call("read_events")
        after = box2.call("whats_new")
        check("read_events does not consume the cursor (whats_new stays empty after it)",
              after["count"] == 0, f"before={before['count']} after={after['count']}")

        print("\n--- stable citation across the two id spaces ---")
        mev = [e for e in box1.call("read_events", {"kind": "message"})["events"]
               if e["message_id"] == pm["message_id"]]
        check("a chat event names the message it describes", len(mev) == 1, str(mev)[:160])
        check("...and that id is NOT the event id (separate sequences)",
              mev[0]["id"] != mev[0]["message_id"],
              f"event {mev[0]['id']} vs message {mev[0]['message_id']}")
        body = [m for m in box1.call("read_messages")["messages"]
                if m["id"] == mev[0]["message_id"]]
        check("so the cited id resolves to the actual message body",
              len(body) == 1 and body[0]["body"] == "citation and cursor probe", str(body)[:120])
        # A fresh agent in the SAME room, so its cursor still has the post ahead of it.
        # (box3 lives in projB and would legitimately see nothing.)
        fresh = Agent(mint("citeprobe", "projA"))
        check("whats_new carries message_id too, so the hook path can cite as well",
              any(e.get("message_id") == pm["message_id"]
                  for e in fresh.call("whats_new")["events"]))

        print("\n--- agent roster ---")
        roster = box1.call("list_agents")
        names = {a["agent"] for a in roster["agents"]}
        check("roster lists projA members", {"box1", "box2", "observer"} <= names, str(names))
        check("roster excludes other rooms", "box3" not in names, str(names))
        check("caller is marked", any(a["is_you"] for a in roster["agents"]))

        print("\n--- REST surface (hooks and dashboard) ---")
        h = {"Authorization": f"Bearer {t_box1}"}
        rc = httpx2.Client(trust_env=False, timeout=15)
        wn = rc.get(f"{BASE}/v1/whats_new?peek=1", headers=h).json()
        check("REST whats_new works", "events" in wn, str(wn)[:200])
        check("REST whats_new filters out your own events",
              all(e["actor"] != "box1" for e in wn["events"]), str(wn)[:200])
        wn2 = rc.get(f"{BASE}/v1/whats_new?peek=1", headers=h).json()
        check("peek=1 does not advance the cursor", wn2["count"] == wn["count"])
        check("REST tasks returns only open work",
              all(t["status"] in ("pending", "in_progress", "blocked")
                  for t in rc.get(f"{BASE}/v1/tasks", headers=h).json()["tasks"]))
        check("REST rejects a bad token",
              rc.get(f"{BASE}/v1/tasks",
                     headers={"Authorization": "Bearer cr_bogus"}).status_code == 401)
        check("REST rejects an ungranted room",
              rc.get(f"{BASE}/v1/tasks?room=projB", headers=h).status_code == 403)
        check("dashboard is served", "chatroom" in rc.get(f"{BASE}/ui").text)

        print("\n--- chat (messages alongside the board) ---")
        check("chat tools are exposed",
              {"post_message", "read_messages"} <= set(box1.tools()), str(box1.tools()))
        pm = box1.call("post_message", {"body": "PDU poller is live, retire the YAML sensors"})
        check("post_message returns an id", bool(pm.get("ok") and pm.get("message_id")), str(pm))
        rmsg = box2.call("read_messages")
        check("peer reads the full message body",
              any(m["body"].startswith("PDU poller is live") for m in rmsg["messages"]),
              str(rmsg)[:200])
        check("read_messages is room-scoped",
              box3.call("read_messages")["count"] == 0, str(box3.call("read_messages")))
        reply = box2.call("post_message", {"body": "done, retired", "reply_to": pm["message_id"]})
        last = box1.call("read_messages")["messages"][-1]
        check("reply threads onto the parent",
              bool(reply.get("ok")) and last["reply_to"] == pm["message_id"], str(reply))
        check("cross-room reply_to is rejected",
              "_tool_error" in box3.call("post_message", {"body": "x", "reply_to": pm["message_id"]}))
        check("observer cannot post_message",
              "read-only" in obs.call("post_message", {"body": "nope"}).get("_tool_error", ""),
              str(obs.call("post_message", {"body": "nope"})))
        check("empty message is rejected",
              "_tool_error" in box1.call("post_message", {"body": "   "}))
        peek = rc.get(f"{BASE}/v1/whats_new?peek=1",
                      headers={"Authorization": f"Bearer {t_box2}"}).json()
        check("chat surfaces in whats_new as a message event",
              any(e["kind"] == "message" for e in peek["events"]), str(peek)[:200])
        restm = rc.get(f"{BASE}/v1/messages", headers=h).json()
        check("REST messages endpoint returns the thread",
              len(restm["messages"]) >= 2 and restm["latest_id"] >= pm["message_id"], str(restm)[:200])

        print("\n--- files ---")
        payload = base64.b64encode(b"print('hi')\n").decode()
        pf = box1.call("put_file", {"name": "a.py", "content_base64": payload, "mime": "text/x-python"})
        check("put_file returns id + sha", bool(pf.get("ok") and pf.get("file_id")), str(pf))
        fid = pf["file_id"]
        gf = box2.call("get_file", {"file_id": fid})
        check("peer fetches exact file bytes",
              base64.b64decode(gf["content_base64"]) == b"print('hi')\n", str(gf)[:120])
        check("list_files shows the file",
              any(x["id"] == fid for x in box2.call("list_files")["files"]))
        check("files are room-scoped", box3.call("list_files")["count"] == 0)
        big = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()
        cap = box1.call("put_file", {"name": "big.bin", "content_base64": big})
        check("oversize file is rejected",
              "_tool_error" in cap and "cap" in cap["_tool_error"], str(cap)[:120])
        check("observer cannot put_file",
              "read-only" in obs.call("put_file", {"name": "x", "content_base64": payload}).get("_tool_error", ""))
        dl = rc.get(f"{BASE}/v1/files/{fid}", headers=h)
        check("REST file download returns raw bytes",
              dl.status_code == 200 and dl.content == b"print('hi')\n", str(dl.status_code))
        check("REST download rejects an ungranted room",
              rc.get(f"{BASE}/v1/files/{fid}",
                     headers={"Authorization": f"Bearer {t_box3}"}).status_code == 403)

        # --- REST upload: the half that lets bytes skip a model's context -------
        # A payload deliberately larger than a model could comfortably re-emit as base64,
        # which is the entire reason this route exists.
        big = ("x" * 200_000).encode()
        up = rc.post(f"{BASE}/v1/files", content=big,
                     headers={**h, "X-Chatroom-Filename": "big.bin",
                              "Content-Type": "application/octet-stream"})
        check("REST upload accepts a raw body", up.status_code == 201, str(up.status_code))
        uj = up.json() if up.status_code == 201 else {}
        check("...reporting size and sha256", uj.get("size") == len(big) and len(uj.get("sha256", "")) == 64,
              str(uj)[:120])
        # Round-trip byte-for-byte: an upload path that corrupts is worse than none.
        back = rc.get(f"{BASE}/v1/files/{uj.get('file_id')}", headers=h)
        check("...and the bytes come back identical", back.content == big,
              f"{len(back.content)} vs {len(big)}")
        check("...visible to peers as a file_added event",
              any(e.get("kind") == "file_added" and "big.bin" in (e.get("detail") or "")
                  for e in rc.get(f"{BASE}/v1/whats_new?peek=1",
                                  headers={"Authorization": f"Bearer {t_box2}"}).json()["events"]))
        check("upload takes the name from ?name= too",
              rc.post(f"{BASE}/v1/files?name=q.txt", content=b"q", headers=h).status_code == 201)
        check("upload without a name is refused",
              rc.post(f"{BASE}/v1/files", content=b"q", headers=h).status_code == 400)
        check("upload refuses a path rather than silently basenaming it",
              rc.post(f"{BASE}/v1/files?name=../escape", content=b"q", headers=h).status_code == 400)
        check("upload refuses an empty body",
              rc.post(f"{BASE}/v1/files?name=e.txt", content=b"", headers=h).status_code == 400)
        check("upload refuses a read-only token",
              rc.post(f"{BASE}/v1/files?name=ro.txt", content=b"q",
                      headers={"Authorization": f"Bearer {t_obs}"}).status_code == 403)
        check("upload refuses an ungranted room",
              rc.post(f"{BASE}/v1/files?name=x.txt&room=projA", content=b"q",
                      headers={"Authorization": f"Bearer {t_box3}"}).status_code == 403)
        check("upload requires a token at all",
              rc.post(f"{BASE}/v1/files?name=x.txt", content=b"q").status_code != 201)
        check("upload honours expires_in_hours",
              (rc.post(f"{BASE}/v1/files?name=tmp.txt&expires_in_hours=1", content=b"q",
                       headers=h).json() or {}).get("expires_at") is not None)
        check("upload rejects a non-numeric expiry",
              rc.post(f"{BASE}/v1/files?name=t2.txt&expires_in_hours=soon", content=b"q",
                      headers=h).status_code == 400)
        # Content-Type carries "; charset=", which must not end up in the stored mime.
        ct = rc.post(f"{BASE}/v1/files?name=n.md", content=b"# hi",
                     headers={**h, "Content-Type": "text/markdown; charset=utf-8"})
        check("upload stores the media type without charset parameters",
              rc.get(f"{BASE}/v1/files/{ct.json()['file_id']}",
                     headers=h).headers.get("content-type", "").startswith("text/markdown"),
              str(ct.json()))

        print("\n--- file expiry & deletion ---")
        blob = base64.b64encode(b"scratch").decode()
        perm = box1.call("put_file", {"name": "keep.txt", "content_base64": blob})
        check("a file with no expiry reports expires_at=None", perm["expires_at"] is None,
              str(perm)[:120])
        # 1.8s, so the assertion below is not racing a 1s boundary.
        tmp = box1.call("put_file", {"name": "scratch.txt", "content_base64": blob,
                                     "expires_in_hours": 0.0005})
        check("an expiring file reports its absolute expiry",
              bool(tmp["expires_at"]), str(tmp)[:120])
        names = [f["name"] for f in box1.call("list_files")["files"]]
        check("it is listed while still valid", "scratch.txt" in names, str(names))
        time.sleep(2.5)
        names = [f["name"] for f in box1.call("list_files")["files"]]
        # The sweep runs hourly; reads must not serve an expired file in the meantime.
        check("an expired file disappears from list_files before any sweep runs",
              "scratch.txt" not in names, str(names))
        check("...and cannot be fetched either",
              "_tool_error" in box1.call("get_file", {"file_id": tmp["file_id"]}))
        check("a non-expiring file is unaffected", "keep.txt" in names, str(names))

        check("a peer cannot delete a file it did not upload",
              "_tool_error" in box2.call("delete_file", {"file_id": perm["file_id"]}))
        check("a read-only observer cannot delete",
              "_tool_error" in obs.call("delete_file", {"file_id": perm["file_id"]}))
        adm = Agent(mint("fileadmin", "projA", admin=True))
        gone = adm.call("delete_file", {"file_id": perm["file_id"]})
        check("an admin can delete another agent's file",
              gone.get("ok") and gone["deleted"]["name"] == "keep.txt", str(gone)[:140])
        check("the file is really gone",
              "_tool_error" in box1.call("get_file", {"file_id": perm["file_id"]}))
        own = box1.call("put_file", {"name": "mine.txt", "content_base64": blob})
        check("an author can delete their own file",
              box1.call("delete_file", {"file_id": own["file_id"]}).get("ok") is True)
        check("deletion is recorded as an event, so the audit trail keeps the fact",
              any(e["kind"] == "file_deleted"
                  for e in box1.call("read_events", {"kind": "file_deleted"})["events"]))
        check("deleting a missing file is an error, not a silent success",
              "_tool_error" in box1.call("delete_file", {"file_id": 999999}))
        rest_del = rc.delete(f"{BASE}/v1/files/1",
                             headers={"Authorization": f"Bearer {t_obs}"})
        check("REST delete refuses a read-only token", rest_del.status_code == 403,
              str(rest_del.status_code))

        print("\n--- room info & onboarding ---")
        si = box1.call("set_room_info", {"description": "proj A ops", "repo_url": "https://x/a",
                                         "onboarding_notes": "read the README first"})
        check("set_room_info works",
              si.get("ok") and si["room_info"]["description"] == "proj A ops", str(si)[:150])
        gi = box2.call("get_room_info")
        check("get_room_info returns fields", gi.get("onboarding_notes") == "read the README first")
        check("observer cannot set_room_info",
              "read-only" in obs.call("set_room_info", {"description": "no"}).get("_tool_error", ""))
        t_newbie = mint("newbie", "projA")
        nb = Agent(t_newbie).call("whats_new")
        check("onboarding shown on a newcomer's first whats_new",
              nb.get("room_onboarding", {}).get("onboarding_notes") == "read the README first",
              str(nb.get("room_onboarding")))
        nb2 = Agent(t_newbie).call("whats_new")
        check("onboarding not repeated after the first look", "room_onboarding" not in nb2)

        print("\n--- admin: retention & delete ---")
        t_admin = mint("boss", "projA", admin=True)
        boss = Agent(t_admin)
        check("non-admin cannot set_retention",
              "not an admin" in box1.call("set_retention", {"days": 7}).get("_tool_error", ""))
        sr = boss.call("set_retention", {"days": 7})
        check("admin sets retention", sr.get("ok") and sr["retention_days"] == 7, str(sr))
        check("non-admin cannot delete_room",
              "not an admin" in box1.call("delete_room", {"room": "projA"}).get("_tool_error", ""))
        check("REST retention rejects a non-admin token",
              rc.post(f"{BASE}/v1/rooms/projA/retention", headers=h, json={"days": 5}).status_code == 403)
        check("REST retention works for an admin token",
              rc.post(f"{BASE}/v1/rooms/projA/retention",
                      headers={"Authorization": f"Bearer {t_admin}"}, json={"days": 5}).status_code == 200)
        t_boss2 = mint("boss2", "junkroom", admin=True)
        Agent(t_boss2).call("create_task", {"title": "temp"})
        dl2 = Agent(t_boss2).call("delete_room", {"room": "junkroom"})
        check("admin delete_room tool cascades", dl2.get("ok") and dl2["deleted_room"] == "junkroom", str(dl2))
        check("deleted room is gone",
              rc.get(f"{BASE}/v1/rooms", headers={"Authorization": f"Bearer {t_boss2}"}).json()
              .get("rooms") == [] or all(r["name"] != "junkroom" for r in
              rc.get(f"{BASE}/v1/rooms", headers={"Authorization": f"Bearer {t_admin}"}).json()["rooms"]))

        print("\n--- all-rooms observer & /v1/rooms ---")
        t_all = mint("watcher", "projA", readonly=True, all_rooms=True)
        allr = rc.get(f"{BASE}/v1/rooms", headers={"Authorization": f"Bearer {t_all}"}).json()
        check("all-rooms token sees multiple rooms",
              {"projA", "projB"} <= {r["name"] for r in allr["rooms"]}, str([r["name"] for r in allr["rooms"]]))
        check("all-rooms flag is reported", allr["all_rooms"] is True)
        scoped = rc.get(f"{BASE}/v1/rooms", headers=h).json()
        check("scoped token sees only its granted rooms",
              {r["name"] for r in scoped["rooms"]} == {"projA"}, str(scoped)[:150])

        print("\n--- SSE stream (live watching) ---")
        seen: list[dict] = []

        def reader() -> None:
            with rc.stream("GET", f"{BASE}/v1/stream?after=all",
                           headers={"Authorization": f"Bearer {t_obs}"}) as r:
                ev = None
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:") and ev == "activity":
                        seen.append(json.loads(line[5:].strip()))
                        if len(seen) > 200:
                            return
                    if len(seen) > 3 and any(e["detail"] == "sse probe" for e in seen):
                        return

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        time.sleep(2.0)
        box1.call("create_task", {"title": "sse probe"})
        th.join(timeout=12)
        check("SSE stream replays history", len(seen) > 3, f"got {len(seen)}")
        check("SSE stream delivers a live event",
              any(e["detail"] == "sse probe" for e in seen),
              f"{[e['detail'] for e in seen][-5:]}")
        check("observer watching did not consume box2's unread",
              box2.call("whats_new")["count"] > 0)

        print("\n--- UserPromptSubmit hook ---")
        # Earlier assertions drained box2's cursor, so generate fresh peer
        # activity for the hook to find.
        box1.call("add_note", {"task_id": tid, "body": "activity for the hook to pick up"})
        hook = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "CHATROOM_URL": BASE, "CHATROOM_TOKEN": t_box2})
        check("hook exits 0", hook.returncode == 0, hook.stderr[-300:])
        check("hook emits activity block", "<chatroom_activity>" in hook.stdout, hook.stdout[:200])
        check("hook labels peer content untrusted", "UNTRUSTED DATA" in hook.stdout)
        hook2 = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "CHATROOM_URL": BASE, "CHATROOM_TOKEN": t_box2})
        check("hook is silent when nothing is new", hook2.stdout.strip() == "", hook2.stdout[:200])
        dead = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "CHATROOM_URL": "http://127.0.0.1:9", "CHATROOM_TOKEN": t_box2})
        check("hook fails open when the bus is unreachable",
              dead.returncode == 0 and dead.stdout.strip() == "", dead.stdout[:200])

        # The hook and the whats_new() tool share one cursor and the hook consumes it, so a
        # hook-running agent's own whats_new() reports 0. Intended, but it reads as "the room
        # is quiet", which misled a live agent — so pin the behaviour and keep the docs honest.
        t_cursor = mint("cursorshare", "projA")
        box1.call("post_message", {"body": "activity for the cursor-sharing check"})
        injected = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "CHATROOM_URL": BASE, "CHATROOM_TOKEN": t_cursor})
        check("hook delivers peer activity to a fresh agent",
              "<chatroom_activity>" in injected.stdout, injected.stdout[:150])
        after = Agent(t_cursor).call("whats_new")
        check("hook consumed the cursor, so the agent's own whats_new() sees 0",
              after.get("count") == 0, str(after)[:200])
        check("...and nothing is left stranded behind that cursor",
              after.get("remaining") == 0, str(after)[:200])
        check("read_messages stays side-effect free and still shows the activity",
              any("cursor-sharing check" in m["body"]
                  for m in Agent(t_cursor).call("read_messages")["messages"]))

        # Fail-open makes "misconfigured" and "nothing new" look identical, which cost real
        # debugging time. CHATROOM_HOOK_DEBUG must tell them apart on stderr, never stdout —
        # stdout is prepended to the agent's prompt, so a diagnostic there becomes context.
        def run_hook(env_extra):
            # Drop any inherited CHATROOM_ROOM first, then apply the case's overrides —
            # the operator's own shell may well have it set, which would silently change
            # which room each case exercises.
            e = {k: v for k, v in os.environ.items() if k != "CHATROOM_ROOM"}
            e["CHATROOM_URL"] = BASE
            e.update(env_extra)
            return subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
                capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL)

        t_dbg = mint("hookdebug", "projA")
        box1.call("post_message", {"body": "activity for the debug-flag check"})
        d1 = run_hook({"CHATROOM_TOKEN": t_dbg, "CHATROOM_HOOK_DEBUG": "1"})
        check("debug reports HTTP 200 and the event count on stderr",
              "HTTP 200" in d1.stderr and "events=" in d1.stderr, d1.stderr[:200])
        check("debug does not contaminate stdout (stdout is prompt context)",
              "[chatroom-hook]" not in d1.stdout, d1.stdout[:200])
        d2 = run_hook({"CHATROOM_TOKEN": t_dbg, "CHATROOM_HOOK_DEBUG": "1"})
        check("debug distinguishes a healthy quiet room from a failure",
              "nothing unread" in d2.stderr, d2.stderr[:200])
        d3 = run_hook({"CHATROOM_TOKEN": t_dbg, "CHATROOM_HOOK_DEBUG": "1",
                       "CHATROOM_ROOM": "projB"})
        check("debug explains an ungranted CHATROOM_ROOM instead of failing silently",
              "403" in d3.stderr and "does not grant" in d3.stderr, d3.stderr[:250])
        d4 = run_hook({"CHATROOM_TOKEN": "cr_bogus_debug", "CHATROOM_HOOK_DEBUG": "1"})
        check("debug reports 401 for a bad token", "401" in d4.stderr, d4.stderr[:200])
        d5 = run_hook({"CHATROOM_TOKEN": t_dbg, "CHATROOM_HOOK_DEBUG": "1",
                       "CHATROOM_URL": "http://127.0.0.1:9"})
        check("debug reports an unreachable bus", "cannot reach" in d5.stderr, d5.stderr[:200])
        quiet = run_hook({"CHATROOM_TOKEN": "cr_bogus_debug"})
        check("with debug off, failures stay silent on both streams",
              quiet.stderr.strip() == "" and quiet.stdout.strip() == "",
              f"out={quiet.stdout[:80]} err={quiet.stderr[:80]}")
        check("every debug path still exits 0 (fail-open preserved)",
              all(r.returncode == 0 for r in (d1, d2, d3, d4, d5, quiet)),
              str([r.returncode for r in (d1, d2, d3, d4, d5, quiet)]))
        # The explicit User-Agent is what keeps a WAF/edge from silently 403ing the hook.
        hook_src = (ROOT / "hooks" / "chatroom_whats_new.py").read_text()
        check("hook sends an explicit User-Agent, not urllib's default",
              '"User-Agent"' in hook_src and "chatroom-hook/" in hook_src)
        check("terminal watcher sends one too",
              '"User-Agent"' in (ROOT / "chatroom" / "watch.py").read_text())

        print("\n--- revocation ---")
        subprocess.run([sys.executable, "-m", "chatroom.admin", "revoke", "--agent", "box2"],
                       cwd=ROOT, env=env, capture_output=True)
        check("revoked token stops working",
              "_tool_error" in Agent(t_box2).call("list_tasks"))
        check("other agents are unaffected", box1.call("list_tasks").get("room") == "projA")

        # --- admin console API -------------------------------------------------
        print("\n--- admin console (/admin + /v1/admin/*) ---")
        from chatroom import provision, security

        # Pure-function checks first: URL derivation and snippet shape are where the
        # awkwardness lives, and they need no server.
        check("lan_url comes from the admin's own (LAN) request",
              provision.lan_url({"host": "10.0.0.5:8090"}) == "http://10.0.0.5:8090")
        check("public_url is configuration, never inferred from a request",
              provision.public_url() is None)
        os.environ["CHATROOM_PUBLIC_URL"] = "https://bus.example.com/"
        try:
            check("public_url reads CHATROOM_PUBLIC_URL and strips the trailing slash",
                  provision.public_url() == "https://bus.example.com")
            both = provision.both_setups("http://10.0.0.5:8090", provision.public_url(),
                                         "box9", "cr_TESTTOKEN", "projA")
            check("both_setups emits a LAN and a public variant",
                  both["lan"]["url"] == "http://10.0.0.5:8090"
                  and both["public"]["url"] == "https://bus.example.com", str(both)[:120])
            check("LAN is the recommended route", both["prefer"] == "lan")
            check("the two variants differ only in URL",
                  both["lan"]["claude_cli"].replace("http://10.0.0.5:8090", "X")
                  == both["public"]["claude_cli"].replace("https://bus.example.com", "X"))
        finally:
            os.environ.pop("CHATROOM_PUBLIC_URL", None)
        nopub = provision.both_setups("http://10.0.0.5:8090", None, "box9", "cr_T", "projA")
        check("with no public URL configured, the public variant is absent and explained",
              nopub["public"] is None and "CHATROOM_PUBLIC_URL" in nopub["public_hint"])
        check("server_name is per-room so cross-posting stays impossible",
              provision.server_name("proj-a") == "chatroom-proj-a")

        # Consoles must refuse public-side requests on their own, not only via an edge rule.
        check("an edge header marks a request as public-side",
              security.arrived_from_public({"CF-Ray": "abc123"}))
        check("a plain LAN request is not public-side",
              not security.arrived_from_public({"Host": "10.0.0.5:8090"}))
        os.environ["CHATROOM_PUBLIC_HOSTS"] = "bus.example.com"
        try:
            check("a Host matching a configured public hostname is public-side",
                  security.arrived_from_public({"Host": "bus.example.com"}))
        finally:
            os.environ.pop("CHATROOM_PUBLIC_HOSTS", None)
        check("console_lan_only defaults on", security.console_lan_only())

        # A page can only hide things if the hidden attribute actually hides them. The UA
        # rule [hidden]{display:none} loses to any author `display:` on the same element,
        # so `main{display:grid}` and `#veil{display:flex}` silently defeated it and the
        # mint modal rendered over the console from page load, close button inert.
        for page in ("admin.html", "dashboard.html"):
            src = (ROOT / "chatroom" / page).read_text()
            uses_hidden = bool(re.search(r"<[a-zA-Z][^>]*\shidden(\s|>)", src))
            # Strip CSS/HTML comments first: prose *describing* the rule is not the rule,
            # and matching it made an earlier version of this check pass with the fix removed.
            code = re.sub(r"/\*.*?\*/|<!--.*?-->", "", src, flags=re.S)
            guarded = bool(re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none", code))
            check(f"{page}: hidden attribute is CSS-guarded where it is used",
                  guarded or not uses_hidden,
                  f"uses hidden={uses_hidden} guard={guarded}")
            # navigator.clipboard is undefined outside a secure context, and these
            # consoles are LAN-only so plain HTTP on a LAN address is the normal way to
            # reach them. A copy button that only calls it silently does nothing there.
            # A debug view spanning days needs the date, not just a wall-clock time.
            if page == "dashboard.html":
                check("dashboard timestamps include the date, not just HH:MM:SS",
                      "getFullYear()" in code and "toTimeString" not in code,
                      "still time-only")
                check("both live panels render newest-first",
                      "#log,#chat{display:flex;flex-direction:column-reverse}" in code,
                      "chat or log still oldest-first")
                check("chat no longer force-scrolls away from the newest message",
                      "scrollTop=$(\"#chat\").scrollHeight" not in code)
            if "navigator.clipboard" in code:
                check(f"{page}: clipboard use has a non-secure-context fallback",
                      "execCommand" in code and "isSecureContext" in code,
                      "relies on navigator.clipboard alone")
        setup = provision.client_setup("https://bus.example.com", "box9", "cr_TESTTOKEN", "projA",
                                       extra_rooms=["projB"])
        check("generated CLI carries url, name and token",
              all(s in setup["claude_cli"]
                  for s in ("https://bus.example.com/mcp", "chatroom-projA", "cr_TESTTOKEN")),
              setup["claude_cli"])
        check("generated .mcp.json parses and points at the right server",
              json.loads(setup["mcp_json"])["mcpServers"]["chatroom-projA"]["url"]
              == "https://bus.example.com/mcp")
        check("generated hook env sets all three variables",
              all(k in setup["hook_env"]
                  for k in ("CHATROOM_URL=", "CHATROOM_TOKEN=", "CHATROOM_ROOM=")))
        check("agent brief states the untrusted-data rule",
              "untrusted DATA" in setup["agent_brief"], setup["agent_brief"][-120:])
        check("agent brief warns about the hook consuming whats_new",
              "already delivered" in setup["agent_brief"])
        check("hook install fetches from the server, not a local checkout",
              "/v1/hook" in setup["hook_install"]
              and "cp hooks/" not in setup["hook_install"], setup["hook_install"][:160])
        check("hook install carries the token on the fetch (survives an edge auth rule)",
              "Authorization: Bearer" in setup["hook_install"])
        # A failed download must not leave settings.json pointing at a hook that is not
        # there — that turns one missing file into a hook error on every single prompt.
        check("hook install aborts the settings merge if the download failed",
              "missing or empty" in setup["hook_install"]
              and "DOWNLOAD FAILED" in setup["hook_install"], setup["hook_install"][:200])
        check("hook install validates the download parses as python before installing",
              "ast.parse" in setup["hook_install"])
        check("hook install stages via a temp file, not straight over the target",
              "mktemp" in setup["hook_install"] and "mv " in setup["hook_install"])
        check("hook install avoids set -e (it is pasted into an interactive shell)",
              "set -e" not in setup["hook_install"])
        check("hook verify passes env explicitly, not relying on the shell",
              "CHATROOM_HOOK_DEBUG=1" in setup["hook_install"]
              and setup["hook_install"].count("CHATROOM_TOKEN=") >= 1
              and "CHATROOM_URL=" in setup["hook_install"])
        check("host-CLI equivalent is surfaced for auditability",
              "add-token" in setup["admin_cli_equivalent"]
              and "--also-room" in setup["admin_cli_equivalent"])

        # The admin API is opt-in, so this server (started without the flag) must refuse.
        ah = {"Authorization": f"Bearer {t_admin}"}
        check("admin API is 404 when CHATROOM_ADMIN_API is unset",
              rc.get(f"{BASE}/v1/admin/state", headers=ah).status_code == 404)
        check("/admin console is 404 when disabled",
              rc.get(f"{BASE}/admin").status_code == 404)

        # Second instance with the flag on, same DB, to exercise the enabled path.
        aport = PORT + 1
        abase = f"http://127.0.0.1:{aport}"
        asrv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "chatroom.server:app",
             "--host", "127.0.0.1", "--port", str(aport), "--log-level", "warning"],
            cwd=ROOT, env={**env, "CHATROOM_ADMIN_API": "on"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(60):
                try:
                    if httpx2.get(f"{abase}/healthz", trust_env=False, timeout=2).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.4)
            check("/admin console is served when enabled",
                  "chatroom admin" in rc.get(f"{abase}/admin").text)
            # A whole-server admin means --admin AND --all-rooms; neither alone is enough.
            t_alladmin = mint("srvadmin", "projA", admin=True, all_rooms=True)
            for label, tk, want in (("read-write token", t_box1, 403),
                                    ("read-only all-rooms observer", t_all, 403),
                                    ("room-scoped admin (no --all-rooms)", t_admin, 403),
                                    ("whole-server admin", t_alladmin, 200)):
                got_code = rc.get(f"{abase}/v1/admin/state",
                                  headers={"Authorization": f"Bearer {tk}"}).status_code
                check(f"admin state: {label} -> {want}", got_code == want, str(got_code))

            sah = {"Authorization": f"Bearer {t_alladmin}"}
            st = rc.get(f"{abase}/v1/admin/state", headers=sah).json()
            check("state lists rooms, token metadata, posture and both client URLs",
                  {"rooms", "tokens", "server", "lan_url", "public_url"} <= set(st), str(sorted(st)))
            check("state never leaks token values, only metadata",
                  not any("cr_" in json.dumps(t) for t in st["tokens"]), str(st["tokens"])[:150])

            made = rc.post(f"{abase}/v1/admin/rooms", headers=sah,
                           json={"name": "adminmade", "description": "made by the admin API",
                                 "onboarding_notes": "orientation text"}).json()
            check("admin can create a room with its info in one call",
                  made["created"] and made["room"]["description"] == "made by the admin API",
                  str(made)[:150])
            check("room names with whitespace are rejected",
                  rc.post(f"{abase}/v1/admin/rooms", headers=sah,
                          json={"name": "bad name"}).status_code == 400)

            got_tok = rc.post(f"{abase}/v1/admin/tokens", headers=sah,
                              json={"agent": "mintedbox", "room": "adminmade",
                                    "lan_url": "https://bus.example.com"}).json()
            check("mint returns a usable token plus setup snippets",
                  got_tok["token"].startswith("cr_")
                  and "claude_cli" in got_tok["setup"]["lan"], str(got_tok)[:150])
            check("mint honours an explicit lan_url",
                  "https://bus.example.com/mcp" in got_tok["setup"]["lan"]["claude_cli"],
                  got_tok["setup"]["lan"]["claude_cli"])
            minted = Agent(got_tok["token"])
            who = minted.call("list_tasks")
            check("a token minted over HTTP actually authenticates",
                  who.get("you_are") == "mintedbox" and who.get("room") == "adminmade", str(who))
            check("and is still room-scoped",
                  "_tool_error" in minted.call("list_tasks", {"room": "projA"}))
            check("mint rejects a missing agent or room",
                  rc.post(f"{abase}/v1/admin/tokens", headers=sah,
                          json={"room": "adminmade"}).status_code == 400)

            check("revoking your own agent needs confirm_self",
                  rc.post(f"{abase}/v1/admin/tokens/revoke", headers=sah,
                          json={"agent": "srvadmin"}).status_code == 409)
            rev = rc.post(f"{abase}/v1/admin/tokens/revoke", headers=sah,
                          json={"agent": "mintedbox"}).json()
            check("revoke reports how many tokens it killed", rev["revoked"] == 1, str(rev))
            check("the revoked token stops working",
                  "_tool_error" in Agent(got_tok["token"]).call("list_tasks"))
            hooksrc = rc.get(f"{abase}/v1/hook",
                             headers={"Authorization": f"Bearer {t_box1}"})
            check("/v1/hook serves the hook source to any valid token",
                  hooksrc.status_code == 200
                  and "chatroom_activity" in hooksrc.text, str(hooksrc.status_code))
            check("/v1/hook matches the bundled hook byte for byte",
                  hooksrc.text == (ROOT / "hooks" / "chatroom_whats_new.py").read_text())
            # A server whose container was built but never recreated serves a stale hook
            # while every other health check looks fine. Publishing the digest makes that
            # detectable with one HEAD request instead of by reading the file.
            import hashlib as _h
            check("/v1/hook advertises its digest so staleness is detectable",
                  hooksrc.headers.get("X-Chatroom-Hook-SHA256")
                  == _h.sha256(hooksrc.content).hexdigest(),
                  str(hooksrc.headers.get("X-Chatroom-Hook-SHA256")))
            check("admin state reports the same digest",
                  rc.get(f"{abase}/v1/admin/state", headers=sah).json()["server"]["hook_sha256"]
                  == _h.sha256(hooksrc.content).hexdigest())
            check("/v1/hook still requires a credential",
                  rc.get(f"{abase}/v1/hook").status_code == 401)
            check("/v1/hook is NOT console-gated — remote boxes must be able to fetch it",
                  rc.get(f"{abase}/v1/hook",
                         headers={"Authorization": f"Bearer {t_box1}",
                                  "CF-Ray": "0000000000000000-TEST"}).status_code == 200)
            # Purge is destructive in a way revoke is not, so the invariant that matters
            # is that a LIVE token can never be removed by it, whatever the age filter says.
            t_doomed = mint("doomed", "projA")
            t_keeper = mint("keeper", "projA")
            rc.post(f"{abase}/v1/admin/tokens/revoke", headers=sah, json={"agent": "doomed"})
            # Snapshot the live set *after* the revoke, so the comparison is "purge changed
            # nothing that was live", not "purge changed nothing at all".
            live_before = {x["agent"] for x in rc.get(f"{abase}/v1/admin/state",
                                                      headers=sah).json()["tokens"]
                           if not x["revoked"]}
            far = rc.post(f"{abase}/v1/admin/tokens/purge", headers=sah,
                          json={"older_than_days": 3650}).json()
            check("purge with a long age filter spares a just-revoked row",
                  far["removed"] == 0 and far["revoked_remaining"] >= 1, str(far))
            allp = rc.post(f"{abase}/v1/admin/tokens/purge", headers=sah,
                           json={"older_than_days": 0}).json()
            check("purge with no age filter removes revoked rows",
                  allp["removed"] >= 1 and allp["revoked_remaining"] == 0, str(allp))
            after = rc.get(f"{abase}/v1/admin/state", headers=sah).json()["tokens"]
            live_after = {x["agent"] for x in after if not x["revoked"]}
            check("purge left every LIVE token intact",
                  live_after == live_before,
                  f"lost {live_before - live_after}, gained {live_after - live_before}")
            check("...including the one whose peer was purged",
                  Agent(t_keeper).call("list_tasks").get("you_are") == "keeper")
            check("the purged agent is gone from the listing entirely",
                  not any(x["agent"] == "doomed" for x in after))
            check("purge rejects a non-numeric age",
                  rc.post(f"{abase}/v1/admin/tokens/purge", headers=sah,
                          json={"older_than_days": "soon"}).status_code == 400)
            check("purge rejects a negative age",
                  rc.post(f"{abase}/v1/admin/tokens/purge", headers=sah,
                          json={"older_than_days": -5}).status_code == 400)
            check("purge needs a whole-server admin like every other admin route",
                  rc.post(f"{abase}/v1/admin/tokens/purge",
                          headers={"Authorization": f"Bearer {t_box1}"},
                          json={}).status_code == 403)
            check("admin can delete a room it created",
                  rc.delete(f"{abase}/v1/rooms/adminmade", headers=sah).json()["ok"])

            # Same credential, same endpoint, but presented as if it arrived from the
            # public side: the console surfaces must vanish rather than authenticate.
            pub_hdr = {**sah, "CF-Ray": "0000000000000000-TEST"}
            for path in ("/v1/admin/state", "/admin", "/ui"):
                r = rc.get(f"{abase}{path}", headers=pub_hdr)
                check(f"{path} is 404 when the request looks public-side",
                      r.status_code == 404, f"{path} -> {r.status_code}")
            check("but the MCP endpoint still serves public-side traffic",
                  rc.post(f"{abase}/mcp",
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          headers={"Authorization": f"Bearer {t_box1}",
                                   "Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream",
                                   "CF-Ray": "0000000000000000-TEST"}).status_code == 200)
            check("and so does the REST surface agents and hooks use",
                  rc.get(f"{abase}/v1/tasks",
                         headers={"Authorization": f"Bearer {t_box1}",
                                  "CF-Ray": "0000000000000000-TEST"}).status_code == 200)
            mint_pub = rc.post(f"{abase}/v1/admin/tokens", headers=sah,
                               json={"agent": "dualbox", "room": "projA",
                                     "lan_url": "http://10.0.0.5:8090",
                                     "public_url": "https://bus.example.com"}).json()
            check("mint returns setup for both routes",
                  mint_pub["setup"]["lan"]["url"] == "http://10.0.0.5:8090"
                  and mint_pub["setup"]["public"]["url"] == "https://bus.example.com",
                  str(mint_pub.get("setup"))[:140])
            rc.post(f"{abase}/v1/admin/tokens/revoke", headers=sah, json={"agent": "dualbox"})
        finally:
            asrv.terminate()
            try:
                asrv.wait(timeout=10)
            except subprocess.TimeoutExpired:
                asrv.kill()

        # --- exposure hardening ------------------------------------------------
        # Runs last: it deliberately burns the failed-auth budget for 127.0.0.1,
        # which would otherwise colour later assertions.
        print("\n--- hook: in-loop delivery (PostToolUse) and throttling ---")

        def hook_ev(event, token, extra=None, stdin_json=True):
            """Invoke the hook the way Claude Code does: event name as JSON on stdin."""
            e = {k: v for k, v in os.environ.items() if k != "CHATROOM_ROOM"}
            e.update({"CHATROOM_URL": BASE, "CHATROOM_TOKEN": token,
                      "CHATROOM_HOOK_DEBUG": "1"})
            e.update(extra or {})
            return subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
                input=json.dumps({"hook_event_name": event}) if stdin_json else "",
                capture_output=True, text=True, env=e)

        t_loop = mint("loopbox", "projA")
        box1.call("post_message", {"body": "in-loop delivery probe"})

        r1 = hook_ev("PostToolUse", t_loop)
        check("PostToolUse emits the JSON envelope, not bare text",
              r1.stdout.lstrip().startswith("{"), r1.stdout[:120])
        env1 = json.loads(r1.stdout)["hookSpecificOutput"]
        check("...naming the event it was fired for",
              env1["hookEventName"] == "PostToolUse", str(env1)[:120])
        check("...and carrying the activity plus the untrusted-data frame",
              "in-loop delivery probe" in env1["additionalContext"]
              and "UNTRUSTED DATA" in env1["additionalContext"],
              env1["additionalContext"][:150])
        # With a backlog larger than the cap, the newest must survive: the cursor advances
        # past everything returned, so whatever the hook drops is gone from this path.
        check("a truncated backlog keeps the NEWEST events and says what it dropped",
              ("older event(s) omitted" in env1["additionalContext"]) ==
              (int(env1["additionalContext"].split("update(s)")[0].split()[-1]) > 25),
              env1["additionalContext"][:200])

        # The whole point of the throttle: a tool-heavy turn must not become a request
        # storm, and must cost the model nothing when there is no news.
        r2 = hook_ev("PostToolUse", t_loop)
        check("a second PostToolUse inside the window makes no request",
              "no request made" in r2.stderr, r2.stderr[:160])
        check("...and writes nothing at all to stdout",
              r2.stdout == "", repr(r2.stdout[:80]))
        check("...and still exits 0", r2.returncode == 0)

        r3 = hook_ev("PostToolUse", t_loop, {"CHATROOM_HOOK_MIN_INTERVAL": "0"})
        check("with the interval disabled it checks again",
              "no request made" not in r3.stderr, r3.stderr[:160])

        # UserPromptSubmit must keep its old contract exactly: plain stdout, no throttle.
        box1.call("post_message", {"body": "prompt-path probe"})
        r4 = hook_ev("UserPromptSubmit", t_loop)
        check("UserPromptSubmit is never throttled",
              "no request made" not in r4.stderr, r4.stderr[:160])
        check("...and still emits plain text, not JSON",
              r4.stdout.lstrip().startswith("<chatroom_activity>"), r4.stdout[:80])

        # An unreachable bus during a long turn must not retry on every single tool call.
        # In-loop delivery must not consume the cursor: an event spent while the agent is
        # deep in unrelated work would otherwise never re-surface at a prompt.
        t_peek = mint("peekbox", "projA")
        box1.call("post_message", {"body": "peek probe one"})
        cursor_of = lambda a: rc.get(f"{BASE}/v1/rooms", headers={"Authorization": f"Bearer {a}"}).status_code
        p1 = hook_ev("PostToolUse", t_peek)
        check("in-loop delivery reaches the agent", "peek probe one" in p1.stdout, p1.stdout[:120])
        check("...via peek, leaving the cursor untouched",
              "cursor untouched" in p1.stderr, p1.stderr[:200])
        after_peek = Agent(t_peek).call("whats_new")
        check("...so the prompt-time path still has it to deliver",
              any("peek probe one" in (e.get("detail") or "") for e in after_peek["events"]),
              str(after_peek)[:200])

        # Peek returns the whole unread backlog each time, so without a local high-water
        # mark every in-loop check would re-inject the same events.
        t_dedup = mint("dedupbox", "projA")
        box1.call("post_message", {"body": "dedup probe alpha"})
        d_a = hook_ev("PostToolUse", t_dedup, {"CHATROOM_HOOK_MIN_INTERVAL": "0"})
        d_b = hook_ev("PostToolUse", t_dedup, {"CHATROOM_HOOK_MIN_INTERVAL": "0"})
        check("in-loop delivers new activity once", "dedup probe alpha" in d_a.stdout)
        check("...and stays silent on the next due check rather than repeating",
              d_b.stdout == "", d_b.stdout[:120])
        box1.call("post_message", {"body": "dedup probe beta"})
        d_c = hook_ev("PostToolUse", t_dedup, {"CHATROOM_HOOK_MIN_INTERVAL": "0"})
        check("...and delivers only the genuinely new event",
              "dedup probe beta" in d_c.stdout and "dedup probe alpha" not in d_c.stdout,
              d_c.stdout[:200])

        # The suppressed path runs on every tool call, so its import set is a real cost.
        src = (ROOT / "hooks" / "chatroom_whats_new.py").read_text()
        head = src.split("def _debug", 1)[0]
        for heavy in ("import urllib.request", "import urllib.error", "import tempfile",
                      "import pathlib", "import json"):
            check(f"module scope avoids {heavy.split()[1]} (deferred off the hot path)",
                  heavy not in head, heavy)
        sc = subprocess.run([sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py"),
                             "--selfcheck"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
        check("--selfcheck identifies the file offline, with the digest the header serves",
              "sha256" in sc.stdout and "peek=1" in sc.stdout, sc.stdout[:160])

        t_down = mint("downbox", "projA")
        d1 = hook_ev("PostToolUse", t_down, {"CHATROOM_URL": "http://127.0.0.1:9"})
        check("an unreachable bus fails open on the in-loop path",
              d1.returncode == 0 and d1.stdout == "", d1.stdout[:80])
        d2 = hook_ev("PostToolUse", t_down, {"CHATROOM_URL": "http://127.0.0.1:9"})
        check("...and is not retried on the next tool call either",
              "no request made" in d2.stderr, d2.stderr[:160])

        t_ev = mint("evbox", "projA")
        r5 = hook_ev("Stop", t_ev)
        check("Stop is treated as an in-loop event too",
              json.loads(r5.stdout)["hookSpecificOutput"]["hookEventName"] == "Stop"
              if r5.stdout.strip() else True, r5.stdout[:120])
        r6 = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            input="", capture_output=True, text=True,
            env={**{k: v for k, v in os.environ.items() if k != "CHATROOM_ROOM"},
                 "CHATROOM_URL": BASE, "CHATROOM_TOKEN": mint("nostdin", "projA"),
                 "CHATROOM_HOOK_DEBUG": "1"})
        # Regression: reading stdin unconditionally made the hook wait for EOF, so any
        # caller that left stdin open hung it — which stalls the agent on every tool call.
        import time as _t
        _start = _t.time()
        _p = subprocess.Popen(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "CHATROOM_TOKEN": "cr_never_used",
                 "CHATROOM_URL": "http://127.0.0.1:9"})
        try:
            _p.communicate(timeout=8)          # never write, never close stdin
            _hung = False
        except subprocess.TimeoutExpired:
            _p.kill(); _hung = True
        check("the hook never blocks on an open stdin",
              not _hung and _t.time() - _start < 8,
              f"took {_t.time() - _start:.1f}s")

        check("no stdin at all falls back to the prompt-path contract",
              r6.returncode == 0 and not r6.stdout.lstrip().startswith("{"),
              r6.stdout[:80])

        # --- watcher: push delivery via SSE -------------------------------------
        print("\n--- watcher: push delivery, modes, coalescing ---")
        WATCH = str(ROOT / "hooks" / "chatroom_watch.py")

        def watch_env(token, extra=None):
            e = {k: v for k, v in os.environ.items()
                 if not k.startswith("CHATROOM_WATCH_") and k != "CHATROOM_ROOM"}
            e.update({"CHATROOM_URL": BASE, "CHATROOM_TOKEN": token,
                      "CHATROOM_ROOM": "projA", "CHATROOM_WATCH_DEBUG": "1"})
            e.update(extra or {})
            return e

        def run_watcher(token, extra=None, seconds=6.0, posts=()):
            """Start the watcher, post as a peer while it runs, return (stdout, stderr).

            Terminating rather than waiting is the point: this process is designed never
            to exit on its own, so the test asserts on what it printed while alive.
            """
            p = subprocess.Popen([sys.executable, WATCH], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env=watch_env(token, extra))
            try:
                _t.sleep(2.0)                     # let it connect and drain backfill
                for body in posts:
                    box1.call("post_message", {"body": body})
                    _t.sleep(0.6)
                _t.sleep(seconds)
            finally:
                p.terminate()
                try:
                    out, err = p.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    out, err = p.communicate()
            return out, err

        t_watch = mint("watchbox", "projA")

        # Identity has to come from the credential, same as everywhere else: the watcher
        # cannot know its own name (and so cannot detect a mention) without asking.
        sc = subprocess.run([sys.executable, WATCH, "--selfcheck"],
                            capture_output=True, text=True, env=watch_env(t_watch))
        check("watcher --selfcheck reports version and its own digest",
              "chatroom_watch.py" in sc.stdout and re.search(r"sha256:\s+[0-9a-f]{64}", sc.stdout),
              sc.stdout[:160])

        # mentions is the default, so unrelated chatter must produce nothing at all.
        out, err = run_watcher(t_watch, posts=["watcher noise, nobody named here"])
        check("watcher identifies itself from the token", "as watchbox" in err, err[:160])
        check("default mode is mentions", "mode=mentions" in err, err[:160])
        check("...so unrelated chat produces no notification", out.strip() == "",
              repr(out[:160]))

        # A mention must arrive, and must arrive WITHOUT waiting out the 60s window —
        # that is the property that lets two agents actually converse.
        t0 = _t.time()
        out, err = run_watcher(t_watch, posts=["hey watchbox can you confirm this"],
                               seconds=4.0)
        check("a mention notifies", "@you" in out and "watchbox" in out, repr(out[:200]))
        check("...bypassing the 60s coalescing window",
              out.strip() != "" and _t.time() - t0 < 30, f"{_t.time() - t0:.0f}s")
        check("...as exactly one line", len([l for l in out.splitlines() if l.strip()]) == 1,
              repr(out[:200]))

        # Coalescing, with the window shortened so the behaviour is observable. The
        # budget starts full on purpose: the first thing that happens after you arm gets
        # through promptly, and only the follow-ups are batched.
        # Runs past a server keepalive (15s) on purpose: keepalives are what wake an idle
        # reader, so they set how promptly a pending batch can flush.
        out, werr = run_watcher(t_watch, {"CHATROOM_WATCH_MODE": "all",
                                          "CHATROOM_WATCH_MIN_INTERVAL": "5"},
                                posts=["broadcast one", "broadcast two", "broadcast three"],
                                seconds=20.0)
        lines = [l for l in out.splitlines() if l.strip()]

        # Regression: a socket timeout used to be treated as a tick, but http.client
        # poisons the connection once one fires, so every tick silently redialled TLS.
        # Short runs hid it completely — only an idle run longer than the timeout shows it.
        check("an idle watcher holds ONE connection instead of redialling",
              werr.count("connected:") == 1, f"{werr.count('connected:')} connects")
        check("...and does not log read timeouts while the server is healthy",
              "reconnecting" not in werr, werr[-200:])
        check("mode=all lets the first event through immediately",
              bool(lines) and "broadcast one" in lines[0] and "1 new" in lines[0],
              repr(out[:250]))
        check("...then coalesces the rest into ONE summary line",
              len(lines) == 2 and "broadcast two" in lines[1] and "broadcast three" in lines[1],
              f"{len(lines)} lines: " + repr(out[:250]))
        check("...which says how many it merged", len(lines) == 2 and "2 new" in lines[1],
              repr(out[:250]))
        check("a chat message notifies once, not once per underlying event",
              out.count("broadcast two") == 1, repr(out[:250]))

        out, _ = run_watcher(t_watch, {"CHATROOM_WATCH_MODE": "all",
                                       "CHATROOM_WATCH_MIN_INTERVAL": "0"},
                             posts=["immediate one", "immediate two"], seconds=3.0)
        check("a zero window emits each event as its own notification",
              len([l for l in out.splitlines() if l.strip()]) == 2, repr(out[:250]))

        # hook-only at launch must not hold a connection open for nothing.
        honly = subprocess.run([sys.executable, WATCH, "--mode", "hook-only"],
                            capture_output=True, text=True, timeout=20,
                            env=watch_env(t_watch))
        check("mode=hook-only exits instead of arming",
              honly.returncode == 0 and honly.stdout == "" and "nothing to watch" in honly.stderr,
              (honly.stdout + honly.stderr)[:160])

        # Runtime switching: --set-mode has to retarget a watcher that is already running,
        # or the mode is only settable by restarting, which an agent cannot do to itself.
        # Its own token, so the mode file it writes cannot colour any other assertion —
        # the state path is keyed on (server, credential, room) precisely so it is scoped.
        t_setmode = mint("setmodebox", "projA")
        state_probe = subprocess.run([sys.executable, WATCH, "--set-mode", "all"],
                                     capture_output=True, text=True, env=watch_env(t_setmode))
        check("--set-mode writes the mode and exits",
              state_probe.returncode == 0 and "mode=all" in state_probe.stderr,
              (state_probe.stdout + state_probe.stderr)[:160])
        check("...and echoes the resolved match pattern while it is at it",
              "matching:" in state_probe.stderr, state_probe.stderr[:160])
        out, err = run_watcher(t_setmode, posts=["set-mode took effect"], seconds=4.0)
        check("...and a watcher launched afterwards honours the file over its default",
              "set-mode took effect" in out, repr(out[:200]) + " | " + err[:120])

        # An agent must never be woken for its own work — the single most common way a
        # notification loop turns into a feedback loop.
        self_env = watch_env(t_watch, {"CHATROOM_WATCH_MODE": "all"})
        p = subprocess.Popen([sys.executable, WATCH], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self_env)
        try:
            _t.sleep(2.0)
            Agent(t_watch).call("post_message", {"body": "watchbox talking to itself"})
            _t.sleep(4.0)
        finally:
            p.terminate()
            self_out, _ = p.communicate(timeout=10)
        check("an agent is never notified about its own message",
              "talking to itself" not in self_out, repr(self_out[:200]))

        # A bad credential must say so and stop. Failing open here would be silent:
        # the watcher would retry forever and the room would just look quiet.
        bad = subprocess.run([sys.executable, WATCH], capture_output=True, text=True,
                             timeout=25, env=watch_env("cr_watch_bogus"))
        check("a rejected token stops the watcher instead of retrying silently",
              bad.returncode != 0 and "cannot identify" in (bad.stdout + bad.stderr).lower(),
              (bad.stdout + bad.stderr)[:160])
        check("...and says the token was rejected, not that the bus was unreachable",
              "401" in bad.stderr and "not recognised" in bad.stderr, bad.stderr[:160])

        # Reported from the field by a second tunnel client: their first connect attempt
        # got a 502 from the Cloudflare edge (transient, origin healthy). A watcher armed
        # during one of those must survive it rather than exiting — but a bad credential
        # must still fail fast, so the two cases have to stay distinguishable.
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Flaky(BaseHTTPRequestHandler):
            hits = 0
            code = 502

            def do_GET(self):                      # noqa: N802 - stdlib naming
                Flaky.hits += 1
                if Flaky.hits <= 2 and Flaky.code == 502:
                    self.send_response(502)
                    self.end_headers()
                    return
                if Flaky.code != 502:
                    self.send_response(Flaky.code)
                    self.end_headers()
                    return
                body = json.dumps({"agent": "flakybox", "default_room": "projA"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):             # noqa: A002 - silence stdlib logging
                pass

        stub = HTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        stub_url = f"http://127.0.0.1:{stub.server_address[1]}"
        try:
            Flaky.hits, Flaky.code = 0, 502
            p = subprocess.Popen([sys.executable, WATCH], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env={**watch_env(t_watch), "CHATROOM_URL": stub_url})
            try:
                _t.sleep(9.0)                      # spans the 1s + 2s retry backoff
            finally:
                p.terminate()
                _, ferr = p.communicate(timeout=10)
            check("a transient 502 at connect is retried, not fatal",
                  Flaky.hits >= 3 and "as flakybox" in ferr, f"{Flaky.hits} hits | {ferr[:160]}")

            Flaky.hits, Flaky.code = 0, 401
            fatal = subprocess.run([sys.executable, WATCH], capture_output=True, text=True,
                                   timeout=30,
                                   env={**watch_env(t_watch), "CHATROOM_URL": stub_url})
            check("...but a 401 still fails fast instead of burning the retry budget",
                  fatal.returncode != 0 and Flaky.hits == 1, f"{Flaky.hits} hits")
        finally:
            stub.shutdown()

        # --- mention matching: the invisible-failure surface -------------------
        # A mis-targeted watcher and a healthy one look identical (connected, silent),
        # so every one of these is about making a miss impossible or visible.
        sys.path.insert(0, str(ROOT / "hooks"))
        import chatroom_watch as cw

        # Reported from the field: peers write "the 4821", not "srv4821". Measured against
        # a real 234k-char room before defaulting on — zero false matches, one genuine
        # reference the full-name pattern dropped.
        check("a bare numeric run in the agent name is derived as an alias",
              cw._aliases("srv4821") == ["4821"] and cw._aliases("host-5150") == ["5150"],
              f'{cw._aliases("srv4821")} {cw._aliases("host-5150")}')
        check("...but not from a short number that would collide with ports etc",
              cw._aliases("box-80") == [] and cw._aliases("ops") == [],
              f'{cw._aliases("box-80")} {cw._aliases("ops")}')

        mre = cw._mention_re("srv4821", "")
        check("the derived alias matches the prose form peers actually use",
              bool(mre.search("compare 4821's numbers to mine"))
              and bool(mre.search("on the 4821 only")), mre.pattern)
        # The alternation bug: "|" binds looser than concatenation, so an unwrapped
        # `(?<!..)a|b(?!..)` guards only the first alternative and only the last. It was
        # accidentally correct with a single name and broken the instant a second existed.
        check("alternation is grouped, so BOTH boundaries apply to EVERY alternative",
              not mre.search("srv4821x") and not mre.search("x7740")
              and not mre.search("~7740ms"), mre.pattern)
        multi = cw._mention_re("boxone", "boxtwo,boxthree")
        check("...with extra mentions too, which is where the bug used to bite",
              bool(multi.search("hi boxtwo")) and not multi.search("boxtwoish")
              and not multi.search("aboxthree"), multi.pattern)
        check("CHATROOM_WATCH_ALIAS=off suppresses derivation",
              (lambda: (os.environ.__setitem__("CHATROOM_WATCH_ALIAS", "off"),
                        cw._aliases("srv4821") == [],
                        os.environ.pop("CHATROOM_WATCH_ALIAS"))[1])())

        # The read-once inconsistency: --set-mode took effect live but mentions did not,
        # and the operator who needs a new alias is by definition one already missing
        # messages. Both now live in the same re-read state file.
        sp = cw._state_path(BASE, t_watch, "projA")
        try:
            cw._write_state(sp, mode="all", mentions="shortname")
            st = cw._read_state(sp)
            check("state file carries mode AND mentions together",
                  st.get("mode") == "all" and st.get("mentions") == "shortname", str(st))
            with open(sp, "w", encoding="utf-8") as fh:
                fh.write("mentions\n")          # pre-1.2 bare-word format
            check("a legacy bare-mode state file still reads",
                  cw._read_mode(sp) == "mentions", str(cw._read_state(sp)))

            # The compatibility that matters is OLD reader / NEW file: this file steers a
            # process that has been running for days, so the reader predates the writer by
            # construction. A pre-1.2 reader consumes the whole file and compares it to
            # MODES, so a `mode=x` line parses as None and it silently keeps its launch
            # mode -- a control that reports success and does nothing. Found in production
            # after --set-mode was ignored twice by an 11-day-old watcher.
            def legacy_read(p):                 # verbatim pre-1.2 _read_mode
                with open(p, encoding="utf-8") as fh:
                    v = fh.read().strip()
                return v if v in cw.MODES else None

            cw._write_state(sp, mode="hook-only")
            check("a mode-only write stays parseable by a PRE-1.2 reader",
                  legacy_read(sp) == "hook-only", repr(open(sp).read()))
            check("...and by the current one", cw._read_mode(sp) == "hook-only",
                  repr(open(sp).read()))
            cw._write_state(sp, mentions="alias1")
            check("adding mentions switches to key=value (old reader degrades, as designed)",
                  cw._read_state(sp) == {"mode": "hook-only", "mentions": "alias1"},
                  repr(open(sp).read()))
        finally:
            try:
                os.unlink(sp)
            except OSError:
                pass

        # --selfcheck is the command an operator runs to answer "what is my watcher doing
        # right now". Reporting the env default while a running watcher is in another mode
        # is the same invisible-state defect as an unprintable match pattern.
        subprocess.run([sys.executable, WATCH, "--set-mode", "all"],
                       capture_output=True, text=True, env=watch_env(t_watch))
        sc2 = subprocess.run([sys.executable, WATCH, "--selfcheck"], capture_output=True,
                             text=True, env=watch_env(t_watch, {"CHATROOM_WATCH_MODE": "mentions"}))
        check("--selfcheck reports the LIVE mode, not the env default",
              "mode:" in sc2.stdout
              and re.search(r"mode:\s+all\b.*state file", sc2.stdout), sc2.stdout[:400])
        check("...and resolves the agent name and match pattern",
              "watchbox" in sc2.stdout and "matching:" in sc2.stdout, sc2.stdout[:400])
        # Must still be usable offline for digest verification — that is its other job.
        off = subprocess.run([sys.executable, WATCH, "--selfcheck"], capture_output=True,
                             text=True, timeout=60,
                             env={**watch_env(t_watch), "CHATROOM_URL": "http://127.0.0.1:9"})
        check("--selfcheck still works with the bus unreachable",
              off.returncode == 0 and re.search(r"sha256:\s+[0-9a-f]{64}", off.stdout),
              off.stdout[:200])
        check("...and says it could not read the live mode rather than implying it did",
              "could NOT read live mode" in off.stdout, off.stdout[:400])
        setm = subprocess.run([sys.executable, WATCH, "--set-mentions", "shortname"],
                              capture_output=True, text=True, env=watch_env(t_watch))
        check("--set-mentions reports the RESOLVED pattern, not just the setting",
              "matching:" in setm.stderr and "shortname" in setm.stderr, setm.stderr[:200])
        out, werr2 = run_watcher(t_watch, posts=["calling shortname now"], seconds=4.0)
        check("...and a mention via that alias is delivered",
              "shortname" in out, repr(out[:200]))
        check("the resolved pattern is printed at startup WITHOUT needing DEBUG",
              "matching:" in werr2, werr2[:200])
        # Remove the state file, not just blank its fields: it legitimately outranks the
        # env, so anything left here silently overrides CHATROOM_WATCH_MODE in every later
        # assertion — which is exactly how it broke the cold-start check once already.
        try:
            os.unlink(cw._state_path(BASE, t_watch, "projA"))
        except OSError:
            pass

        # Cold start must not replay: an armed watcher reports what happens next.
        box1.call("post_message", {"body": "posted BEFORE the watcher arms"})
        out, _ = run_watcher(t_watch, {"CHATROOM_WATCH_MODE": "all"},
                             posts=["posted AFTER the watcher arms"], seconds=4.0)
        check("a cold start does not replay history as notifications",
              "BEFORE the watcher arms" not in out, repr(out[:250]))
        check("...but does deliver what happens next", "AFTER the watcher arms" in out,
              repr(out[:250]))

        # after_msg is what stops a reconnect from replaying history as "new".
        hw = rc.get(f"{BASE}/v1/messages?room=projA",
                    headers={"Authorization": f"Bearer {t_watch}"}).json()
        top = max((m["id"] for m in hw.get("messages", [])), default=0)
        with rc.stream("GET", f"{BASE}/v1/stream?room=projA&after=999999&after_msg={top}",
                       headers={"Authorization": f"Bearer {t_watch}"},
                       timeout=6) as sresp:
            got = ""
            try:
                for chunk in sresp.iter_text():
                    got += chunk
                    if len(got) > 400:
                        break
            except Exception:                     # noqa: BLE001 - read timeout ends the peek
                pass
        check("/v1/stream honours after_msg (no replay on reconnect)",
              "event: chat" not in got, repr(got[:200]))

        # The server must hand out the watcher the same way it hands out the hook, or a
        # remote box has no way to get it without a clone.
        wsrc = rc.get(f"{BASE}/v1/watch", headers={"Authorization": f"Bearer {t_watch}"})
        disk = (ROOT / "hooks" / "chatroom_watch.py").read_bytes()
        check("/v1/watch serves the watcher source",
              wsrc.status_code == 200 and "chatroom_watch" in wsrc.text, str(wsrc.status_code))
        check("...with a digest header matching the bytes on disk",
              wsrc.headers.get("X-Chatroom-Hook-SHA256") ==
              __import__("hashlib").sha256(disk).hexdigest(),
              str(wsrc.headers.get("X-Chatroom-Hook-SHA256")))
        check("/v1/watch requires a token",
              rc.get(f"{BASE}/v1/watch").status_code == 401)

        print("\n--- exposure hardening (tunnel / public reachability) ---")
        from chatroom import security

        exp = security.expand_allowed_hosts(["chat.example.com:*", "10.0.0.5:8090", " ", ""])
        check("host allowlist keeps the ':*' form", "chat.example.com:*" in exp, str(exp))
        check("host allowlist also covers the portless form",
              "chat.example.com" in exp, str(exp))
        check("host allowlist preserves an exact host:port entry",
              "10.0.0.5:8090" in exp and len([e for e in exp if e.strip()]) == 3, str(exp))

        # A tunnel/HTTPS front door forwards `Host: name` with no port at all, so
        # the ':*' entries must admit it or every tunnelled /mcp call 421s while the
        # REST routes (which are not Host-checked) keep working — a confusing split.
        mcp_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        mcp_hdrs = {"Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {t_box1}"}
        portless = rc.post(f"{BASE}/mcp", json=mcp_body,
                           headers={**mcp_hdrs, "Host": "127.0.0.1"})
        check("portless Host is accepted on /mcp (the tunnel case)",
              portless.status_code == 200, f"{portless.status_code} {portless.text[:80]}")
        foreign = rc.post(f"{BASE}/mcp", json=mcp_body,
                          headers={**mcp_hdrs, "Host": "evil.example.com"})
        check("an unlisted Host is still rejected with 421",
              foreign.status_code == 421, str(foreign.status_code))

        # Only failures count, and a good credential is never throttled: agents
        # routinely share one apparent address behind a tunnel or NAT, so one stale
        # token must not be able to lock out its peers.
        limit = int(os.environ.get("CHATROOM_AUTH_FAIL_LIMIT", "20"))
        codes = [rc.get(f"{BASE}/v1/tasks",
                        headers={"Authorization": f"Bearer cr_spray_{i}"}).status_code
                 for i in range(limit + 2)]
        check("failed credentials start as 401", codes[0] == 401, str(codes[:3]))
        check("failed credentials become 429 once the budget is spent",
              codes[-1] == 429, str(codes[-3:]))
        spent = rc.get(f"{BASE}/v1/tasks",
                       headers={"Authorization": "Bearer cr_spray_more"})
        check("throttled response advertises Retry-After",
              spent.status_code == 429 and int(spent.headers.get("Retry-After", 0)) > 0,
              f"{spent.status_code} {spent.headers.get('Retry-After')}")
        check("a VALID token is never throttled (shared-address safety)",
              rc.get(f"{BASE}/v1/tasks", headers=h).status_code == 200)
        check("a valid MCP call also survives the throttle",
              rc.post(f"{BASE}/mcp", json=mcp_body, headers=mcp_hdrs).status_code == 200)
        check("a good credential does not reset the window for the address",
              rc.get(f"{BASE}/v1/tasks",
                     headers={"Authorization": "Bearer cr_spray_after"}).status_code == 429)

        # --- self-describing clients -------------------------------------------
        print("\n--- self-describing clients (manifest, version drift, announce) ---")
        from chatroom import db as _db, server as _srv

        man = rc.get(f"{BASE}/v1/client", headers=h)
        # Not 200 rather than ==401: the hardening section above deliberately spends this
        # address's failed-auth budget, so an unauthenticated call here may be throttled to
        # 429 before it is ever judged on the credential. Either way it is refused.
        check("/v1/client requires a token",
              rc.get(f"{BASE}/v1/client").status_code != 200)
        check("/v1/client returns a manifest", man.status_code == 200, str(man.status_code))
        mj = man.json() if man.status_code == 200 else {}
        check("...naming the server version",
              mj.get("server", {}).get("version") == _srv.SERVER_VERSION,
              str(mj.get("server")))
        check("...and both client scripts",
              set(mj.get("scripts", {})) == {"hook", "watch"}, str(list(mj.get("scripts", {}))))
        hook_meta = mj.get("scripts", {}).get("hook", {})
        check("...with a version parsed from the script",
              hook_meta.get("version", "").count(".") >= 1, str(hook_meta.get("version")))
        # The digest must match what /v1/hook actually serves, or the manifest is a
        # second source of truth that can disagree with the file — the exact failure
        # (a built-but-not-recreated container serving a stale copy) this is meant to catch.
        served = rc.get(f"{BASE}/v1/hook", headers=h)
        check("...and a sha256 matching the bytes /v1/hook serves",
              hook_meta.get("sha256") == served.headers.get("X-Chatroom-Hook-SHA256"),
              f"{hook_meta.get('sha256')} vs {served.headers.get('X-Chatroom-Hook-SHA256')}")

        wn = rc.get(f"{BASE}/v1/whats_new?peek=1", headers=h)
        check("whats_new advertises the canonical hook version in a header",
              wn.headers.get("X-Chatroom-Hook-Version") == hook_meta.get("version"),
              str(wn.headers.get("X-Chatroom-Hook-Version")))
        check("...and the server version too",
              wn.headers.get("X-Chatroom-Server-Version") == _srv.SERVER_VERSION)

        # Version drift notice: compares the DECLARED version, not the bytes, so an
        # intentionally adapted local copy is not branded stale forever.
        sys.path.insert(0, str(ROOT / "hooks"))
        import chatroom_whats_new as _hook
        tmpdir = tempfile.mkdtemp(prefix="chatroom-notice-")
        os.environ["TMPDIR"] = tmpdir
        check("no drift notice when versions match",
              _hook._version_notice(_hook.__version__, "tok", "ops") == "")
        n1 = _hook._version_notice("99.0.0", "tok", "ops")
        check("a newer server version produces one notice", n1.startswith("chatroom:"), n1)
        check("...naming both versions",
              _hook.__version__ in n1 and "99.0.0" in n1, n1)
        check("...and is throttled so it cannot repeat every turn",
              _hook._version_notice("99.0.0", "tok", "ops") == "")
        os.environ["CHATROOM_HOOK_VERSION_CHECK"] = "off"
        check("...and can be silenced entirely",
              _hook._version_notice("99.0.0", "tok", "other-room") == "")
        os.environ.pop("CHATROOM_HOOK_VERSION_CHECK")
        check("an absent header is not treated as drift",
              _hook._version_notice("", "tok", "ops") == "")

        # Watcher liveness is opt-in: a box that never wanted push delivery must not be
        # nagged about a watcher it deliberately does not run.
        os.environ.pop("CHATROOM_WATCH_EXPECTED", None)
        check("watcher notice stays silent when not opted in", _hook._watcher_notice() == "")
        os.environ["CHATROOM_WATCH_EXPECTED"] = "1"
        check("...reports a watcher that has never beaten once opted in",
              "never reported" in _hook._watcher_notice())
        beat = Path(tmpdir) / "chatroom-watch-heartbeat"
        beat.write_text("now\n")
        check("...stays silent while the heartbeat is fresh", _hook._watcher_notice() == "")
        os.utime(beat, (time.time() - 4000, time.time() - 4000))
        check("...and reports a stale heartbeat as dead",
              "stale" in _hook._watcher_notice())
        os.environ.pop("CHATROOM_WATCH_EXPECTED")

        # Upgrade announcement: on version CHANGE only, never on a plain restart.
        c2 = _db.connect(DB)
        _db.ensure_schema(c2)
        rooms = _db.list_rooms(c2)
        _db.set_meta(c2, "announced_version", "0.0.1")
        posted = _srv.announce_upgrade(c2, "9.9.9")
        check("an upgrade is announced into every room",
              posted == rooms and len(rooms) > 1, f"posted={posted} rooms={rooms}")
        body = _db.read_messages(c2, rooms[0], 0, 200)[-1]["body"]
        check("...naming both the old and new version",
              "0.0.1" in body and "9.9.9" in body, body[:120])
        check("...and recording the new version", _db.get_meta(c2, "announced_version") == "9.9.9")
        check("a restart at the SAME version announces nothing",
              _srv.announce_upgrade(c2, "9.9.9") == [])
        _db.set_meta(c2, "announced_version", "9.9.9")
        os.environ["CHATROOM_ANNOUNCE_UPGRADES"] = "off"
        check("...and the announcement can be turned off",
              _srv.announce_upgrade(c2, "10.0.0") == [])
        os.environ.pop("CHATROOM_ANNOUNCE_UPGRADES")
        # A fresh install has nobody to tell and nothing to compare against.
        c2.execute("DELETE FROM meta WHERE key='announced_version'")
        check("a first boot records the version silently",
              _srv.announce_upgrade(c2, "1.0.0") == []
              and _db.get_meta(c2, "announced_version") == "1.0.0")
        c2.close()

        # last_seen throttling: the auth path must not write on every request.
        check("last_seen refreshes when never set", _db._last_seen_is_stale(None))
        check("...is skipped when written moments ago",
              not _db._last_seen_is_stale(_db.now()))
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        check("...refreshes again once stale", _db._last_seen_is_stale(old))
        check("...and heals from an unparseable value", _db._last_seen_is_stale("not-a-date"))
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        check("...and from a clock that jumped forward", _db._last_seen_is_stale(future))

        print(f"\n{'=' * 62}")
        print(f"{len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            for f in FAIL:
                print("  FAILED:", f)
        return 1 if FAIL else 0
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    sys.exit(main())
