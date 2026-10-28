"""Local MCP server for Outreach CRM.

Cursor (or any other MCP client on this machine) can list, create, and edit
leads through the same database the web app uses. When crm/.env sets
DATABASE_URL or NEON_DATABASE_URL, that is Neon. Otherwise it is the local
SQLite file crm.db.

Writes are attributed to a CRM user:

  MCP_USER_EMAIL=you@example.com

If that is unset and the database has exactly one user, that user is used.
Only the lead owner can update, move, or delete a lead. Stage changes go
through the same rules as the Leads page.

Run (stdio, which is what Cursor launches):

  ..\\.venv\\Scripts\\python.exe mcp_server.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from core import (
    ALL_STAGES,
    EDITABLE_FIELDS,
    STAGE_LABELS,
    apply_lead_updates,
    connect,
    create_lead as insert_lead,
    init_db,
    lead_to_dict,
    transition_stage,
)

_EDITABLE = ", ".join(EDITABLE_FIELDS)
_STAGES = ", ".join(f"{s} ({STAGE_LABELS.get(s, s)})" for s in ALL_STAGES)

mcp = MCPServer(
    "outreach-crm",
    title="Outreach CRM",
    instructions=(
        "Tools for the Outreach CRM database used by the web app. "
        "Creating a lead puts it in To research. Updating a lead changes "
        "profile, contact, draft, score, and campaign fields. "
        "Moving a lead uses the same stage rules as the app: Finalized needs "
        "a link-free subject and message body, Contacted needs confirm_sent, "
        "and Do not contact needs suppression_reason. "
        "Only the lead owner can change or delete a lead."
    ),
)


def _json(payload):
    return json.dumps(payload, default=str)


def _safe_error(exc):
    message = f"{type(exc).__name__}: {exc}"
    if "://" in message or "password" in message.lower():
        message = f"{type(exc).__name__}: database operation failed"
    print(message, file=sys.stderr)
    return {"error": message}


def _call(fn):
    db = connect()
    try:
        user, error = _resolve_user(db)
        if error:
            return error
        return fn(db, user)
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        return _safe_error(exc)
    finally:
        db.close()


def _resolve_user(db):
    email = (os.environ.get("MCP_USER_EMAIL") or "").strip().lower()
    if email:
        user = db.execute(
            "SELECT id, name, email FROM users WHERE lower(email)=?",
            (email,),
        ).fetchone()
        if not user:
            return None, {
                "error": (
                    f"No CRM user with email {email}. "
                    "Register in the app, or set MCP_USER_EMAIL in crm/.env to an existing account."
                )
            }
        return user, None
    users = db.execute("SELECT id, name, email FROM users ORDER BY id").fetchall()
    if len(users) == 1:
        return users[0], None
    if not users:
        return None, {
            "error": "No CRM users yet. Register in the app, then set MCP_USER_EMAIL in crm/.env if there is more than one account."
        }
    return None, {
        "error": "More than one CRM user. Set MCP_USER_EMAIL in crm/.env to the account this MCP should act as."
    }


def _owner_error(db, lead_id, user_id):
    lead = db.execute(
        "SELECT id, company_name, owner_user_id FROM leads WHERE id=?",
        (lead_id,),
    ).fetchone()
    if not lead:
        return None, {"error": "not found", "id": lead_id}
    owner_id = lead["owner_user_id"]
    if owner_id is not None and owner_id != user_id:
        return None, {"error": "Only the lead owner can edit or delete this lead.", "id": lead_id}
    return lead, None


def _commit(db, payload, code):
    if code == 200:
        db.commit()
    else:
        db.rollback()
    return payload


def _summary(row):
    lead = lead_to_dict(row)
    return {
        "id": lead.get("id"),
        "company_name": lead.get("company_name"),
        "website": lead.get("website"),
        "geography": lead.get("geography"),
        "stage": lead.get("stage"),
        "stage_label": lead.get("stage_label"),
        "campaign": lead.get("campaign"),
        "source_type": lead.get("source_type"),
        "review_count": lead.get("review_count"),
        "contact_email": lead.get("contact_email"),
        "owner_user_id": lead.get("owner_user_id"),
        "updated_at": lead.get("updated_at"),
    }


def _fields(value):
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("fields must be a JSON object")
        value = parsed
    if not isinstance(value, dict):
        raise ValueError("fields must be an object")
    clean = {}
    for key, item in value.items():
        clean[key] = int(item) if isinstance(item, bool) else item
    return clean


@mcp.tool(
    annotations=ToolAnnotations(title="List leads", read_only_hint=True, open_world_hint=False)
)
def list_leads(q: str = "", stage: str = "", campaign: str = "", limit: int = 25, offset: int = 0) -> str:
    """Search leads. q matches company, website, location, or services. stage is a pipeline key such as to_research or drafted. Results are newest-updated first."""

    def run(db, _user):
        if stage and stage not in ALL_STAGES:
            return {"error": "invalid stage", "stages": list(ALL_STAGES)}
        where = []
        params = []
        if stage:
            where.append("stage = ?")
            params.append(stage)
        if campaign:
            where.append("campaign = ?")
            params.append(campaign)
        if q.strip():
            where.append("(company_name LIKE ? OR website LIKE ? OR geography LIKE ? OR services LIKE ?)")
            params += [f"%{q.strip()}%"] * 4
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        capped = min(max(int(limit or 25), 1), 100)
        start = max(int(offset or 0), 0)
        total = db.execute("SELECT COUNT(*) n FROM leads" + where_sql, params).fetchone()["n"]
        rows = db.execute(
            "SELECT * FROM leads" + where_sql + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [capped, start],
        ).fetchall()
        return {"leads": [_summary(r) for r in rows], "total": total, "limit": capped, "offset": start}

    return _json(_call(run))


@mcp.tool(
    annotations=ToolAnnotations(title="Get lead", read_only_hint=True, open_world_hint=False)
)
def get_lead(lead_id: int) -> str:
    """Return one lead, including draft, contact, score, and the latest events."""

    def run(db, _user):
        row = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            return {"error": "not found", "id": lead_id}
        lead = lead_to_dict(row)
        lead["events"] = [
            dict(e)
            for e in db.execute(
                "SELECT id, ts, type, detail FROM events WHERE lead_id=? ORDER BY ts DESC, id DESC LIMIT 20",
                (lead_id,),
            )
        ]
        return lead

    return _json(_call(run))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create lead",
        read_only_hint=False,
        destructive_hint=False,
        open_world_hint=False,
    )
)
def create_lead(
    company_name: str,
    website: str = "",
    geography: str = "",
    source_type: str = "mcp",
    source_url: str = "",
    campaign: str = "",
    review_count: int = 0,
    force: bool = False,
) -> str:
    """Add a lead in To research. A matching website or company name is rejected unless force is true. source_type defaults to mcp."""

    def run(db, user):
        payload, code = insert_lead(db, {
            "company_name": company_name,
            "website": website,
            "geography": geography,
            "source_type": source_type,
            "source_url": source_url,
            "campaign": campaign,
            "review_count": review_count,
            "force": force,
        }, user_id=user["id"])
        return _commit(db, payload, code)

    return _json(_call(run))


@mcp.tool(
    description=(
        "Change fields on a lead the acting user owns. "
        "fields is an object of column names to new values. "
        "Stage is not a field here; use set_stage. Editable fields: " + _EDITABLE
    ),
    annotations=ToolAnnotations(title="Update lead", read_only_hint=False, destructive_hint=True, open_world_hint=False),
)
def update_lead(lead_id: int, fields: dict) -> str:
    """Change editable fields on a lead. Stage changes go through set_stage."""

    def run(db, user):
        _lead, error = _owner_error(db, lead_id, user["id"])
        if error:
            return error
        incoming = _fields(fields)
        unknown = sorted(k for k in incoming if k not in EDITABLE_FIELDS)
        if unknown:
            return {"error": "unknown fields", "unknown": unknown, "editable": list(EDITABLE_FIELDS)}
        payload, code = apply_lead_updates(db, lead_id, incoming, actor="user", user_id=user["id"])
        return _commit(db, payload, code)

    return _json(_call(run))


@mcp.tool(
    description=(
        "Move a lead the acting user owns. Stages: " + _STAGES + ". "
        "Contacted requires confirm_sent. Do not contact requires suppression_reason. "
        "Finalized requires a link-free subject and message body already saved on the lead."
    ),
    annotations=ToolAnnotations(title="Set lead stage", read_only_hint=False, destructive_hint=True, open_world_hint=False),
)
def set_stage(
    lead_id: int,
    stage: str,
    confirm_sent: bool = False,
    sent_date: str = "",
    channel: str = "",
    suppression_reason: str = "",
    note: str = "",
    next_action: str = "",
) -> str:
    """Move a lead to another stage, using the same rules as the web app."""

    def run(db, user):
        _lead, error = _owner_error(db, lead_id, user["id"])
        if error:
            return error
        data = {
            "stage": stage,
            "confirm_sent": confirm_sent,
            "channel": channel,
            "suppression_reason": suppression_reason,
            "note": note,
        }
        if sent_date:
            data["date"] = sent_date
        if next_action:
            data["next_action"] = next_action
        payload, code = transition_stage(db, lead_id, data, actor="user", user_id=user["id"])
        return _commit(db, payload, code)

    return _json(_call(run))


@mcp.tool(
    annotations=ToolAnnotations(title="Delete lead", read_only_hint=False, destructive_hint=True, open_world_hint=False)
)
def delete_lead(lead_id: int) -> str:
    """Permanently delete a lead the acting user owns, including its events and autopilot rows."""

    def run(db, user):
        lead, error = _owner_error(db, lead_id, user["id"])
        if error:
            return error
        db.execute("DELETE FROM ai_run_log WHERE item_id IN (SELECT id FROM ai_run_items WHERE lead_id=?)", (lead_id,))
        db.execute("DELETE FROM ai_run_items WHERE lead_id=?", (lead_id,))
        db.execute("DELETE FROM events WHERE lead_id=?", (lead_id,))
        db.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        db.commit()
        return {"ok": True, "id": lead_id, "company_name": lead["company_name"]}

    return _json(_call(run))


if __name__ == "__main__":
    init_db()
    mcp.run(transport="stdio")
