# Outreach CRM: backend (developer notes)

For how to *use* the CRM, see `../HANDBOOK.md`. This file is for whoever changes the code.

## Run by hand

```
cd crm && python app.py                   # API on http://127.0.0.1:5050 (creates crm.db if missing)
cd crm-web && npm install && npm run build && npm run start   # UI on http://localhost:3100
cd crm-web && npm run dev                 # UI with hot reload while editing
```

The UI proxies `/api/*` to the Flask API (`crm-web/next.config.ts`). The old single-page UI is still
served at http://127.0.0.1:5050 as a fallback.

With `DATABASE_URL` or `NEON_DATABASE_URL` in `crm/.env`, the API and the MCP server both use that
Postgres database. SQLite is only the fallback when neither variable is set.

## MCP

`mcp_server.py` exposes lead tools over stdio for Cursor. Project config is `.cursor/mcp.json`.
Set `MCP_USER_EMAIL` in `crm/.env` when more than one user exists; that account owns new leads and
is the only account that can edit them. The MCP process opens its own connection, so the API does
not need a restart.

## Files

| File | What it is |
|---|---|
| `core.py` | Schema, additive migrations, guard rails (`transition_stage`, `detect_links`), scoring, settings defaults |
| `app.py` | Flask JSON API (leads, stages, queue, metrics, bulk finalize, settings) |
| `mcp_server.py` | Local MCP tools (`list_leads`, `get_lead`, `create_lead`, `update_lead`, `set_stage`, `delete_lead`). Uses Neon when `DATABASE_URL` is set. |
| `autopilot.py` | AI Autopilot: the prompt (`RULES`), result schema, Claude/Codex runners, stream parsers, validation |
| `gmail.py` | Google OAuth (PKCE) + Gmail **drafts** only (`gmail.compose` scope; no send call) |
| `seed.py` | Imports a CSV into `crm.db` (idempotent, keyed on `clutch_id`) |
| `tests/` | `python -m unittest discover -s tests` |
| `.env.example` | Google OAuth client settings. Copy it to `.env`, which stays private. |
| `crm.db` | SQLite database used when `DATABASE_URL` / `NEON_DATABASE_URL` is not set. Not shipped. |

## How Autopilot works

The API runs `claude -p` (or `codex exec`) headless, one session per lead, using the CLI's own login.
Each session gets **web read tools only**. It reads the agency's site and returns JSON that matches the
schema. The server then checks it, and never trusts the model:

- Links in the subject/body are stripped and the lead is flagged.
- An email whose domain isn't the agency's domain is dropped.
- A decision maker is kept only if verified.
- Every claim goes into Evidence with its URL + confidence.
- The lead moves to **Drafted** only if it qualifies *and* has a route + subject + body. Otherwise it gets
  a hold/exclude suggestion for the human.
- The AI can never move a lead past Drafted. `core.transition_stage(..., actor="ai")` returns 403.
- A run is refused (400) until Settings has a sender name, a location and a proof library with no
  `[placeholders]`. See `settings_problem()`.

## Env overrides

| Variable | Effect |
|---|---|
| `CRM_DB` | Database path |
| `CRM_PORT` | API port |
| `CRM_API` | API URL for the UI, read at build time |
| `GOOGLE_*`, `CRM_UI_URL` | In `crm/.env` |

## Guard rails worth keeping

- Finalize is blocked while any URL / domain / email / handle remains in the copy.
- Contacted requires confirming it was actually sent (date + channel).
- Do not contact requires a reason and is never deleted.
- Follow-ups: first at +5 business days, final at +7 calendar days, at most two.
- Every change, including every AI write, goes into the lead's activity log.
- The Gmail tests fail if a send call is added to `gmail.py`.
