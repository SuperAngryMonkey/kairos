#!/usr/bin/env python3
"""
kairos — a Microsoft Planner MCP that writes tasks.

The opportune moment. An agent creates tasks and buckets in your Planner
plans, lists what is there, and marks tasks complete. That is the whole job.

This is the deliberately un-safe sibling of iris. iris cannot send mail, by
construction. kairos writes to Planner, by construction — creating a task
creates a task, completing one completes one. The controls below narrow the
blast radius; they do not remove it.

Containment:
  - DELEGATED AUTH      public client + device code against YOUR OWN Entra app.
                        No client secret on disk, no admin consent. The token
                        reaches only the plans this one user is a member of.
  - NARROW SCOPE        Tasks.ReadWrite only. Nothing else in the tenant.
  - NO DELETE           there is no delete tool. Tasks can be completed and
                        reopened; nothing is destroyed.
  - AMBIGUITY REFUSAL   completing by title refuses when more than one open
                        task matches, rather than guessing.
  - AUDIT LOG           every write appended to audit.log.
  - KILL SWITCH         a DISABLED file (or KAIROS_DISABLED=1) blocks everything.

Setup (one time, in Entra ID):
  1. Register an application. Single tenant is fine. No redirect URI needed.
  2. Authentication -> Settings -> "Allow public client flows" = Yes.
  3. API permissions -> Microsoft Graph -> Delegated -> Tasks.ReadWrite.
  4. Export KAIROS_CLIENT_ID and KAIROS_TENANT_ID, then run `kairos-mcp auth`
     (terminal) or call kairos_login() / kairos_login_finish() (from the MCP).
"""
import functools
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests
from mcp.server.fastmcp import FastMCP

__version__ = "0.1.0"

CONFIG_DIR = Path(os.environ.get("KAIROS_CONFIG_DIR", Path.home() / ".config" / "kairos"))
CACHE_FILE = Path(os.environ.get("KAIROS_TOKEN_CACHE", CONFIG_DIR / "token_cache.json"))
FLOW_FILE = Path(os.environ.get("KAIROS_FLOW_FILE", CONFIG_DIR / ".pending_flow.json"))
AUDIT_LOG = Path(os.environ.get("KAIROS_AUDIT_LOG", CONFIG_DIR / "audit.log"))
DISABLED_FILE = CONFIG_DIR / "DISABLED"

CLIENT_ID = os.environ.get("KAIROS_CLIENT_ID", "")
TENANT_ID = os.environ.get("KAIROS_TENANT_ID", "organizations")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["Tasks.ReadWrite"]

# Plan used when a tool is called without one. Blank = must be given, unless
# the account has exactly one plan.
DEFAULT_PLAN = os.environ.get("KAIROS_DEFAULT_PLAN", "")

GRAPH = "https://graph.microsoft.com/v1.0"
HTTP_TIMEOUT = 30
MAX_NOTES = 30_000
PLAN_CACHE_TTL = 300          # seconds; plan/bucket IDs rarely change
CALL_GAP = 0.4                # Planner throttles aggressively
MAX_RETRIES = 6

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SETUP_HINT = "KAIROS_CLIENT_ID is not set. See the setup notes in server.py / README."

mcp = FastMCP("kairos")


# ----------------------------------------------------------------- plumbing

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _disabled() -> bool:
    return DISABLED_FILE.exists() or os.environ.get("KAIROS_DISABLED") == "1"


def _ensure_dir() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass


def _audit(action: str, detail: str) -> None:
    try:
        _ensure_dir()
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{_now()}\t{action}\t{detail}\n")
    except OSError:
        pass


def _cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if CACHE_FILE.exists():
        try:
            cache.deserialize(CACHE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        _ensure_dir()
        CACHE_FILE.write_text(cache.serialize(), encoding="utf-8")
        try:
            CACHE_FILE.chmod(0o600)
        except OSError:
            pass


def _app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    # NOTE: constructing this performs authority discovery over the network.
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)


def _safe(fn):
    """Turn network failures into a plain tool result instead of a traceback."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except requests.RequestException as e:
            return f"cannot reach Microsoft ({e.__class__.__name__}) — check network/proxy and retry"
    return wrapper


REAUTH_HINT = (
    "token expired or revoked (this also happens after a password change) — "
    "run `kairos-mcp auth` in a terminal, or call kairos_login() then "
    "kairos_login_finish()"
)


def _token() -> tuple[str | None, str | None]:
    """Return (access_token, error)."""
    if not CLIENT_ID:
        return None, SETUP_HINT
    cache = _cache()
    app = _app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return None, "not signed in — run `kairos-mcp auth` or call kairos_login() first"
    try:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    except requests.RequestException as e:
        return None, f"cannot reach Microsoft sign-in ({e.__class__.__name__}) — check network/proxy"
    _save_cache(cache)
    if not result or "access_token" not in result:
        return None, REAUTH_HINT
    return result["access_token"], None


def _device_flow(app: msal.PublicClientApplication) -> tuple[dict | None, str | None]:
    try:
        flow = app.initiate_device_flow(scopes=SCOPES)
    except requests.RequestException as e:
        return None, f"cannot reach Microsoft sign-in ({e.__class__.__name__}) — check network/proxy"
    if "user_code" not in flow:
        return None, f"failed to start device flow: {json.dumps(flow)[:500]}"
    return flow, None


def _graph(method: str, path: str, token: str, **kw) -> tuple[dict, int, dict]:
    """One Graph call with 429 / 5xx retry. Returns (body, status, headers)."""
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    headers.update(kw.pop("headers", {}))
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kw)
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return {"error": {"message": f"cannot reach Graph ({e.__class__.__name__})"}}, 0, {}
        if resp.status_code == 429:
            wait = resp.headers.get("Retry-After", "5")
            time.sleep(min(int(wait) if wait.isdigit() else 5, 60))
            continue
        if resp.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        break
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"raw": resp.text[:2000]}
    return body, resp.status_code, dict(resp.headers)


def _err(what: str, code: int, body: dict) -> str:
    if code == 401:
        return f"{what} failed 401 (unauthorized) — {REAUTH_HINT}"
    if code == 403:
        return (f"{what} failed 403 (forbidden) — the Entra app needs the delegated "
                f"Tasks.ReadWrite permission, and you may need to sign in again after adding it")
    msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else None
    return f"{what} failed {code}: {msg or json.dumps(body)[:300]}"


def _paged(path: str, token: str) -> tuple[list, str | None]:
    """Follow @odata.nextLink. Returns (items, error)."""
    items: list = []
    url = path
    while url:
        body, code, _ = _graph("GET", url, token)
        if code != 200:
            return items, _err(f"GET {path}", code, body)
        items.extend(body.get("value", []))
        url = body.get("@odata.nextLink")
    return items, None


# ---------------------------------------------------------- plan resolution

_plans_cache: dict = {"at": 0.0, "plans": []}


def _load_plans(token: str, force: bool = False) -> tuple[list, str | None]:
    """[{plan_id, plan_name, buckets: [{bucket_id, bucket_name}]}] with a TTL cache."""
    if not force and _plans_cache["plans"] and time.time() - _plans_cache["at"] < PLAN_CACHE_TTL:
        return _plans_cache["plans"], None
    raw, err = _paged("/me/planner/plans?$select=id,title", token)
    if err:
        return [], err
    plans = []
    for p in raw:
        buckets, berr = _paged(f"/planner/plans/{p['id']}/buckets?$select=id,name", token)
        if berr:
            return [], berr
        plans.append({
            "plan_id": p["id"],
            "plan_name": p.get("title", ""),
            "buckets": [{"bucket_id": b["id"], "bucket_name": b.get("name", "")} for b in buckets],
        })
        time.sleep(CALL_GAP)
    _plans_cache.update(at=time.time(), plans=plans)
    return plans, None


def _invalidate_plans() -> None:
    _plans_cache["at"] = 0.0


def _resolve_plan(token: str, plan: str | None) -> tuple[dict | None, str | None]:
    """Find a plan by name (case-insensitive) or ID. Falls back to
    KAIROS_DEFAULT_PLAN, then to the only plan if there is exactly one."""
    plans, err = _load_plans(token)
    if err:
        return None, err
    if not plans:
        return None, "no Planner plans found for this account"
    want = (plan or DEFAULT_PLAN or "").strip()
    if not want:
        if len(plans) == 1:
            return plans[0], None
        names = ", ".join(repr(p["plan_name"]) for p in plans)
        return None, (f"plan is required (you have {len(plans)}: {names}). "
                      "Pass plan=..., or set KAIROS_DEFAULT_PLAN.")
    low = want.lower()
    for p in plans:
        if p["plan_name"].lower() == low or p["plan_id"] == want:
            return p, None
    partial = [p for p in plans if low in p["plan_name"].lower()]
    if len(partial) == 1:
        return partial[0], None
    names = ", ".join(repr(p["plan_name"]) for p in plans)
    return None, f"no plan named {want!r}. Plans: {names}"


def _resolve_bucket(p: dict, bucket: str | None) -> tuple[dict | None, str | None]:
    """Find a bucket in a resolved plan by name (case-insensitive) or ID.
    Omitted -> first bucket."""
    buckets = p["buckets"]
    if not buckets:
        return None, f"plan {p['plan_name']!r} has no buckets — create one with create_planner_bucket"
    want = (bucket or "").strip()
    if not want:
        return buckets[0], None
    low = want.lower()
    for b in buckets:
        if b["bucket_name"].lower() == low or b["bucket_id"] == want:
            return b, None
    partial = [b for b in buckets if low in b["bucket_name"].lower()]
    if len(partial) == 1:
        return partial[0], None
    names = ", ".join(repr(b["bucket_name"]) for b in buckets)
    return None, f"no bucket named {want!r} in {p['plan_name']!r}. Buckets: {names}"


def _task_url(plan_id: str, task_id: str) -> str:
    return f"https://planner.cloud.microsoft/webui/plan/{plan_id}/view/board/task/{task_id}"


def _due_iso(due_on: str) -> str:
    # Noon UTC keeps the calendar date stable in every timezone Planner renders in.
    return f"{due_on}T12:00:00Z"


def _due_date(dt: str | None) -> str | None:
    return dt[:10] if dt else None


def _set_notes(token: str, task_id: str, notes: str) -> str | None:
    """Notes live on a child `details` entity behind an ETag. It can lag a
    freshly created task by a moment, so 404 is retried."""
    etag = None
    for attempt in range(5):
        d, code, _ = _graph("GET", f"/planner/tasks/{task_id}/details", token)
        if code == 200:
            etag = d.get("@odata.etag")
            break
        if code != 404:
            return _err("read task details", code, d)
        time.sleep(1.0 + attempt)
    if not etag:
        return "task details not available yet — notes not written (retry with update later)"
    out, code, _ = _graph("PATCH", f"/planner/tasks/{task_id}/details", token,
                          headers={"If-Match": etag},
                          json={"description": notes, "previewType": "description"})
    if code not in (200, 204):
        return _err("write task notes", code, out)
    return None


# -------------------------------------------------------------------- tools

@mcp.tool()
@_safe
def kairos_login() -> str:
    """Start a device-code sign-in to Microsoft 365. Returns a URL and a code
    for the human to enter in a browser; then call kairos_login_finish() to
    complete. Only needed once, or after the refresh token lapses (for example
    after a password change)."""
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    if not CLIENT_ID:
        return SETUP_HINT
    cache = _cache()
    app = _app(cache)
    flow, err = _device_flow(app)
    if err:
        return err
    _ensure_dir()
    FLOW_FILE.write_text(json.dumps(flow))
    try:
        FLOW_FILE.chmod(0o600)
    except OSError:
        pass
    return f"{flow.get('message', '')}\n\nAfter entering the code, call kairos_login_finish() to complete sign-in."


@mcp.tool()
@_safe
def kairos_login_finish() -> str:
    """Complete a device-code sign-in started with kairos_login(). Call after
    entering the code in the browser. Waits up to ~60s; if the code has not
    been entered yet, it says so and can simply be called again."""
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    if not CLIENT_ID:
        return SETUP_HINT
    if not FLOW_FILE.exists():
        return "no pending sign-in — call kairos_login() first"
    try:
        flow = json.loads(FLOW_FILE.read_text())
    except ValueError:
        FLOW_FILE.unlink(missing_ok=True)
        return "pending sign-in state was unreadable — call kairos_login() again"
    if time.time() > flow.get("expires_at", 0):
        FLOW_FILE.unlink(missing_ok=True)
        return "the device code expired — call kairos_login() to get a new one"
    flow["expires_at"] = min(flow.get("expires_at", 0), int(time.time()) + 60)
    cache = _cache()
    app = _app(cache)
    try:
        result = app.acquire_token_by_device_flow(flow)
    except requests.RequestException as e:
        return f"cannot reach Microsoft sign-in ({e.__class__.__name__}) — check network/proxy, then call kairos_login_finish() again"
    _save_cache(cache)
    if "access_token" in result:
        FLOW_FILE.unlink(missing_ok=True)
        who = result.get("id_token_claims", {}).get("preferred_username", "unknown")
        _audit("login", who)
        _invalidate_plans()
        return f"signed in as {who} (scopes: {' '.join(SCOPES)})"
    if result.get("error") == "authorization_pending":
        return "code not entered yet — finish it in the browser, then call kairos_login_finish() again"
    FLOW_FILE.unlink(missing_ok=True)
    return f"sign-in failed: {result.get('error_description', json.dumps(result))[:500]}"


@mcp.tool()
@_safe
def kairos_auth_status() -> str:
    """Report whether kairos is signed in, as whom, whether Graph is reachable,
    and how many plans are visible."""
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    if not CLIENT_ID:
        return SETUP_HINT
    cache = _cache()
    app = _app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return "not signed in — run `kairos-mcp auth` or call kairos_login() first"
    token, err = _token()
    if err:
        return err
    body, code, _ = _graph("GET", "/me/planner/plans?$select=id", token)
    return json.dumps({
        "signed_in_as": accounts[0].get("username"),
        "scopes": SCOPES,
        "graph_ok": code == 200,
        "plans_visible": len(body.get("value", [])) if code == 200 else None,
        "default_plan": DEFAULT_PLAN or "(none — plan must be given unless you have exactly one)",
        "token_cache": str(CACHE_FILE),
        "version": __version__,
    }, indent=2)


@mcp.tool()
@_safe
def list_planner_plans(refresh: bool = False) -> str:
    """List the Planner plans this account can see, each with its buckets and
    IDs. Results are cached for a few minutes; pass refresh=true to force a
    re-read (for example after someone added a bucket in the Planner UI)."""
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    token, err = _token()
    if err:
        return err
    plans, err = _load_plans(token, force=refresh)
    if err:
        return err
    return json.dumps({"default_plan": DEFAULT_PLAN or None, "plans": plans}, indent=2)


@mcp.tool()
@_safe
def create_planner_task(
    title: str,
    notes: str = "",
    plan: str = "",
    bucket: str = "",
    due_on: str = "",
) -> str:
    """Create a task in Microsoft Planner. This is a real write: the task
    exists the moment this returns.

    Args:
        title: Short, action-oriented task title (required).
        notes: Longer context for the task body (Planner "Notes").
        plan: Plan name (case-insensitive) or ID. Empty uses KAIROS_DEFAULT_PLAN,
              or the only plan if the account has exactly one.
        bucket: Bucket within the plan, by name or ID. Empty uses the plan's
                first bucket. Call list_planner_plans to see what exists.
        due_on: Due date as YYYY-MM-DD, or empty for none.
    """
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    title = (title or "").strip()
    if not title:
        return "title is required"
    if len(title) > 255:
        return "title too long (max 255 characters)"
    if len(notes) > MAX_NOTES:
        return f"notes too long ({len(notes)} chars, max {MAX_NOTES})"
    if due_on and not DATE_RE.match(due_on):
        return "due_on must be YYYY-MM-DD"
    token, err = _token()
    if err:
        return err
    p, err = _resolve_plan(token, plan)
    if err:
        return err
    b, err = _resolve_bucket(p, bucket)
    if err:
        return err

    payload = {"planId": p["plan_id"], "bucketId": b["bucket_id"], "title": title, "orderHint": " !"}
    if due_on:
        payload["dueDateTime"] = _due_iso(due_on)
    out, code, _ = _graph("POST", "/planner/tasks", token, json=payload)
    if code not in (200, 201):
        return _err("create task", code, out)
    task_id = out.get("id")
    _audit("create_task", f"id={task_id} plan={p['plan_name']!r} bucket={b['bucket_name']!r} title={title!r}")

    warning = None
    if notes.strip():
        time.sleep(CALL_GAP)
        warning = _set_notes(token, task_id, notes)

    result = {
        "status": "task created",
        "task_id": task_id,
        "title": title,
        "plan": p["plan_name"],
        "bucket": b["bucket_name"],
        "due_on": due_on or None,
        "url": _task_url(p["plan_id"], task_id),
    }
    if warning:
        result["warning"] = warning
    return json.dumps(result, indent=2)


@mcp.tool()
@_safe
def create_planner_bucket(bucket_name: str, plan: str = "") -> str:
    """Create a bucket (board column) in a Planner plan. Idempotent: if a bucket
    with that name already exists (case-insensitive) its ID is returned and
    nothing is created.

    Args:
        bucket_name: Name of the bucket.
        plan: Plan name or ID. Empty uses KAIROS_DEFAULT_PLAN / the only plan.
    """
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    bucket_name = (bucket_name or "").strip()
    if not bucket_name:
        return "bucket_name is required"
    token, err = _token()
    if err:
        return err
    # Always re-read before creating, so we never duplicate a bucket that was
    # added in the UI since the cache was filled.
    _invalidate_plans()
    p, err = _resolve_plan(token, plan)
    if err:
        return err
    low = bucket_name.lower()
    for b in p["buckets"]:
        if b["bucket_name"].lower() == low:
            return json.dumps({
                "status": "bucket already exists — nothing created",
                "bucket_id": b["bucket_id"],
                "bucket_name": b["bucket_name"],
                "plan": p["plan_name"],
            }, indent=2)
    out, code, _ = _graph("POST", "/planner/buckets", token,
                          json={"name": bucket_name, "planId": p["plan_id"], "orderHint": " !"})
    if code not in (200, 201):
        return _err("create bucket", code, out)
    _invalidate_plans()
    _audit("create_bucket", f"id={out.get('id')} plan={p['plan_name']!r} name={bucket_name!r}")
    return json.dumps({
        "status": "bucket created",
        "bucket_id": out.get("id"),
        "bucket_name": bucket_name,
        "plan": p["plan_name"],
    }, indent=2)


def _list_tasks(token: str, p: dict, bucket: str | None, include_completed: bool) -> tuple[list, str | None]:
    b = None
    if bucket:
        b, err = _resolve_bucket(p, bucket)
        if err:
            return [], err
    raw, err = _paged(
        f"/planner/plans/{p['plan_id']}/tasks?$select=id,title,bucketId,dueDateTime,percentComplete,createdDateTime",
        token,
    )
    if err:
        return [], err
    names = {x["bucket_id"]: x["bucket_name"] for x in p["buckets"]}
    out = []
    for t in raw:
        if b and t.get("bucketId") != b["bucket_id"]:
            continue
        pct = t.get("percentComplete", 0)
        if not include_completed and pct == 100:
            continue
        out.append({
            "task_id": t["id"],
            "title": t.get("title", ""),
            "plan": p["plan_name"],
            "bucket": names.get(t.get("bucketId"), t.get("bucketId")),
            "due_on": _due_date(t.get("dueDateTime")),
            "percent_complete": pct,
            "url": _task_url(p["plan_id"], t["id"]),
        })
    return out, None


@mcp.tool()
@_safe
def list_planner_tasks(plan: str = "", bucket: str = "", include_completed: bool = False) -> str:
    """List Planner tasks with their IDs, so a task can be referenced or completed.

    Args:
        plan: Plan name or ID. Empty uses KAIROS_DEFAULT_PLAN / the only plan.
              Pass "*" to list every plan the account can see.
        bucket: Optional bucket name to filter by (ignored with plan="*").
        include_completed: Include tasks already at 100%. Defaults to open only.
    """
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    token, err = _token()
    if err:
        return err
    if plan.strip() == "*":
        plans, err = _load_plans(token)
        if err:
            return err
        tasks: list = []
        for p in plans:
            got, err = _list_tasks(token, p, None, include_completed)
            if err:
                return err
            tasks.extend(got)
            time.sleep(CALL_GAP)
        return json.dumps({"count": len(tasks), "tasks": tasks}, indent=2)
    p, err = _resolve_plan(token, plan)
    if err:
        return err
    tasks, err = _list_tasks(token, p, bucket, include_completed)
    if err:
        return err
    return json.dumps({"plan": p["plan_name"], "count": len(tasks), "tasks": tasks}, indent=2)


@mcp.tool()
@_safe
def complete_planner_task(task_id: str = "", title: str = "", plan: str = "",
                          reopen: bool = False) -> str:
    """Mark a Planner task complete (or reopen it). This is a real write.

    Give either task_id (exact, preferred) or title. A title is matched against
    OPEN tasks only (COMPLETED tasks when reopen=true); if it matches more than
    one, this refuses and lists the candidates rather than guessing.

    Args:
        task_id: Exact Planner task ID. Preferred when known.
        title: Task title or a distinctive part of it. Used only if task_id is empty.
        plan: Plan to search when matching by title. Empty uses the default plan.
        reopen: Set true to undo a completion (sets 0%) instead.
    """
    if _disabled():
        return "kairos is DISABLED (kill switch engaged)"
    task_id = (task_id or "").strip()
    title = (title or "").strip()
    if not task_id and not title:
        return "give either task_id or title"
    token, err = _token()
    if err:
        return err

    if not task_id:
        p, err = _resolve_plan(token, plan)
        if err:
            return err
        tasks, err = _list_tasks(token, p, None, include_completed=True)
        if err:
            return err
        pool = [t for t in tasks if (t["percent_complete"] == 100) == reopen]
        needle = title.lower()
        exact = [t for t in pool if t["title"].lower() == needle]
        matches = exact or [t for t in pool if needle in t["title"].lower()]
        state = "completed" if reopen else "open"
        if not matches:
            return f"no {state} task matching {title!r} in {p['plan_name']!r}"
        if len(matches) > 1:
            listing = "\n".join(f"  {t['task_id']}  [{t['bucket']}]  {t['title']}" for t in matches[:10])
            return (f"found {len(matches)} {state} tasks matching {title!r} — please specify. "
                    f"Re-call with an exact task_id:\n{listing}")
        task_id = matches[0]["task_id"]
        title = matches[0]["title"]

    cur, code, _ = _graph("GET", f"/planner/tasks/{task_id}?$select=id,title,percentComplete,planId", token)
    if code == 404:
        return f"no task with id {task_id!r}"
    if code != 200:
        return _err("read task", code, cur)
    etag = cur.get("@odata.etag")
    title = cur.get("title", title)
    target = 0 if reopen else 100
    if cur.get("percentComplete") == target:
        return json.dumps({
            "status": "already " + ("open" if reopen else "complete") + " — nothing changed",
            "task_id": task_id, "title": title, "completed": not reopen,
        }, indent=2)
    out, code, _ = _graph("PATCH", f"/planner/tasks/{task_id}", token,
                          headers={"If-Match": etag}, json={"percentComplete": target})
    if code not in (200, 204):
        return _err("reopen task" if reopen else "complete task", code, out)
    _audit("reopen" if reopen else "complete", f"id={task_id} title={title!r}")
    return json.dumps({
        "status": "reopened" if reopen else "completed",
        "task_id": task_id,
        "title": title,
        "completed": not reopen,
        "url": _task_url(cur.get("planId", ""), task_id),
    }, indent=2)


# ---------------------------------------------------------------------- CLI

def _cli_auth() -> int:
    """Interactive device-code sign-in from a terminal. Everything goes to
    stderr so a stdio MCP transport on stdout is never polluted."""
    if not CLIENT_ID:
        print(SETUP_HINT, file=sys.stderr)
        return 2
    cache = _cache()
    app = _app(cache)
    flow, err = _device_flow(app)
    if err:
        print(err, file=sys.stderr)
        return 1
    print(flow.get("message", ""), file=sys.stderr, flush=True)
    try:
        result = app.acquire_token_by_device_flow(flow)
    except requests.RequestException as e:
        print(f"cannot reach Microsoft sign-in ({e.__class__.__name__})", file=sys.stderr)
        return 1
    _save_cache(cache)
    if "access_token" not in result:
        print(f"sign-in failed: {result.get('error_description', json.dumps(result))[:500]}", file=sys.stderr)
        return 1
    who = result.get("id_token_claims", {}).get("preferred_username", "unknown")
    _audit("login", who)
    print(f"signed in as {who}. Token cache: {CACHE_FILE}", file=sys.stderr)
    return 0


def _cli_status() -> int:
    print(kairos_auth_status(), file=sys.stderr)
    return 0


def _cli_logout() -> int:
    removed = False
    for f in (CACHE_FILE, FLOW_FILE):
        if f.exists():
            f.unlink()
            removed = True
    _invalidate_plans()
    print("signed out (token cache removed)" if removed else "nothing to remove", file=sys.stderr)
    return 0


USAGE = """usage: kairos-mcp [auth | status | logout | --version]

  (no args)   run the MCP server on stdio (what Claude Desktop launches)
  auth        interactive device-code sign-in; writes the token cache
  status      show who is signed in and whether Graph is reachable
  logout      remove the token cache
"""


def main() -> None:
    """Console-script entry point."""
    args = sys.argv[1:]
    if not args:
        mcp.run()
        return
    cmd = args[0]
    if cmd in ("-V", "--version"):
        print(f"kairos-mcp {__version__}")
        sys.exit(0)
    if cmd == "auth":
        sys.exit(_cli_auth())
    if cmd == "status":
        sys.exit(_cli_status())
    if cmd == "logout":
        sys.exit(_cli_logout())
    print(USAGE, file=sys.stderr)
    sys.exit(2 if cmd not in ("-h", "--help") else 0)


if __name__ == "__main__":
    main()
