"""
Shared core for the CRM: schema, DB access, SOP guard rails and helpers.

Used by app.py (HTTP API), autopilot.py (AI worker) and seed.py, so every path
that changes a lead goes through the same rules.
"""
import os
import re
import json
import sqlite3
import datetime as dt

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # optional unless DATABASE_URL/NEON_DATABASE_URL is set
    psycopg = None
    dict_row = None

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(HERE, ".env")):
    """Minimal KEY=VALUE loader for crm/.env (real environment variables win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
DB_PATH = os.environ.get("CRM_DB") or os.path.join(HERE, "crm.db")
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
USING_POSTGRES = bool(DATABASE_URL)

# ---------------------------------------------------------------------------
# Lifecycle stages (Appendix A). Order matters for the board.
# ---------------------------------------------------------------------------
PIPELINE_STAGES = [
    "to_research", "drafted", "finalized", "contacted",
    "replied", "call", "proposal", "won",
]
TERMINAL_STAGES = ["do_not_contact", "archived"]
ALL_STAGES = PIPELINE_STAGES + TERMINAL_STAGES

STAGE_LABELS = {
    "to_research": "To research",
    "drafted": "Drafted",
    "finalized": "Finalized",
    "contacted": "Contacted",
    "replied": "Replied",
    "call": "Call",
    "proposal": "Proposal",
    "won": "Won",
    "do_not_contact": "Do not contact",
    "archived": "Archived",
}

DEV_CAPABILITY = ["none_visible", "partner_led", "partial", "full_service", "uncertain"]
CONFIDENCE = ["high", "medium", "low"]
AI_STATUSES = ["none", "queued", "running", "done", "needs_review", "failed"]

# Scorecard dimensions (Appendix C), each 0-2.
SCORE_FIELDS = [
    "sc_agency_fit", "sc_size_maturity", "sc_recent_activity", "sc_dev_gap",
    "sc_work_relevance", "sc_access", "sc_personalization",
]

# Fields the client may write directly via PATCH /api/leads/<id>.
EDITABLE_FIELDS = [
    "company_name", "normalized_name", "website", "normalized_domain",
    "geography", "country", "city", "employees", "founded",
    "agency_profile", "dev_capability", "tech_signals", "relevant_work",
    "decision_maker", "contact_route", "contact_email", "contact_is_form",
    "restrictions", "evidence_notes", "opportunity_hypothesis", "pitch_angle",
    "subject", "message_draft", "gmail_account", "channel",
    "next_action", "follow_up_date", "reply_notes",
    "proposed_value", "payment_received", "suppression_reason", "campaign",
    "confidence", "priority_flag", "source_type", "source_url", "source_date",
] + SCORE_FIELDS

# Extra fields only the AI worker writes.
AI_FIELDS = ["ai_status", "ai_engine", "ai_last_run", "ai_suggestion",
             "ai_suggestion_reason", "ai_flags"]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clutch_id TEXT UNIQUE,
    company_name TEXT NOT NULL,
    normalized_name TEXT,
    website TEXT,
    normalized_domain TEXT,
    website_status TEXT,
    website_issue TEXT,
    geography TEXT,
    country TEXT,
    city TEXT,
    employees TEXT,
    founded TEXT,
    latest_review TEXT,
    review_count INTEGER DEFAULT 0,
    rating REAL,
    hourly_rate TEXT,
    min_project_size TEXT,
    phone TEXT,
    services TEXT,
    clutch_summary TEXT,
    profile_url TEXT,
    social_linkedin TEXT,
    social_facebook TEXT,
    social_instagram TEXT,
    social_twitter TEXT,
    social_youtube TEXT,
    social_behance TEXT,
    social_dribbble TEXT,
    source_type TEXT DEFAULT 'Clutch',
    source_url TEXT,
    source_date TEXT,
    -- enrichment
    agency_profile TEXT,
    dev_capability TEXT,
    tech_signals TEXT,
    relevant_work TEXT,
    decision_maker TEXT,
    contact_route TEXT,
    contact_email TEXT,
    contact_is_form INTEGER DEFAULT 0,
    restrictions TEXT,
    evidence_notes TEXT,
    opportunity_hypothesis TEXT,
    pitch_angle TEXT,
    confidence TEXT,
    -- scorecard
    sc_agency_fit INTEGER,
    sc_size_maturity INTEGER,
    sc_recent_activity INTEGER,
    sc_dev_gap INTEGER,
    sc_work_relevance INTEGER,
    sc_access INTEGER,
    sc_personalization INTEGER,
    score_total INTEGER,
    -- message
    subject TEXT,
    message_draft TEXT,
    draft_status TEXT DEFAULT 'none',
    -- lifecycle
    stage TEXT DEFAULT 'to_research',
    gmail_account TEXT,
    gmail_draft_id TEXT,
    channel TEXT,
    last_contacted TEXT,
    next_action TEXT,
    follow_up_date TEXT,
    followup_count INTEGER DEFAULT 0,
    reply_notes TEXT,
    proposed_value REAL,
    payment_received REAL,
    suppression_reason TEXT,
    campaign TEXT,
    priority_flag INTEGER DEFAULT 0,
    created_by_user_id INTEGER,
    owner_user_id INTEGER,
    updated_by_user_id INTEGER,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(normalized_domain);
CREATE INDEX IF NOT EXISTS idx_leads_reviews ON leads(review_count);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    ts TEXT,
    type TEXT,
    detail TEXT,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Local app users ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- AI Autopilot ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine TEXT,
    model TEXT,
    effort TEXT,
    status TEXT,              -- queued | running | done | cancelled | failed
    concurrency INTEGER,
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    lead_id INTEGER,
    status TEXT,              -- queued | running | done | needs_review | failed | cancelled
    started_at TEXT,
    finished_at TEXT,
    outcome TEXT,             -- short human summary, e.g. "Drafted" / "Hold: no contact route"
    error TEXT,
    result_json TEXT,
    FOREIGN KEY (run_id) REFERENCES ai_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_items_run ON ai_run_items(run_id);

CREATE TABLE IF NOT EXISTS ai_run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    item_id INTEGER,
    ts TEXT,
    kind TEXT,                -- info | tool | text | stderr | error | result
    line TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_log_run ON ai_run_log(run_id, id);

-- Connected Gmail accounts (OAuth, drafts only) --------------------------
CREATE TABLE IF NOT EXISTS google_accounts (
    email TEXT PRIMARY KEY,
    refresh_token TEXT,
    access_token TEXT,
    expires_at REAL,
    scopes TEXT,
    status TEXT DEFAULT 'connected',   -- connected | needs_reconnect
    connected_at TEXT,
    last_used TEXT
);
"""

POSTGRES_SCHEMA = """
-- Local app users ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    clutch_id TEXT UNIQUE,
    company_name TEXT NOT NULL,
    normalized_name TEXT,
    website TEXT,
    normalized_domain TEXT,
    website_status TEXT,
    website_issue TEXT,
    geography TEXT,
    country TEXT,
    city TEXT,
    employees TEXT,
    founded TEXT,
    latest_review TEXT,
    review_count INTEGER DEFAULT 0,
    rating REAL,
    hourly_rate TEXT,
    min_project_size TEXT,
    phone TEXT,
    services TEXT,
    clutch_summary TEXT,
    profile_url TEXT,
    social_linkedin TEXT,
    social_facebook TEXT,
    social_instagram TEXT,
    social_twitter TEXT,
    social_youtube TEXT,
    social_behance TEXT,
    social_dribbble TEXT,
    source_type TEXT DEFAULT 'Clutch',
    source_url TEXT,
    source_date TEXT,
    agency_profile TEXT,
    dev_capability TEXT,
    tech_signals TEXT,
    relevant_work TEXT,
    decision_maker TEXT,
    contact_route TEXT,
    contact_email TEXT,
    contact_is_form INTEGER DEFAULT 0,
    restrictions TEXT,
    evidence_notes TEXT,
    opportunity_hypothesis TEXT,
    pitch_angle TEXT,
    confidence TEXT,
    sc_agency_fit INTEGER,
    sc_size_maturity INTEGER,
    sc_recent_activity INTEGER,
    sc_dev_gap INTEGER,
    sc_work_relevance INTEGER,
    sc_access INTEGER,
    sc_personalization INTEGER,
    score_total INTEGER,
    subject TEXT,
    message_draft TEXT,
    draft_status TEXT DEFAULT 'none',
    stage TEXT DEFAULT 'to_research',
    gmail_account TEXT,
    gmail_draft_id TEXT,
    channel TEXT,
    last_contacted TEXT,
    next_action TEXT,
    follow_up_date TEXT,
    followup_count INTEGER DEFAULT 0,
    reply_notes TEXT,
    proposed_value REAL,
    payment_received REAL,
    suppression_reason TEXT,
    campaign TEXT,
    priority_flag INTEGER DEFAULT 0,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(normalized_domain);
CREATE INDEX IF NOT EXISTS idx_leads_reviews ON leads(review_count);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    ts TEXT,
    type TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ai_runs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    engine TEXT,
    model TEXT,
    effort TEXT,
    status TEXT,
    concurrency INTEGER,
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_run_items (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    run_id INTEGER REFERENCES ai_runs(id) ON DELETE CASCADE,
    lead_id INTEGER,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    outcome TEXT,
    error TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_items_run ON ai_run_items(run_id);

CREATE TABLE IF NOT EXISTS ai_run_log (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    run_id INTEGER,
    item_id INTEGER,
    ts TEXT,
    kind TEXT,
    line TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_log_run ON ai_run_log(run_id, id);

CREATE TABLE IF NOT EXISTS google_accounts (
    email TEXT PRIMARY KEY,
    refresh_token TEXT,
    access_token TEXT,
    expires_at REAL,
    scopes TEXT,
    status TEXT DEFAULT 'connected',
    connected_at TEXT,
    last_used TEXT
);
"""

# Columns added after v1 - applied by migrate() on existing databases.
LEAD_MIGRATIONS = [
    ("ai_status", "TEXT DEFAULT 'none'"),
    ("ai_engine", "TEXT"),
    ("ai_last_run", "TEXT"),
    ("ai_suggestion", "TEXT"),
    ("ai_suggestion_reason", "TEXT"),
    ("ai_flags", "TEXT"),
    ("created_by_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("owner_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("updated_by_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
]


def _pg_sql(sql):
    """Translate the app's sqlite-style query strings for psycopg."""
    sql = sql.replace("?", "%s")
    sql = sql.replace("ORDER BY company_name COLLATE NOCASE", "ORDER BY LOWER(company_name)")
    return sql


def _insert_table(sql):
    m = re.match(r"\s*INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", sql, re.I)
    return m.group(1).lower() if m else ""


class PostgresCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class PostgresConnection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=None):
        params = params or ()
        table = _insert_table(sql)
        wants_id = table in {"leads", "events", "users", "ai_runs", "ai_run_items", "ai_run_log"}
        if wants_id and " returning " not in sql.lower():
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self._raw.execute(_pg_sql(sql), params)
        lastrowid = None
        if wants_id:
            row = cur.fetchone()
            if row:
                lastrowid = row["id"]
        return PostgresCursor(cur, lastrowid)

    def executescript(self, sql):
        self._raw.execute(sql)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def connect():
    if USING_POSTGRES:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set, but psycopg is not installed. Run: pip install -r requirements.txt")
        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return PostgresConnection(raw)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def migrate(con):
    if USING_POSTGRES:
        have = {
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='leads'"
            )
        }
        for col, decl in LEAD_MIGRATIONS:
            if col not in have:
                con.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_leads_ai_status ON leads(ai_status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner_user_id)")
        return
    have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    for col, decl in LEAD_MIGRATIONS:
        if col not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_ai_status ON leads(ai_status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner_user_id)")


def init_db():
    con = connect()
    con.executescript(POSTGRES_SCHEMA if USING_POSTGRES else SCHEMA)
    migrate(con)
    if not USING_POSTGRES:
        # WAL lets the AI worker threads write while the UI reads.
        con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return dt.date.today().isoformat()


def add_business_days(start, n):
    d = start
    added = 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            added += 1
    return d


# Link detection for the first-touch rule (SOP 12.3 / 13.2): no URLs at all.
LINK_PATTERNS = [
    (r"https?://\S+", "http/https link"),
    (r"www\.\S+", "www. link"),
    (r"\b[a-z0-9][a-z0-9\-]*\.(?:com|net|org|io|co|dev|app|ai|xyz|design|studio|agency|us|uk|ca|au|shop|store)\b(?:/\S*)?", "domain-like string"),
    (r"\bmailto:\S+", "mailto link"),
    (r"<a\s", "embedded hyperlink"),
    (r"\]\(\S+\)", "markdown link"),
]


def detect_links(*texts):
    found = []
    for t in texts:
        if not t:
            continue
        for pat, label in LINK_PATTERNS:
            for m in re.finditer(pat, t, re.IGNORECASE):
                found.append({"match": m.group(0), "kind": label})
    return found


def compute_score(row):
    vals = [row.get(f) for f in SCORE_FIELDS]
    if all(v is None for v in vals):
        return None
    return sum(int(v) for v in vals if v is not None)


def log_event(db, lead_id, type_, detail):
    db.execute(
        "INSERT INTO events (lead_id, ts, type, detail) VALUES (?,?,?,?)",
        (lead_id, now_iso(), type_, detail),
    )


def lead_to_dict(row):
    d = dict(row)
    d["stage_label"] = STAGE_LABELS.get(d.get("stage"), d.get("stage"))
    if "ai_flags" in d:
        try:
            d["ai_flags"] = json.loads(d["ai_flags"]) if d["ai_flags"] else []
        except Exception:
            d["ai_flags"] = []
    return d


def normalize_domain(url):
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("/")[0].split("?")[0].strip("/")
    return u


def normalize_name(name):
    n = (name or "").lower()
    n = re.sub(r"\b(ltd|llc|inc|co|gmbh|pvt|limited|corporation|corp|studio|agency|group)\b", "", n)
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_PROOF_LIBRARY = """\
Positioning: [one line on what you build, e.g. full-stack development partner across front-end delivery, SaaS products, APIs and integrations, Shopify and WordPress]

Proof examples - pick at most one or two that fit the angle, never imply one site is the whole of the sender's experience:
- SaaS / product design studio -> [Your project] ([what it shows, e.g. approved flows turned into working product features])
- Creative website studio -> [Your project] ([what it shows])
- Shopify agency -> [Your project] ([what it shows])
- Ecommerce platform specialist -> [Your project] ([what it shows])
- AI / automation studio -> [Your project] ([what it shows])
- WordPress / B2B web agency -> [Your project or general experience] ([what it shows])
"""

DEFAULT_SETTINGS = {
    "gmail_accounts": [],
    "default_engine": "claude",
    "default_model": "",
    "default_effort": "",
    "concurrency": 2,
    "timeout_min": 8,
    "proof_library": DEFAULT_PROOF_LIBRARY,
    "sender_name": "",        # set in Settings - Autopilot won't run without it
    "sender_location": "",
    "offer_style": "paid_guarantee",   # paid | paid_guarantee
}


def get_setting(db, key, default=None):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not r:
        return DEFAULT_SETTINGS.get(key, default) if default is None else default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def set_setting(db, key, value):
    db.execute("INSERT INTO settings (key,value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))


def all_settings(db):
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


# ---------------------------------------------------------------------------
# Lead mutations (shared by the API, the MCP server, and the AI worker)
# ---------------------------------------------------------------------------
def create_lead(db, data, user_id=None):
    """Insert a manually added lead. Returns (payload, http_status). Does not commit.

    Duplicate website domains and normalized company names are rejected unless
    data['force'] is true. New leads start in To research.
    """
    name = (data.get("company_name") or "").strip()
    if not name:
        return {"error": "company_name required"}, 400
    domain = normalize_domain(data.get("website", ""))
    norm_name = normalize_name(name)
    dup = None
    if domain:
        dup = db.execute(
            "SELECT id, company_name, stage FROM leads WHERE normalized_domain=?",
            (domain,),
        ).fetchone()
    if not dup and norm_name:
        dup = db.execute(
            "SELECT id, company_name, stage FROM leads WHERE normalized_name=?",
            (norm_name,),
        ).fetchone()
    if dup and not data.get("force"):
        return {"error": "duplicate", "existing": dict(dup)}, 409

    source_type = (data.get("source_type") or "manual").strip() or "manual"
    cols = [
        "company_name", "normalized_name", "website", "normalized_domain",
        "geography", "source_type", "source_url", "campaign", "stage",
        "created_by_user_id", "owner_user_id", "updated_by_user_id",
        "created_at", "updated_at", "review_count",
    ]
    vals = [
        name, norm_name, data.get("website", "") or "", domain,
        data.get("geography", "") or "", source_type,
        data.get("source_url", "") or "", data.get("campaign", "") or "",
        "to_research", user_id, user_id, user_id,
        now_iso(), now_iso(), int(data.get("review_count") or 0),
    ]
    cur = db.execute(
        f"INSERT INTO leads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals,
    )
    log_event(db, cur.lastrowid, "system", f"Lead created manually ({source_type}).")
    return {"id": cur.lastrowid, "stage": "to_research"}, 200


def apply_lead_updates(db, lead_id, data, actor="user", allowed=None, user_id=None):
    """Write editable fields, keep derived fields in sync, log the change.

    Returns (payload, http_status). Does not commit."""
    r = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return {"error": "not found"}, 404
    allowed = allowed or EDITABLE_FIELDS
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return {"error": "no editable fields"}, 400

    # keep normalized_domain / name in sync if website/name changed
    if "website" in updates:
        updates["normalized_domain"] = normalize_domain(updates["website"])
    if "company_name" in updates:
        updates["normalized_name"] = normalize_name(updates["company_name"])
    if "ai_flags" in updates and not isinstance(updates["ai_flags"], str):
        updates["ai_flags"] = json.dumps(updates["ai_flags"] or [])

    merged = dict(r)
    merged.update(updates)
    updates["score_total"] = compute_score(merged)
    updates["updated_at"] = now_iso()
    if user_id is not None:
        updates["updated_by_user_id"] = user_id

    # draft_status housekeeping
    if ("subject" in updates or "message_draft" in updates):
        if (merged.get("subject") or "").strip() and (merged.get("message_draft") or "").strip():
            if merged.get("draft_status") in (None, "none"):
                updates["draft_status"] = "drafted"

    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE leads SET {sets} WHERE id=?", list(updates.values()) + [lead_id])
    changed = sorted(k for k in updates if k not in ("updated_at", "updated_by_user_id", "score_total"))
    if actor == "ai":
        log_event(db, lead_id, "ai", "AI updated: " + ", ".join(changed))
    else:
        log_event(db, lead_id, "note", "Fields updated: " + ", ".join(sorted(updates.keys())))
    return {"ok": True, "score_total": updates["score_total"]}, 200


def transition_stage(db, lead_id, data, actor="user", user_id=None):
    """Move a lead to data['stage'] with the SOP guard rails.

    Returns (payload, http_status). Does not commit."""
    r = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return {"error": "not found"}, 404
    target = data.get("stage")
    if target not in ALL_STAGES:
        return {"error": "invalid stage"}, 400

    lead = dict(r)
    extra = {}

    # The AI may only ever move a lead to Drafted (SOP 3: human-gated stages).
    if actor == "ai" and target != "drafted":
        return {"error": f"AI is not allowed to move a lead to {target}."}, 403

    if target == "drafted" and actor == "ai":
        if not (lead.get("subject") or "").strip() or not (lead.get("message_draft") or "").strip():
            return {"error": "Cannot mark Drafted: subject and message body are required."}, 422

    # Guard: Finalized requires link-free subject + body (SOP 12 / 13.2).
    if target == "finalized":
        subject = lead.get("subject") or ""
        body = lead.get("message_draft") or ""
        if not subject.strip() or not body.strip():
            return {"error": "Cannot finalize: subject and message body are required."}, 422
        links = detect_links(subject, body)
        if links:
            return {
                "error": "Cannot finalize: the first-touch message must contain no links.",
                "links": links,
            }, 422
        extra["draft_status"] = "finalized"

    # Guard: Contacted requires evidence (SOP 15.2) -> channel + date.
    if target == "contacted":
        if not data.get("confirm_sent"):
            return {
                "error": "Contacted requires send evidence.",
                "need": "Confirm the message was sent from Gmail (or the form submitted), then confirm."
            }, 422
        extra["last_contacted"] = data.get("date") or today()
        extra["channel"] = data.get("channel") or lead.get("channel") or (lead.get("gmail_account") or "Gmail")
        # schedule first follow-up 5 business days out
        start = dt.date.fromisoformat(extra["last_contacted"])
        extra["follow_up_date"] = add_business_days(start, 5).isoformat()
        extra["next_action"] = "First follow-up"

    # Do not contact requires a suppression reason (SOP 18.1).
    if target == "do_not_contact":
        reason = data.get("suppression_reason") or lead.get("suppression_reason")
        if not reason:
            return {"error": "Do not contact requires a suppression reason."}, 422
        extra["suppression_reason"] = reason
        extra["next_action"] = ""
        extra["follow_up_date"] = ""

    if target == "archived" and data.get("suppression_reason"):
        extra["suppression_reason"] = data["suppression_reason"]

    if target == "won":
        extra["next_action"] = data.get("next_action", "Invoice / deliver")

    extra["stage"] = target
    extra["updated_at"] = now_iso()
    if user_id is not None:
        extra["updated_by_user_id"] = user_id
    sets = ", ".join(f"{k}=?" for k in extra)
    db.execute(f"UPDATE leads SET {sets} WHERE id=?", list(extra.values()) + [lead_id])
    log_event(db, lead_id, "stage_change",
              f"{STAGE_LABELS.get(lead.get('stage'))} -> {STAGE_LABELS.get(target)}"
              + (" (AI)" if actor == "ai" else "")
              + (f" | {data.get('note')}" if data.get('note') else ""))
    return {"ok": True, "stage": target, "updates": extra}, 200
