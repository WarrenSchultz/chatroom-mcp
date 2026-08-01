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

        # The hook and the whats_new() tool share one cursor and the hook consumes it, so a
        # hook-running agent's own whats_new() reports 0. Intended, but it reads as "the room
        # is quiet", which misled a live agent — so pin the behaviour and keep the docs honest.
        t_cursor = mint("cursorshare", "projA")
        box1.call("post_message", {"body": "activity for the cursor-sharing check"})
        injected = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "chatroom_whats_new.py")],
            capture_output=True, text=True,
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
                capture_output=True, text=True, env=e)

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
