"""
Outreach CRM - local CRM for the AI-Assisted Agency Outreach Process.

Run:   python app.py
API on http://127.0.0.1:5050  (new UI: crm-web on http://localhost:3100,
legacy UI still served at http://127.0.0.1:5050/).

Everything stays on this machine. SQLite file: crm.db (created on first run).
Seed your leads once with:  python seed.py
"""
import os
import datetime as dt
from flask import Flask, request, jsonify, g, render_template, session
from werkzeug.security import check_password_hash, generate_password_hash

from core import (  # noqa: F401  (re-exported for seed.py / older scripts)
    DB_PATH, PIPELINE_STAGES, TERMINAL_STAGES, ALL_STAGES, STAGE_LABELS,
    DEV_CAPABILITY, CONFIDENCE, AI_STATUSES, SCORE_FIELDS, EDITABLE_FIELDS,
    connect, init_db, now_iso, today, add_business_days, detect_links,
    compute_score, log_event, lead_to_dict, normalize_domain, normalize_name,
    get_setting, set_setting, all_settings, DEFAULT_SETTINGS,
    create_lead, apply_lead_updates, transition_stage,
)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("CRM_SECRET_KEY", "outreach-crm-local-dev-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def public_user(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
    }


def users_exist(db):
    return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def current_user(db):
    uid = session.get("user_id")
    if not uid:
        return None
    return db.execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone()


def current_user_id():
    return session.get("user_id")


def require_lead_owner(db, lead_id):
    """Return (lead, None) when the current user may mutate the lead."""
    lead = db.execute("SELECT id, company_name, owner_user_id FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return None, (jsonify({"error": "not found"}), 404)
    owner_id = lead["owner_user_id"]
    if owner_id is not None and owner_id != current_user_id():
        return None, (jsonify({"error": "Only the lead owner can edit or delete this lead."}), 403)
    return lead, None


def require_auth():
    if not request.path.startswith("/api/"):
        return None
    if request.path.startswith("/api/auth/"):
        return None
    if request.path == "/api/google/callback":
        return None
    db = get_db()
    user = current_user(db)
    if not user:
        return jsonify({"error": "login required"}), 401
    g.user = user
    return None


@app.before_request
def before_request():
    return require_auth()


@app.get("/api/auth/me")
def api_auth_me():
    db = get_db()
    user = current_user(db)
    return jsonify({
        "authenticated": bool(user),
        "user": public_user(user) if user else None,
        "has_users": users_exist(db),
    })


@app.post("/api/auth/register")
def api_auth_register():
    db = get_db()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not name:
        return jsonify({"error": "name required"}), 400
    if "@" not in email or "." not in email:
        return jsonify({"error": "valid email required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400
    if db.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
        return jsonify({"error": "email already registered"}), 409
    cur = db.execute(
        "INSERT INTO users (name, email, password_hash, created_at, last_login_at) VALUES (?, ?, ?, ?, ?)",
        (name, email, generate_password_hash(password), now_iso(), now_iso()),
    )
    db.commit()
    session.clear()
    session["user_id"] = cur.lastrowid
    user = db.execute("SELECT id, name, email FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"user": public_user(user)}), 201


@app.post("/api/auth/login")
def api_auth_login():
    db = get_db()
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user = db.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "invalid email or password"}), 401
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
    db.commit()
    session.clear()
    session["user_id"] = user["id"]
    return jsonify({"user": public_user(user)})


@app.post("/api/auth/logout")
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes - pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# ---------------------------------------------------------------------------
# API - meta
# ---------------------------------------------------------------------------
@app.get("/api/meta")
def api_meta():
    db = get_db()
    counts = {s: 0 for s in ALL_STAGES}
    for r in db.execute("SELECT stage, COUNT(*) c FROM leads GROUP BY stage"):
        counts[r["stage"]] = r["c"]
    ai_counts = {s: 0 for s in AI_STATUSES}
    for r in db.execute("SELECT COALESCE(ai_status,'none') s, COUNT(*) c FROM leads GROUP BY 1"):
        ai_counts[r["s"]] = r["c"]
    campaigns = [r["campaign"] for r in db.execute(
        "SELECT DISTINCT campaign FROM leads WHERE campaign IS NOT NULL AND campaign<>'' ORDER BY campaign")]
    accounts = get_setting(db, "gmail_accounts", [])
    return jsonify({
        "pipeline_stages": PIPELINE_STAGES,
        "terminal_stages": TERMINAL_STAGES,
        "stage_labels": STAGE_LABELS,
        "dev_capability": DEV_CAPABILITY,
        "confidence": CONFIDENCE,
        "score_fields": SCORE_FIELDS,
        "counts": counts,
        "ai_counts": ai_counts,
        "campaigns": campaigns,
        "gmail_accounts": accounts,
        "today": today(),
    })


@app.post("/api/settings/gmail_accounts")
def api_set_accounts():
    db = get_db()
    data = request.get_json(force=True)
    accounts = [a.strip() for a in data.get("accounts", []) if a.strip()]
    set_setting(db, "gmail_accounts", accounts)
    db.commit()
    return jsonify({"gmail_accounts": accounts})


@app.get("/api/settings")
def api_get_settings():
    return jsonify(all_settings(get_db()))


@app.post("/api/settings")
def api_save_settings():
    db = get_db()
    data = request.get_json(force=True)
    for k, v in data.items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k == "gmail_accounts":
            v = [a.strip() for a in (v or []) if str(a).strip()]
        if k == "concurrency":
            v = max(1, min(4, int(v or 1)))
        if k == "timeout_min":
            v = max(2, min(30, int(v or 8)))
        if k == "offer_style" and v not in ("paid", "paid_guarantee"):
            continue
        set_setting(db, k, v)
    db.commit()
    return jsonify(all_settings(db))


# ---------------------------------------------------------------------------
# API - leads list
# ---------------------------------------------------------------------------
@app.get("/api/leads")
def api_leads():
    db = get_db()
    stage = request.args.get("stage")
    status = request.args.get("status")  # website_status filter
    campaign = request.args.get("campaign")
    ai_status = request.args.get("ai_status")
    q = request.args.get("q", "").strip()
    due = request.args.get("due")  # 'today' -> follow-ups due
    ids = request.args.get("ids")
    min_reviews = request.args.get("min_reviews")
    limit = int(request.args.get("limit", 500))
    offset = int(request.args.get("offset", 0))
    order = request.args.get("order", "reviews")

    where = []
    params = []
    if stage:
        stages = [s for s in stage.split(",") if s in ALL_STAGES]
        where.append(f"stage IN ({','.join('?' * len(stages))})")
        params += stages
    if status:
        if status == "unknown":
            where.append("(website_status IS NULL OR website_status = '')")
        else:
            where.append("website_status = ?")
            params.append(status)
    if campaign:
        where.append("campaign = ?")
        params.append(campaign)
    if ai_status:
        if ai_status == "none":
            where.append("(ai_status IS NULL OR ai_status = 'none')")
        else:
            where.append("ai_status = ?")
            params.append(ai_status)
    if min_reviews:
        where.append("review_count >= ?")
        params.append(int(min_reviews))
    if ids:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        where.append(f"id IN ({','.join('?' * len(id_list)) or 'NULL'})")
        params += id_list
    if q:
        where.append("(company_name LIKE ? OR website LIKE ? OR geography LIKE ? OR services LIKE ?)")
        params += [f"%{q}%"] * 4
    if due == "today":
        where.append("follow_up_date IS NOT NULL AND follow_up_date <> '' AND follow_up_date <= ?")
        params.append(today())
        where.append("stage IN ('contacted','replied','call','proposal')")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = "SELECT * FROM leads" + where_sql
    if order == "score":
        sql += " ORDER BY score_total DESC NULLS LAST, review_count DESC"
    elif order == "name":
        sql += " ORDER BY company_name COLLATE NOCASE"
    elif order == "followup":
        sql += " ORDER BY follow_up_date ASC"
    elif order == "updated":
        sql += " ORDER BY updated_at DESC"
    elif order == "rating":
        sql += " ORDER BY rating DESC NULLS LAST, review_count DESC"
    else:
        sql += " ORDER BY review_count DESC, score_total DESC NULLS LAST"
    sql += " LIMIT ? OFFSET ?"

    total = db.execute("SELECT COUNT(*) n FROM leads" + where_sql, params).fetchone()["n"]
    rows = [lead_to_dict(r) for r in db.execute(sql, params + [limit, offset])]
    return jsonify({"leads": rows, "count": len(rows), "total": total, "offset": offset})


@app.get("/api/leads/<int:lead_id>")
def api_lead(lead_id):
    db = get_db()
    r = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    lead = lead_to_dict(r)
    lead["events"] = [dict(e) for e in db.execute(
        "SELECT * FROM events WHERE lead_id=? ORDER BY ts DESC, id DESC", (lead_id,))]
    lead["ai_runs"] = [dict(x) for x in db.execute(
        "SELECT i.id item_id, i.run_id, i.status, i.outcome, i.error, i.started_at, i.finished_at, r.engine "
        "FROM ai_run_items i JOIN ai_runs r ON r.id=i.run_id WHERE i.lead_id=? ORDER BY i.id DESC LIMIT 10",
        (lead_id,))]
    return jsonify(lead)


@app.post("/api/leads")
def api_create_lead():
    """Manually add a lead (verifies dedupe by normalized domain / name)."""
    db = get_db()
    payload, code = create_lead(db, request.get_json(force=True), user_id=current_user_id())
    if code == 200:
        db.commit()
    return jsonify(payload), code


@app.patch("/api/leads/<int:lead_id>")
def api_update_lead(lead_id):
    db = get_db()
    _, error = require_lead_owner(db, lead_id)
    if error:
        return error
    payload, code = apply_lead_updates(db, lead_id, request.get_json(force=True), user_id=current_user_id())
    if code == 200:
        db.commit()
    return jsonify(payload), code


@app.delete("/api/leads/<int:lead_id>")
def api_delete_lead(lead_id):
    db = get_db()
    lead, error = require_lead_owner(db, lead_id)
    if error:
        return error
    db.execute("DELETE FROM ai_run_log WHERE item_id IN (SELECT id FROM ai_run_items WHERE lead_id=?)", (lead_id,))
    db.execute("DELETE FROM ai_run_items WHERE lead_id=?", (lead_id,))
    db.execute("DELETE FROM events WHERE lead_id=?", (lead_id,))
    db.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    db.commit()
    return jsonify({"ok": True, "id": lead_id, "company_name": lead["company_name"]})


# ---------------------------------------------------------------------------
# API - stage transitions (with SOP guard rails)
# ---------------------------------------------------------------------------
@app.post("/api/leads/<int:lead_id>/stage")
def api_set_stage(lead_id):
    db = get_db()
    _, error = require_lead_owner(db, lead_id)
    if error:
        return error
    payload, code = transition_stage(db, lead_id, request.get_json(force=True), user_id=current_user_id())
    if code == 200:
        db.commit()
    return jsonify(payload), code


@app.post("/api/leads/bulk_finalize")
def api_bulk_finalize():
    """Owner finalizes several Drafted leads at once. Same guard rails as one-by-one
    (link-free subject + body); AI-flagged drafts are skipped unless include_flagged."""
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("ids") or []]
    include_flagged = bool(data.get("include_flagged"))
    db = get_db()
    finalized, skipped = [], []
    for lead_id in ids:
        r = db.execute("SELECT id, company_name, stage, ai_status, owner_user_id FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not r:
            continue
        item = {"id": r["id"], "company": r["company_name"]}
        if r["owner_user_id"] is not None and r["owner_user_id"] != current_user_id():
            skipped.append({**item, "reason": "Only the lead owner can edit this lead"})
            continue
        if r["stage"] != "drafted":
            skipped.append({**item, "reason": f"Not drafted ({STAGE_LABELS.get(r['stage'], r['stage'])})"})
            continue
        if r["ai_status"] == "needs_review" and not include_flagged:
            skipped.append({**item, "reason": "AI flagged it. Open it and review first"})
            continue
        payload, code = transition_stage(
            db, lead_id, {"stage": "finalized", "note": "Finalized for Gmail (bulk)."}, user_id=current_user_id())
        if code == 200:
            finalized.append(item)
        else:
            reason = payload.get("error", "Refused")
            if payload.get("links"):
                reason += " Remove: " + ", ".join(l["match"] for l in payload["links"])
            skipped.append({**item, "reason": reason})
    db.commit()
    return jsonify({"finalized": finalized, "skipped": skipped})


@app.post("/api/leads/<int:lead_id>/followup")
def api_followup(lead_id):
    """Record that a follow-up was sent; schedule the next per SOP 16.1."""
    db = get_db()
    _, error = require_lead_owner(db, lead_id)
    if error:
        return error
    r = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    lead = dict(r)
    n = (lead.get("followup_count") or 0) + 1
    extra = {"followup_count": n, "updated_at": now_iso(), "updated_by_user_id": current_user_id()}
    if n == 1:
        # final follow-up 7 calendar days later
        extra["follow_up_date"] = (dt.date.today() + dt.timedelta(days=7)).isoformat()
        extra["next_action"] = "Final follow-up"
        label = "First follow-up sent"
    else:
        extra["follow_up_date"] = ""
        extra["next_action"] = "Await reply / close loop"
        label = f"Follow-up #{n} sent"
    sets = ", ".join(f"{k}=?" for k in extra)
    db.execute(f"UPDATE leads SET {sets} WHERE id=?", list(extra.values()) + [lead_id])
    log_event(db, lead_id, "followup", label)
    db.commit()
    return jsonify({"ok": True, **extra})


@app.post("/api/leads/<int:lead_id>/check_links")
def api_check_links(lead_id):
    data = request.get_json(force=True)
    links = detect_links(data.get("subject", ""), data.get("message_draft", ""))
    return jsonify({"links": links, "clean": not links})


@app.post("/api/leads/<int:lead_id>/ai_suggestion")
def api_ai_suggestion(lead_id):
    """Human accepts or dismisses the AI's hold/exclude suggestion.

    Accepting 'exclude' archives the lead (never Do not contact - that stays a
    deliberate owner action). Accepting 'hold' keeps it in To research with the
    AI's next action."""
    db = get_db()
    _, error = require_lead_owner(db, lead_id)
    if error:
        return error
    r = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    sugg, reason = r["ai_suggestion"], r["ai_suggestion_reason"] or ""
    if data.get("accept") and sugg == "exclude":
        payload, code = transition_stage(db, lead_id, {
            "stage": "archived", "suppression_reason": f"AI exclude: {reason}",
            "note": "Accepted AI exclude suggestion"}, user_id=current_user_id())
        if code != 200:
            return jsonify(payload), code
    log_event(db, lead_id, "ai",
              f"{'Accepted' if data.get('accept') else 'Dismissed'} AI suggestion: {sugg} - {reason}")
    db.execute("UPDATE leads SET ai_suggestion=NULL, ai_suggestion_reason=NULL, "
               "ai_status=CASE WHEN ai_status='needs_review' THEN 'done' ELSE ai_status END, "
               "updated_at=?, updated_by_user_id=? WHERE id=?", (now_iso(), current_user_id(), lead_id))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API - dashboard metrics (SOP 21)
# ---------------------------------------------------------------------------
@app.get("/api/metrics")
def api_metrics():
    db = get_db()
    c = {s: 0 for s in ALL_STAGES}
    for r in db.execute("SELECT stage, COUNT(*) n FROM leads GROUP BY stage"):
        c[r["stage"]] = r["n"]

    def contacted_plus():
        return c["contacted"] + c["replied"] + c["call"] + c["proposal"] + c["won"]

    replied_plus = c["replied"] + c["call"] + c["proposal"] + c["won"]
    proposal_plus = c["proposal"] + c["won"]
    cash = db.execute("SELECT COALESCE(SUM(payment_received),0) s FROM leads").fetchone()["s"]
    pipeline_value = db.execute(
        "SELECT COALESCE(SUM(proposed_value),0) s FROM leads WHERE stage IN ('proposal','won')").fetchone()["s"]

    def rate(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    week_ago = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    ai = {
        "researched_7d": db.execute(
            "SELECT COUNT(*) n FROM ai_run_items WHERE status IN ('done','needs_review') AND finished_at>=?",
            (week_ago,)).fetchone()["n"],
        "drafted_7d": db.execute(
            "SELECT COUNT(*) n FROM events WHERE type='stage_change' AND detail LIKE '%-> Drafted (AI)%' AND ts>=?",
            (week_ago,)).fetchone()["n"],
        "failed_7d": db.execute(
            "SELECT COUNT(*) n FROM ai_run_items WHERE status='failed' AND finished_at>=?",
            (week_ago,)).fetchone()["n"],
        "needs_review": db.execute(
            "SELECT COUNT(*) n FROM leads WHERE ai_status='needs_review'").fetchone()["n"],
    }

    sent = contacted_plus()
    metrics = {
        "counts": c,
        "funnel": {
            "contacted": sent,
            "replied": replied_plus,
            "call": c["call"] + c["proposal"] + c["won"],
            "proposal": proposal_plus,
            "won": c["won"],
        },
        "rates": {
            "reply_rate": rate(replied_plus, sent),
            "call_rate": rate(c["call"] + c["proposal"] + c["won"], sent),
            "proposal_rate": rate(proposal_plus, replied_plus),
            "win_rate": rate(c["won"], proposal_plus),
        },
        "cash_received": cash,
        "pipeline_value": pipeline_value,
        "ai": ai,
        "due_today": db.execute(
            "SELECT COUNT(*) n FROM leads WHERE follow_up_date<>'' AND follow_up_date<=? "
            "AND stage IN ('contacted','replied','call','proposal')", (today(),)).fetchone()["n"],
    }
    return jsonify(metrics)


# ---------------------------------------------------------------------------
# API - Today / work queue (SOP 20.3 priority order)
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def api_queue():
    """The 'Today' screen: follow-ups due, ready-to-draft, research-next batch."""
    db = get_db()
    batch = int(request.args.get("batch", 25))
    offset = int(request.args.get("offset", 0))

    due = [lead_to_dict(r) for r in db.execute(
        "SELECT * FROM leads WHERE follow_up_date IS NOT NULL AND follow_up_date<>'' "
        "AND follow_up_date<=? AND stage IN ('contacted','replied','call','proposal') "
        "ORDER BY follow_up_date ASC, review_count DESC LIMIT 50", (today(),))]

    # Replies / active conversations needing attention come first (SOP 20.3).
    active = [lead_to_dict(r) for r in db.execute(
        "SELECT * FROM leads WHERE stage IN ('replied','call','proposal') "
        "ORDER BY review_count DESC LIMIT 50")]

    ready = [lead_to_dict(r) for r in db.execute(
        "SELECT * FROM leads WHERE stage IN ('finalized','drafted') "
        "ORDER BY CASE stage WHEN 'finalized' THEN 0 ELSE 1 END, "
        "CASE ai_status WHEN 'needs_review' THEN 0 ELSE 1 END, review_count DESC LIMIT 50")]

    # AI researched these but suggested hold/exclude - waiting for a human call.
    suggested = [lead_to_dict(r) for r in db.execute(
        "SELECT * FROM leads WHERE stage='to_research' AND ai_suggestion IN ('hold','exclude') "
        "ORDER BY review_count DESC LIMIT 50")]

    research = [lead_to_dict(r) for r in db.execute(
        "SELECT * FROM leads WHERE stage='to_research' AND (ai_suggestion IS NULL OR ai_suggestion='') "
        "ORDER BY review_count DESC, score_total DESC NULLS LAST LIMIT ? OFFSET ?",
        (batch, offset))]
    research_total = db.execute(
        "SELECT COUNT(*) n FROM leads WHERE stage='to_research'").fetchone()["n"]

    return jsonify({
        "due": due,
        "active": active,
        "ready": ready,
        "suggested": suggested,
        "research": research,
        "research_total": research_total,
        "offset": offset,
        "batch": batch,
    })


# ---------------------------------------------------------------------------
# API - Gmail batch (SOP 14). No API/OAuth: drafts are created by Claude via the
# in-chat Gmail connector. These endpoints only READ the finalized batch and
# RECORD a draft that was created. Nothing here sends, nothing talks to Google.
# ---------------------------------------------------------------------------
@app.get("/api/gmail/batch")
def api_gmail_batch():
    """Finalized leads ready for draft creation, with resolved recipient + link check."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM leads WHERE stage='finalized' "
        "AND (owner_user_id IS NULL OR owner_user_id=?) ORDER BY review_count DESC",
        (current_user_id(),),
    )
    out = []
    for r in rows:
        lead = dict(r)
        links = detect_links(lead.get("subject"), lead.get("message_draft"))
        to_addr = "" if lead.get("contact_is_form") else (lead.get("contact_email") or "")
        out.append({
            "id": lead["id"],
            "company_name": lead["company_name"],
            "review_count": lead["review_count"],
            "subject": lead.get("subject") or "",
            "message_draft": lead.get("message_draft") or "",
            "to": to_addr,
            "recipient_label": to_addr or "(form only - blank To)",
            "contact_is_form": lead.get("contact_is_form"),
            "contact_route": lead.get("contact_route") or "",
            "website": lead.get("website") or "",
            "has_links": bool(links),
            "links": links,
            "gmail_draft_id": lead.get("gmail_draft_id"),
            "gmail_account": lead.get("gmail_account"),
        })
    return jsonify({"batch": out, "count": len(out)})


@app.post("/api/leads/<int:lead_id>/gmail_draft")
def api_record_draft(lead_id):
    """Record that a Gmail draft was created (by Claude via the connector).

    Stage intentionally stays 'finalized' - it only becomes 'contacted' after the
    human sends it and confirms (see /stage with confirm_sent)."""
    db = get_db()
    _, error = require_lead_owner(db, lead_id)
    if error:
        return error
    r = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    account = (data.get("account") or "").strip()
    draft_id = (data.get("draft_id") or "").strip()
    if not account:
        return jsonify({"error": "account required"}), 400
    to_label = data.get("to") or ("(form only)" if r["contact_is_form"] else (r["contact_email"] or ""))
    db.execute("UPDATE leads SET gmail_draft_id=?, gmail_account=?, updated_at=?, updated_by_user_id=? WHERE id=?",
               (draft_id, account, now_iso(), current_user_id(), lead_id))
    log_event(db, lead_id, "gmail",
              f"Gmail draft created in {account} -> {to_label}"
              + (f" (draft {draft_id})" if draft_id else "") + ". Not sent.")
    db.commit()
    return jsonify({"ok": True, "gmail_draft_id": draft_id, "gmail_account": account})


# AI Autopilot endpoints live in autopilot.py.
import autopilot  # noqa: E402
autopilot.register(app)

# Gmail drafts via Google OAuth (drafts only - never sends) live in gmail.py.
import gmail  # noqa: E402
gmail.register(app)


if __name__ == "__main__":
    init_db()
    autopilot.recover_interrupted()
    # Render sets RENDER and PORT. A local run sets neither, so it stays on loopback.
    if os.environ.get("RENDER"):
        host = "0.0.0.0"
        port = int(os.environ["PORT"])
    else:
        host = "127.0.0.1"
        port = int(os.environ.get("CRM_PORT", 5050))
    print(f"Outreach CRM API -> http://{host}:{port}  (UI: http://localhost:3100)")
    app.run(host=host, port=port, debug=False, threaded=True)
