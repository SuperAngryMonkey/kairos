# Security

kairos writes to Microsoft Planner. This document says exactly what the token
it holds can do, what the code chooses not to do with it, and which of those two
things you are actually relying on.

## The scope

kairos requests one delegated Graph permission: **`Tasks.ReadWrite`**.

Per Microsoft, that scope lets the signed-in user's token create, read, update
and **delete** tasks and plans the user has access to. It reaches every plan the
user is a member of — not just the one named in `KAIROS_DEFAULT_PLAN`. It does
not reach mail, files, Teams, the directory, or plans the user is not a member
of.

There is no narrower Graph scope that still permits creating a Planner task.

## What the code does with it

| Action | Exposed as a tool? |
|---|---|
| Read plans, buckets, tasks | yes |
| Create a task, create a bucket | yes |
| Set percentComplete to 100 or 0 | yes |
| Set notes on a newly created task | yes (creation only) |
| Edit an existing task's title, notes, due date, assignments | **no** |
| Delete a task, bucket or plan | **no** |

The "no" rows are enforced by this program. They are guardrails in code: a bug
here, or a compromised copy of this package, could do those things with the same
token. That is different from iris, where "cannot send" is enforced by Microsoft
against the consent you granted. **With kairos the boundary is the code, not the
consent screen.** Judge it accordingly.

## What limits the blast radius anyway

- **Delegated, not application.** The token is the signed-in user's. It cannot
  touch plans that user cannot see, and admins can revoke it like any user
  session.
- **Public client, no secret.** There is nothing on disk that grants access by
  itself. The token cache (`~/.config/kairos/token_cache.json`, mode 600) holds
  a refresh token bound to your app registration and your tenant; treat it like
  a session cookie.
- **Nothing is destroyed.** Completing a task is reversible (`reopen=true`).
  Bucket creation is idempotent. There is no path in this code that removes
  data.
- **Ambiguity refusal.** Completing by title stops and asks when more than one
  task matches. It never guesses.
- **Kill switch.** A `DISABLED` file or `KAIROS_DISABLED=1` refuses every call.
- **Audit log.** Every write is appended to `audit.log` with a UTC timestamp.

## What you should do

- Register a **dedicated** Entra app for kairos with `Tasks.ReadWrite` and
  nothing else. Do not reuse an app that carries mail or files scopes; a broad
  app registration widens what a leaked cache can do.
- If you run kairos on a shared or exposed machine, the token cache is the
  thing to protect.
- If you need an agent that can *only read* Planner, kairos is the wrong tool —
  it would still hold a write-capable token. Register with `Tasks.Read` and use
  something else.

## Verifying the claims

```bash
grep -n "SCOPES = " server.py      # one scope, Tasks.ReadWrite
grep -n '"DELETE"' server.py        # no matches — no delete call exists
grep -n '@mcp.tool' server.py       # eight tools, listed in the README
```

## Reporting

Open an issue on GitHub, or email the address in `pyproject.toml`.
