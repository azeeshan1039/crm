"""
Gmail drafts through Google OAuth - DRAFTS ONLY.

The CRM signs in to each Gmail account (gmail.compose scope), creates one draft per
Finalized lead and records it. It never sends: there is no send call anywhere in this
module (tests/test_gmail.py enforces that). The owner opens Gmail, reviews, and clicks
Send; the lead becomes Contacted only after "I sent it" in the CRM (SOP 14-15).

Standard library only (urllib) - no Google client libraries needed.
Config comes from crm/.env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI.
"""
import os
import time
import json
import base64
import hashlib
import secrets
import urllib.parse
import urllib.request
import urllib.error
from email.message import EmailMessage
from email.utils import formataddr

from flask import request, jsonify, redirect, session

import core
from core import connect, now_iso, detect_links, log_event, get_setting

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.compose"]

_pending = {}  # OAuth state -> (code_verifier, created)


def current_user_id():
    try:
        return session.get("user_id")
    except RuntimeError:
        return None


class GmailError(Exception):
    def __init__(self, message, reconnect=False, fatal=False):
        super().__init__(message)
        self.reconnect = reconnect
        # fatal: every remaining draft in the batch would fail the same way, so stop.
        self.fatal = fatal or reconnect


def config():
    return {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", "http://127.0.0.1:5050/api/google/callback"),
        "ui_url": os.environ.get("CRM_UI_URL", "http://localhost:3100"),
    }


def configured():
    c = config()
    return bool(c["client_id"] and c["client_secret"])


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)
        except ValueError:
            err = {"error": body[:200]}
        raise GmailError(err.get("error_description") or err.get("error") or f"HTTP {e.code}",
                         reconnect=err.get("error") == "invalid_grant") from None


def _jwt_email(id_token):
    # The id_token comes straight from Google's token endpoint over TLS; we only read the email claim.
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("email", "").lower()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def access_token(con, email):
    row = con.execute("SELECT * FROM google_accounts WHERE email=?", (email,)).fetchone()
    if not row:
        raise GmailError(f"{email} isn't connected. Connect it in Settings.", reconnect=True)
    if row["status"] == "needs_reconnect":
        raise GmailError(f"{email} needs to be reconnected in Settings.", reconnect=True)
    if row["access_token"] and (row["expires_at"] or 0) > time.time() + 60:
        return row["access_token"]
    c = config()
    try:
        tok = _post_form(TOKEN_URL, {
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": row["refresh_token"], "grant_type": "refresh_token"})
    except GmailError as e:
        if e.reconnect:
            con.execute("UPDATE google_accounts SET status='needs_reconnect', access_token=NULL WHERE email=?", (email,))
            con.commit()
            raise GmailError(f"Google signed {email} out (test apps are logged out every 7 days). "
                             "Reconnect it in Settings.", reconnect=True) from None
        raise
    con.execute("UPDATE google_accounts SET access_token=?, expires_at=? WHERE email=?",
                (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)), email))
    con.commit()
    return tok["access_token"]


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------
def build_message(account, sender_name, to, subject, body):
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, account)) if sender_name else account
    if to:
        msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)  # plain text only: no HTML, no signature, no tracking
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def create_draft(con, account, to, subject, body, sender_name=""):
    """Create (never send) one Gmail draft. Returns the Gmail draft resource."""
    token = access_token(con, account)
    payload = json.dumps({"message": {"raw": build_message(account, sender_name, to, subject, body)}}).encode()
    req = urllib.request.Request(DRAFTS_URL, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            draft = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            con.execute("UPDATE google_accounts SET access_token=NULL WHERE email=?", (account,))
            con.commit()
        if e.code == 403 and ("SERVICE_DISABLED" in detail or "has not been used" in detail):
            raise GmailError(
                "The Gmail API is switched off in your Google Cloud project. Enable it at "
                "console.cloud.google.com > APIs & Services > Library > Gmail API, wait a "
                "couple of minutes, then try again.", fatal=True) from None
        raise GmailError(f"Gmail refused the draft (HTTP {e.code}): {detail}", reconnect=e.code == 401,
                         fatal=e.code in (401, 403, 429)) from None
    con.execute("UPDATE google_accounts SET last_used=? WHERE email=?", (now_iso(), account))
    return draft


def draft_batch(con, account, lead_ids=None, force=False):
    """Create drafts for Finalized leads. Re-runs every Gmail gate from SOP 14 / 19.3."""
    sql = "SELECT * FROM leads WHERE stage='finalized'"
    params = []
    user_id = current_user_id()
    if user_id is not None:
        sql += " AND (owner_user_id IS NULL OR owner_user_id=?)"
        params.append(user_id)
    if lead_ids:
        sql += f" AND id IN ({','.join('?' * len(lead_ids))})"
        params += list(lead_ids)
    leads = [dict(r) for r in con.execute(sql + " ORDER BY review_count DESC", params)]
    sender = get_setting(con, "sender_name") or ""
    out = {"account": account, "created": [], "skipped": [], "failed": []}
    for lead in leads:
        name = lead["company_name"]
        subject = (lead.get("subject") or "").strip()
        body = (lead.get("message_draft") or "").strip()
        if not subject or not body:
            out["skipped"].append({"id": lead["id"], "company": name, "reason": "no subject or body"})
            continue
        if detect_links(subject, body):
            out["skipped"].append({"id": lead["id"], "company": name, "reason": "message contains a link"})
            continue
        if lead.get("gmail_draft_id") and not force:
            out["skipped"].append({"id": lead["id"], "company": name,
                                   "reason": f"already has a draft in {lead.get('gmail_account')}"})
            continue
        to = "" if lead.get("contact_is_form") else (lead.get("contact_email") or "").strip()
        if not to and not lead.get("contact_is_form"):
            out["skipped"].append({"id": lead["id"], "company": name,
                                   "reason": "no verified email and not marked form-only"})
            continue
        try:
            draft = create_draft(con, account, to, subject, body, sender)
        except GmailError as e:
            out["failed"].append({"id": lead["id"], "company": name, "reason": str(e)})
            if e.fatal:
                break  # every remaining lead would fail the same way
            continue
        con.execute("UPDATE leads SET gmail_draft_id=?, gmail_account=?, updated_at=?, updated_by_user_id=? WHERE id=?",
                    (draft.get("id", ""), account, now_iso(), session.get("user_id"), lead["id"]))
        log_event(con, lead["id"], "gmail",
                  f"Gmail draft created in {account} -> {to or '(form only - blank To)'} "
                  f"(draft {draft.get('id', '')}). Not sent.")
        con.commit()
        out["created"].append({"id": lead["id"], "company": name, "to": to or "", "form_only": not to})
    out["addressed"] = sum(1 for c in out["created"] if not c["form_only"])
    out["form_only"] = sum(1 for c in out["created"] if c["form_only"])
    return out


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def _accounts(con):
    return [dict(r) for r in con.execute(
        "SELECT email, status, connected_at, last_used FROM google_accounts ORDER BY connected_at")]


def register(app):
    @app.get("/api/google/status")
    def g_status():
        con = connect()
        try:
            return jsonify({"configured": configured(), "accounts": _accounts(con),
                            "redirect_uri": config()["redirect_uri"]})
        finally:
            con.close()

    @app.get("/api/google/connect")
    def g_connect():
        c = config()
        if not configured():
            return jsonify({"error": "Google isn't set up: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing in crm/.env"}), 400
        now = time.time()
        for s in [s for s, (_, t) in _pending.items() if now - t > 900]:
            _pending.pop(s, None)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        _pending[state] = (verifier, now)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {
            "client_id": c["client_id"], "redirect_uri": c["redirect_uri"], "response_type": "code",
            "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent select_account",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        }
        if request.args.get("hint"):
            params["login_hint"] = request.args["hint"]
        return redirect(AUTH_URL + "?" + urllib.parse.urlencode(params))

    @app.get("/api/google/callback")
    def g_callback():
        c = config()
        back = c["ui_url"].rstrip("/") + "/settings"
        if request.args.get("error"):
            return redirect(back + "?" + urllib.parse.urlencode({"gmail_error": request.args["error"]}))
        pending = _pending.pop(request.args.get("state", ""), None)
        if not pending:
            return redirect(back + "?gmail_error=" + urllib.parse.quote("Sign-in expired. Click Connect again."))
        try:
            tok = _post_form(TOKEN_URL, {
                "client_id": c["client_id"], "client_secret": c["client_secret"],
                "code": request.args.get("code", ""), "code_verifier": pending[0],
                "grant_type": "authorization_code", "redirect_uri": c["redirect_uri"]})
        except GmailError as e:
            return redirect(back + "?" + urllib.parse.urlencode({"gmail_error": str(e)}))
        granted = tok.get("scope", "")
        if "gmail.compose" not in granted:
            return redirect(back + "?" + urllib.parse.urlencode(
                {"gmail_error": "Gmail permission wasn't granted. Tick the Gmail box on Google's screen."}))
        email = _jwt_email(tok.get("id_token", ""))
        if not email or not tok.get("refresh_token"):
            return redirect(back + "?gmail_error=" + urllib.parse.quote("Google didn't return a usable sign-in. Try again."))
        con = connect()
        try:
            con.execute(
                "INSERT INTO google_accounts (email, refresh_token, access_token, expires_at, scopes, status, connected_at) "
                "VALUES (?,?,?,?,?, 'connected', ?) ON CONFLICT(email) DO UPDATE SET refresh_token=excluded.refresh_token, "
                "access_token=excluded.access_token, expires_at=excluded.expires_at, scopes=excluded.scopes, "
                "status='connected', connected_at=excluded.connected_at",
                (email, tok["refresh_token"], tok["access_token"], time.time() + int(tok.get("expires_in", 3600)),
                 granted, now_iso()))
            accounts = get_setting(con, "gmail_accounts", []) or []
            if email not in accounts:
                core.set_setting(con, "gmail_accounts", accounts + [email])
            con.commit()
        finally:
            con.close()
        return redirect(back + "?" + urllib.parse.urlencode({"gmail_connected": email}))

    @app.post("/api/google/disconnect")
    def g_disconnect():
        email = (request.get_json(force=True).get("email") or "").lower()
        con = connect()
        try:
            row = con.execute("SELECT refresh_token FROM google_accounts WHERE email=?", (email,)).fetchone()
            if row and row["refresh_token"]:
                try:
                    _post_form(REVOKE_URL, {"token": row["refresh_token"]})
                except GmailError:
                    pass  # already revoked/expired - removing locally is what matters
            con.execute("DELETE FROM google_accounts WHERE email=?", (email,))
            con.commit()
            return jsonify({"ok": True, "accounts": _accounts(con)})
        finally:
            con.close()

    @app.post("/api/gmail/drafts")
    def g_drafts():
        data = request.get_json(force=True)
        account = (data.get("account") or "").lower().strip()
        if not account:
            return jsonify({"error": "Pick the Gmail account for this batch (SOP: never assume it)."}), 400
        con = connect()
        try:
            try:
                access_token(con, account)  # fail fast with a clear reconnect message
            except GmailError as e:
                return jsonify({"error": str(e), "reconnect": e.reconnect}), 409 if e.reconnect else 502
            result = draft_batch(con, account, [int(x) for x in data.get("lead_ids") or []] or None,
                                 force=bool(data.get("force")))
            return jsonify(result)
        finally:
            con.close()
