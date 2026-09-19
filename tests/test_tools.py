"""Tool-logic tests against a fake Graph. Run: python -m pytest tests/ -q
No network, no MSAL. _token and _graph are monkeypatched."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["KAIROS_CLIENT_ID"] = "test-client"
os.environ["KAIROS_CONFIG_DIR"] = "/tmp/kairos-test-config"
import server  # noqa: E402


class FakeGraph:
    """Enough of Planner to exercise every tool."""

    def __init__(self):
        self.plans = {"P1": "Development", "P2": "Master"}
        self.buckets = {"B1": ("P1", "argus"), "B2": ("P1", "To do"), "B3": ("P2", "Inbox")}
        self.tasks = {}          # id -> dict
        self.details = {}        # id -> {"description":..., "etag":...}
        self.calls = []
        self.n = 0

    def __call__(self, method, path, token, **kw):
        self.calls.append((method, path, kw))
        body = kw.get("json") or {}
        hdr = kw.get("headers") or {}
        if path.startswith("/me/planner/plans"):
            return {"value": [{"id": k, "title": v} for k, v in self.plans.items()]}, 200, {}
        if path.startswith("/planner/plans/") and "/buckets" in path:
            pid = path.split("/")[3]
            return {"value": [{"id": k, "name": n} for k, (p, n) in self.buckets.items() if p == pid]}, 200, {}
        if path.startswith("/planner/plans/") and "/tasks" in path:
            pid = path.split("/")[3]
            return {"value": [dict(t, id=k) for k, t in self.tasks.items() if t["planId"] == pid]}, 200, {}
        if method == "POST" and path == "/planner/buckets":
            self.n += 1
            bid = f"B{self.n + 10}"
            self.buckets[bid] = (body["planId"], body["name"])
            return {"id": bid, "name": body["name"]}, 201, {}
        if method == "POST" and path == "/planner/tasks":
            self.n += 1
            tid = f"T{self.n}"
            self.tasks[tid] = {"planId": body["planId"], "bucketId": body["bucketId"],
                               "title": body["title"], "percentComplete": 0,
                               "dueDateTime": body.get("dueDateTime"), "@odata.etag": "W/\"1\""}
            self.details[tid] = {"description": "", "@odata.etag": "W/\"d1\""}
            return {"id": tid}, 201, {}
        if path.endswith("/details"):
            tid = path.split("/")[3]
            if tid not in self.details:
                return {"error": {"message": "nope"}}, 404, {}
            if method == "GET":
                return self.details[tid], 200, {}
            if method == "PATCH":
                assert hdr.get("If-Match") == self.details[tid]["@odata.etag"], "missing ETag"
                self.details[tid]["description"] = body["description"]
                return {}, 204, {}
        if path.startswith("/planner/tasks/"):
            tid = path.split("/")[3].split("?")[0]
            if tid not in self.tasks:
                return {}, 404, {}
            if method == "GET":
                return dict(self.tasks[tid], id=tid), 200, {}
            if method == "PATCH":
                assert hdr.get("If-Match") == self.tasks[tid]["@odata.etag"], "missing ETag"
                self.tasks[tid]["percentComplete"] = body["percentComplete"]
                return {}, 204, {}
        raise AssertionError(f"unhandled {method} {path}")


@pytest.fixture
def g(monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(server, "_graph", fake)
    monkeypatch.setattr(server, "_token", lambda: ("tok", None))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "DEFAULT_PLAN", "")
    server._invalidate_plans()
    return fake


def test_list_plans(g):
    out = json.loads(server.list_planner_plans())
    assert [p["plan_name"] for p in out["plans"]] == ["Development", "Master"]
    assert out["plans"][0]["buckets"][0]["bucket_name"] == "argus"


def test_plan_required_when_ambiguous(g):
    out = server.create_planner_task("x")
    assert "plan is required" in out and "'Development'" in out


def test_default_plan_env(g, monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_PLAN", "development")
    out = json.loads(server.create_planner_task("Fix it"))
    assert out["plan"] == "Development" and out["bucket"] == "argus"


def test_create_task_with_notes_and_due(g):
    out = json.loads(server.create_planner_task("Rotate token", notes="exposed in chat",
                                                plan="Development", bucket="to DO", due_on="2026-10-01"))
    assert out["task_id"] == "T1" and out["bucket"] == "To do" and out["due_on"] == "2026-10-01"
    assert g.tasks["T1"]["dueDateTime"] == "2026-10-01T12:00:00Z"
    assert g.details["T1"]["description"] == "exposed in chat"
    assert "warning" not in out
    assert out["url"].endswith("/task/T1")


def test_bad_date(g):
    assert "YYYY-MM-DD" in server.create_planner_task("x", plan="Master", due_on="10/01/2026")


def test_unknown_bucket_lists_options(g):
    out = server.create_planner_task("x", plan="Development", bucket="nope")
    assert "no bucket named 'nope'" in out and "'argus'" in out


def test_bucket_idempotent(g):
    out = json.loads(server.create_planner_bucket("ARGUS", plan="Development"))
    assert out["bucket_id"] == "B1" and "already exists" in out["status"]
    out = json.loads(server.create_planner_bucket("new-thing", plan="Development"))
    assert out["status"] == "bucket created"
    # visible immediately without refresh
    out = json.loads(server.list_planner_plans())
    assert "new-thing" in [b["bucket_name"] for b in out["plans"][0]["buckets"]]


def test_list_tasks_filters(g):
    server.create_planner_task("a", plan="Development", bucket="argus")
    server.create_planner_task("b", plan="Development", bucket="To do")
    server.create_planner_task("c", plan="Master")
    out = json.loads(server.list_planner_tasks(plan="Development"))
    assert out["count"] == 2
    out = json.loads(server.list_planner_tasks(plan="Development", bucket="argus"))
    assert out["count"] == 1 and out["tasks"][0]["title"] == "a"
    out = json.loads(server.list_planner_tasks(plan="*"))
    assert out["count"] == 3


def test_complete_by_id_then_reopen(g):
    tid = json.loads(server.create_planner_task("done me", plan="Master"))["task_id"]
    out = json.loads(server.complete_planner_task(task_id=tid))
    assert out["completed"] is True and g.tasks[tid]["percentComplete"] == 100
    out = json.loads(server.complete_planner_task(task_id=tid))
    assert "already complete" in out["status"]
    assert json.loads(server.list_planner_tasks(plan="Master"))["count"] == 0
    assert json.loads(server.list_planner_tasks(plan="Master", include_completed=True))["count"] == 1
    out = json.loads(server.complete_planner_task(task_id=tid, reopen=True))
    assert out["completed"] is False and g.tasks[tid]["percentComplete"] == 0


def test_complete_by_title_unique_and_ambiguous(g):
    server.create_planner_task("Deploy sentinel", plan="Development")
    server.create_planner_task("Deploy argus", plan="Development")
    server.create_planner_task("Write ADR", plan="Development")
    out = server.complete_planner_task(title="deploy", plan="Development")
    assert out.startswith("found 2 open tasks matching 'deploy'") and "T1" in out and "T2" in out
    out = json.loads(server.complete_planner_task(title="write adr", plan="Development"))
    assert out["title"] == "Write ADR" and out["completed"] is True
    # exact match wins over substring
    server.create_planner_task("Deploy", plan="Development")
    out = json.loads(server.complete_planner_task(title="Deploy", plan="Development"))
    assert out["title"] == "Deploy"


def test_complete_unknown(g):
    assert "no task with id" in server.complete_planner_task(task_id="ZZZ")
    assert "give either" in server.complete_planner_task()


def test_kill_switch(g, monkeypatch):
    monkeypatch.setenv("KAIROS_DISABLED", "1")
    assert "DISABLED" in server.create_planner_task("x", plan="Master")


def test_auth_error_surfaces(g, monkeypatch):
    monkeypatch.setattr(server, "_token", lambda: (None, server.REAUTH_HINT))
    assert "kairos-mcp auth" in server.list_planner_plans()


def test_graph_retry_429(monkeypatch):
    calls = []

    class R:
        def __init__(self, code):
            self.status_code = code
            self.headers = {"Retry-After": "1"}
            self.content = b"{}"
            self.text = "{}"

        def json(self):
            return {}

    def fake_request(method, url, **kw):
        calls.append(url)
        return R(429 if len(calls) < 3 else 200)

    monkeypatch.setattr(server.requests, "request", fake_request)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    _, code, _ = server._graph("GET", "/x", "tok")
    assert code == 200 and len(calls) == 3
