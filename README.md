# kairos

**A Microsoft Planner MCP server that writes tasks.**

An agent creates tasks and buckets in your Planner plans, lists what is there,
and marks tasks complete. Sign-in is delegated device-code against your own
Entra app with the single Graph scope `Tasks.ReadWrite`; the token reaches only
the plans you are a member of and nothing else in the tenant.

kairos is the sibling of [iris](https://github.com/SuperAngryMonkey/iris), the
mail server that cannot send. They are deliberately separate. iris's value is an
*absent* capability; a Planner writer's value is the write itself. Creating a
task creates a task. Completing one completes one. kairos does not pretend
otherwise, so read [what it can do](#what-it-can-and-cannot-do) before you
install it.

---

## Install

```bash
uvx kairos-mcp        # run without installing
pip install kairos-mcp
```

**Python 3.10 or newer.** macOS ships Python 3.9, which is too old — `mcp`
requires 3.10+. Use [`uv`](https://docs.astral.sh/uv/) (it comes with `uvx` and
manages its own Python), or install a current Python with Homebrew
(`brew install python@3.12`). The system `python3` on macOS will not work.

## Setup

**You must register your own Entra application.** There is no shared app
registration and no hosted service — kairos talks directly from your machine to
your tenant. A shared app would mean trusting someone else's client ID with
write access to your plans.

1. Entra admin centre → **App registrations** → **New registration**. Single
   tenant is fine. No redirect URI needed.
2. **Authentication** → Settings → enable **Allow public client flows**. Device
   code sign-in needs this. No client secret is used anywhere.
3. **API permissions** → Microsoft Graph → **Delegated** → add
   **`Tasks.ReadWrite`** ("Create, read, update and delete user's tasks and
   projects"). Nothing else.
4. Copy the **Application (client) ID** and **Directory (tenant) ID**. Neither
   is a secret.

Then add kairos to your MCP client:

```json
{
  "mcpServers": {
    "kairos": {
      "command": "uvx",
      "args": ["kairos-mcp"],
      "env": {
        "KAIROS_CLIENT_ID": "<application (client) id>",
        "KAIROS_TENANT_ID": "<directory (tenant) id>",
        "KAIROS_DEFAULT_PLAN": "Development"
      }
    }
  }
}
```

Sign in once, either from a terminal:

```bash
KAIROS_CLIENT_ID=... KAIROS_TENANT_ID=... uvx kairos-mcp auth
```

or from inside the MCP client: call `kairos_login`, open the URL, enter the
code, then call `kairos_login_finish`. Either way the token cache lands in
`~/.config/kairos/token_cache.json`, mode 600, and refreshes silently from then
on.

`KAIROS_DEFAULT_PLAN` is optional. Without it, every call must name a plan —
unless your account can see exactly one, in which case that one is used.

## Clients

kairos is a local **stdio** MCP server: your MCP client launches it as a child
process on the same machine. It works with any client that supports local stdio
servers — Claude Desktop, Cursor, the Grok CLI and others. It does not work with
clients that only accept remote MCP connectors over HTTP.

## Tools

| Tool | What it does |
|---|---|
| `kairos_login` | Starts device-code sign-in, returns a URL and a code |
| `kairos_login_finish` | Completes sign-in; safe to call repeatedly while you type the code |
| `kairos_auth_status` | Who is signed in, whether Graph is reachable, how many plans are visible |
| `list_planner_plans` | Every plan you can see, with its buckets and IDs (cached a few minutes; `refresh=true` to re-read) |
| `create_planner_task` | Creates a task: `title`, optional `notes`, `plan`, `bucket`, `due_on` (YYYY-MM-DD). Returns the task ID and a URL |
| `create_planner_bucket` | Creates a bucket in a plan. Idempotent — an existing bucket of that name is returned, not duplicated |
| `list_planner_tasks` | Tasks in a plan (or `plan="*"` for all), optionally one bucket, open only by default |
| `complete_planner_task` | Marks a task 100% by `task_id`, or by `title` when the match is unique. `reopen=true` undoes it |

Plans and buckets are matched by name, case-insensitively, or by ID. A partial
name works when it is unambiguous. Omit `bucket` and the plan's first bucket is
used.

### Completing by title refuses ambiguity

`complete_planner_task(title="deploy")` searches open tasks in the plan. One
match: it completes it. Two or more: it stops and lists them with IDs so you can
re-call with the exact `task_id`. It never picks one for you.

## CLI

```
kairos-mcp            run the MCP server on stdio (what your client launches)
kairos-mcp auth       interactive device-code sign-in
kairos-mcp status     who is signed in, is Graph reachable
kairos-mcp logout     remove the token cache
```

## Re-authenticating

A refresh token stops working after a password change, an admin revocation, or
long disuse. Every tool then returns a message that says so and how to fix it:
`kairos-mcp auth` in a terminal, or `kairos_login` → `kairos_login_finish` from
the client. Nothing else needs to change.

## What it can and cannot do

**Can:** create tasks and buckets in any plan you are a member of; set a task's
notes and due date at creation; list plans, buckets and tasks; mark a task
complete or reopen it.

**Cannot:** delete anything — there is no delete tool, and the design keeps it
that way. Assign people. Set priority, labels, checklists or attachments. Edit an
existing task's title, notes or due date. See plans you are not a member of.

`Tasks.ReadWrite` is the narrowest Graph scope that permits creating a Planner
task. It also permits reading and updating every task in every plan you belong
to, and in principle deleting them. kairos does not expose delete, but the
*token* could — the boundary is this code, not Microsoft's consent screen. If
that distinction matters to you, it should; see [SECURITY.md](SECURITY.md).

## Other controls

- **Kill switch** — create `~/.config/kairos/DISABLED`, or set
  `KAIROS_DISABLED=1`, and every tool refuses.
- **Audit log** — every write (login, create, complete, reopen) is appended to
  `~/.config/kairos/audit.log`.
- **`KAIROS_CONFIG_DIR`** moves the cache, log and kill-switch file elsewhere;
  `KAIROS_TOKEN_CACHE` and `KAIROS_AUDIT_LOG` override individual paths.

## Graph notes, for the curious

A task's notes are not on the task; they live on a child `details` entity that
Graph guards with an ETag. kairos creates the task, reads the details ETag, then
PATCHes with `If-Match`. Completion is the same dance on the task itself.
Planner throttles hard, so every call retries on 429 with `Retry-After` and
there is a short pause between chained calls. New tasks and buckets are pinned
to the top of their column (`orderHint: " !"`).

## License

MIT — see [LICENSE](LICENSE).

<!-- mcp-name: io.github.SuperAngryMonkey/kairos -->
