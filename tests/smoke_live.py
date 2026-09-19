#!/usr/bin/env python3
"""Live smoke test against a REAL Planner plan. Creates a bucket named
`kairos-smoke` and a few tasks in it, exercises every tool, and leaves the
tasks completed (there is no delete). Delete the bucket in the Planner UI
afterwards if you want it gone.

Usage:
    export KAIROS_CLIENT_ID=... KAIROS_TENANT_ID=...
    kairos-mcp auth                       # once
    python tests/smoke_live.py [plan-name]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402

BUCKET = "kairos-smoke"
STAMP = time.strftime("%Y%m%d-%H%M%S")
fails = 0


def step(name, out, want=None):
    global fails
    ok = (want in out) if want else True
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("      " + out.replace("\n", "\n      ")[:600])
    if not ok:
        fails += 1
    time.sleep(0.5)
    return out


plan = sys.argv[1] if len(sys.argv) > 1 else ""

out = step("auth_status", server.kairos_auth_status(), '"graph_ok": true')
if "graph_ok" not in out:
    sys.exit("not signed in — run `kairos-mcp auth` first")

out = step("list_planner_plans", server.list_planner_plans(refresh=True), '"plans"')
plans = json.loads(out)["plans"]
if not plan and len(plans) > 1 and not server.DEFAULT_PLAN:
    sys.exit(f"pass a plan name; you have {len(plans)}: " + ", ".join(p['plan_name'] for p in plans))

step("create_planner_bucket (new)", server.create_planner_bucket(BUCKET, plan=plan), "bucket")
step("create_planner_bucket (idempotent)", server.create_planner_bucket(BUCKET.upper(), plan=plan), "already exists")

t1 = json.loads(step("create_planner_task (notes + due)",
                     server.create_planner_task(f"kairos smoke {STAMP} alpha",
                                                notes=f"created by smoke_live.py at {STAMP}\nline two",
                                                plan=plan, bucket=BUCKET, due_on="2026-12-31"),
                     '"status": "task created"'))
print("      open this URL and check title/notes/due date:", t1.get("url"))

step("create_planner_task (dup 1)", server.create_planner_task(f"kairos smoke {STAMP} dup", plan=plan, bucket=BUCKET), "task created")
step("create_planner_task (dup 2)", server.create_planner_task(f"kairos smoke {STAMP} dup", plan=plan, bucket=BUCKET), "task created")

step("list_planner_tasks (bucket filter)", server.list_planner_tasks(plan=plan, bucket=BUCKET), '"count": 3')

step("complete by title — ambiguous, must refuse",
     server.complete_planner_task(title=f"smoke {STAMP} dup", plan=plan), "found 2 open tasks")

step("complete by title — unique", server.complete_planner_task(title=f"smoke {STAMP} alpha", plan=plan), '"completed": true')
step("complete by id — already complete", server.complete_planner_task(task_id=t1["task_id"]), "already complete")
step("reopen by id", server.complete_planner_task(task_id=t1["task_id"], reopen=True), '"completed": false')
step("list (open only) shows 3 again", server.list_planner_tasks(plan=plan, bucket=BUCKET), '"count": 3')
step("complete alpha again by id", server.complete_planner_task(task_id=t1["task_id"]), '"completed": true')

# tidy: complete the dups by id so the bucket is left with no open tasks
for t in json.loads(server.list_planner_tasks(plan=plan, bucket=BUCKET))["tasks"]:
    step(f"complete leftover {t['title'][-3:]}", server.complete_planner_task(task_id=t["task_id"]), '"completed": true')

step("list include_completed", server.list_planner_tasks(plan=plan, bucket=BUCKET, include_completed=True), '"count": 3')
step("bad date rejected", server.create_planner_task("x", plan=plan, due_on="12/31/2026"), "YYYY-MM-DD")
step("unknown bucket lists options", server.create_planner_task("x", plan=plan, bucket="no-such-bucket"), "no bucket named")

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'} — bucket {BUCKET!r} left in place with 3 completed tasks; delete it in Planner if you like.")
sys.exit(1 if fails else 0)
