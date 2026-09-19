# Changelog

## 0.1.0 — 2026-09-19

Initial release.

- Five Planner tools: `create_planner_task`, `create_planner_bucket`,
  `list_planner_plans`, `list_planner_tasks`, `complete_planner_task`
  (with `reopen`).
- Three auth tools: `kairos_login`, `kairos_login_finish`, `kairos_auth_status`.
- CLI: `kairos-mcp auth | status | logout`.
- Delegated device-code sign-in against your own Entra app; `Tasks.ReadWrite`
  only. Token cache in `~/.config/kairos/`, mode 600.
- Idempotent bucket creation; ambiguity refusal when completing by title;
  no delete tool.
- 429/5xx retry with `Retry-After`; ETag handling for task details and
  completion.
