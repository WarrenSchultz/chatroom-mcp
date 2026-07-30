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
import json
import os
import subprocess
import sys
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

    env = {**os.environ, "CHATROOM_DB": DB, "PYTHONPATH": str(ROOT)}
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
            capture_output=True, text=True,
            env={**os.environ, "CHATROOM_URL": BASE, "CHATROOM_TOKEN": t_box2})
        check("hook exits 0", hook.returncode == 0, hook.stderr[-300:])
        check("hook emits activity block", "<chatroom_activity>" in hook.stdout, hook.stdout[:200])
        check("hook labels peer content untrusted", "UNTRUSTED DATA" in hook.stdout)
        hook2 = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True,
            env={**os.environ, "CHATROOM_URL": BASE, "CHATROOM_TOKEN": t_box2})
        check("hook is silent when nothing is new", hook2.stdout.strip() == "", hook2.stdout[:200])
        dead = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True,
            env={**os.environ, "CHATROOM_URL": "http://127.0.0.1:9", "CHATROOM_TOKEN": t_box2})
        check("hook fails open when the bus is unreachable",
              dead.returncode == 0 and dead.stdout.strip() == "", dead.stdout[:200])

        print("\n--- revocation ---")
        subprocess.run([sys.executable, "-m", "chatroom.admin", "revoke", "--agent", "box2"],
                       cwd=ROOT, env=env, capture_output=True)
        check("revoked token stops working",
              "_tool_error" in Agent(t_box2).call("list_tasks"))
        check("other agents are unaffected", box1.call("list_tasks").get("room") == "projA")

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
