"""
AI Autopilot - research + first-touch drafting through the local Claude / Codex CLIs.

For each lead the worker spawns one headless CLI session (read-only web tools only),
streams its events into ai_run_log (the in-app terminal), then validates the JSON
result server-side and writes it back through the same guard rails as the UI.

Hard limits (SOP 3 / CODEX_HANDOVER):
  * nothing is sent, no form is submitted - the CLIs only get web read tools
  * the AI can move a lead at most to Drafted; Finalize / Contacted / DNC stay human
  * links are stripped from drafts, emails off the official domain are dropped
"""
import os
import re
import json
import shutil
import tempfile
import threading
import subprocess
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from flask import request, jsonify, session

import core
from core import (DEV_CAPABILITY, CONFIDENCE, EDITABLE_FIELDS, AI_FIELDS,
                  connect, now_iso, today, detect_links, apply_lead_updates,
                  transition_stage, get_setting)

ENGINES = {
    "claude": {"label": "Claude Code CLI", "bin": "claude"},
    "codex": {"label": "Codex CLI", "bin": "codex"},
}

# Stages Autopilot may (re)work. Anything later holds approved / sent copy.
WORKABLE_STAGES = ("to_research", "drafted")

SCORE_KEYS = {
    "agency_fit": "sc_agency_fit", "size_maturity": "sc_size_maturity",
    "recent_activity": "sc_recent_activity", "dev_gap": "sc_dev_gap",
    "work_relevance": "sc_work_relevance", "access": "sc_access",
    "personalization": "sc_personalization",
}

# ---------------------------------------------------------------------------
# Result schema - strict (every key required, no extras) so both Claude's
# --json-schema and Codex's --output-schema accept it.
# ---------------------------------------------------------------------------
_STR = {"type": "string"}
_PROPS = {
    "qualification": {"type": "string", "enum": ["contact", "hold", "exclude"]},
    "qualification_reason": _STR,
    "verified_active": {"type": "boolean"},
    "agency_profile": _STR,
    "dev_capability": {"type": "string", "enum": DEV_CAPABILITY},
    "tech_signals": _STR,
    "relevant_work": _STR,
    "decision_maker": _STR,
    "decision_maker_verified": {"type": "boolean"},
    "contact_route": _STR,
    "contact_email": _STR,
    "contact_is_form": {"type": "boolean"},
    "restrictions": _STR,
    "evidence": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "claim": _STR,
                "url": _STR,
                "confidence": {"type": "string", "enum": CONFIDENCE},
            },
            "required": ["claim", "url", "confidence"],
        },
    },
    "opportunity_hypothesis": _STR,
    "pitch_angle": _STR,
    "confidence": {"type": "string", "enum": CONFIDENCE},
    "scores": {
        "type": "object",
        "additionalProperties": False,
        "properties": {k: {"type": "integer", "enum": [0, 1, 2]} for k in SCORE_KEYS},
        "required": list(SCORE_KEYS),
    },
    "subject": _STR,
    "message_draft": _STR,
    "notes_for_owner": _STR,
}
RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": _PROPS,
    "required": list(_PROPS),
}

# ---------------------------------------------------------------------------
# Prompt (SOP 8-13, Appendix C/D/E condensed)
#
# Split in two for prompt caching: RULES is byte-identical for every lead in a run
# (it only depends on Settings), LEAD_PROMPT carries the per-lead facts and goes LAST.
# Claude gets RULES as an appended system prompt (cached by Claude Code across
# sessions); Codex gets RULES first in the input, so the shared prefix is cached.
# Keep anything lead- or date-specific out of RULES, or every lead misses the cache.
# ---------------------------------------------------------------------------
RULES = """\
You are the research and drafting assistant in {sender}'s agency-outreach pipeline.
{sender} is a full-stack developer who offers agencies development help on a small,
paid, clearly scoped basis. Your job: research ONE agency from public pages and, if it
qualifies, write a first-touch email draft. Return ONLY a JSON object (schema enforced).
The agency's details arrive at the end of the input, under "The lead".

## Boundary - never cross
- Read-only. You never send anything, contact anyone, submit forms, log in, or solve CAPTCHAs.
- No guessing. Unknown recipient, person, technology, need or fact -> leave it "" (blank).
  Never invent or pattern-guess an email address (no "hello@" / "info@" unless published).
- Every material claim needs evidence: the exact URL and a confidence level.
  high = a current official page directly supports it; medium = credible public profile;
  low = old/indirect/ambiguous. Only HIGH-confidence facts may appear in the email.
- The lead's Clutch data is a starting point only - re-verify it, don't trust it blindly.

## Research steps (use your web tools on the OFFICIAL site; ~4-10 pages is plenty)
1. Home page: current positioning, services, location. Is the site live and maintained?
2. Work / case studies: pick ONE recent, relevant project (name + type + a concise honest observation).
3. About / Team: size, founder or decision-maker signals (name + role only if clearly published).
4. Services: is development absent, partner-led, partial, or full-service?
5. Careers / Partners / Freelancers pages: collaboration signals and restrictions (region, no contractors,
   no unsolicited outreach).
6. Contact page + footer: the safest public route. An email counts only if it is published on the
   official site and its domain matches the lead's official domain. Form-only -> contact_is_form=true.
7. Optionally the Clutch profile / Behance / Dribbble for recency.

## Field guidance
- dev_capability: none_visible | partner_led | partial | full_service | uncertain.
- tech_signals: only technologies explicitly shown (e.g. "Shopify Partner badge on footer"). Never infer a stack.
- restrictions: region limits, "no contractors", "no unsolicited", form limitations. "" if none found.
- opportunity_hypothesis: ONE sentence, framed as a hypothesis ("They may value ... because ...").
  Never claim they need help.
- pitch_angle: ONE primary offer that matches their signal:
    product/SaaS design, little engineering -> turn approved flows into working React/Next.js features + APIs
    creative website studio -> accurate responsive implementation of approved designs + integrations
    Shopify agency -> custom storefront behaviour: cart, gifting, functions, integrations, backlog support
    ecommerce platform specialist -> checkout, catalog, integration or performance work (Shopify/BigCommerce)
    WordPress / B2B web agency -> custom development, integrations, reliable launch support
    AI / automation studio -> practical interfaces and backend workflows around data and automation
- scores (each 0/1/2, Appendix C):
    agency_fit: 0 not a relevant agency | 1 adjacent service | 2 clear design/product/ecommerce agency
    size_maturity: 0 large/mature with embedded delivery | 1 unknown or mixed | 2 boutique/growing, founder reachable
    recent_activity: 0 inactive/stale | 1 unclear | 2 recent work, posts or case studies
    dev_gap: 0 large visible engineering team | 1 partial/uncertain | 2 strong design, little visible engineering
    work_relevance: 0 no matching work | 1 general digital | 2 SaaS, ecommerce, B2B product or complex web
    access: 0 no legitimate route | 1 general form or inbox | 2 partner/founder/collaboration route
    personalization: 0 nothing specific | 1 one weak detail | 2 current, relevant project or service detail

## Qualification
- contact: real, active design/product/ecommerce/digital agency, fits, has a verified public route,
  and you have one honest, specific, HIGH-confidence observation for the opener.
- hold: potential fit but a material fact or the contact route needs human verification, or it looks like a
  solo freelancer rather than a studio. Say exactly what needs checking in qualification_reason.
- exclude: dead/parked/abandoned site, not an agency, unrelated services, large full-service agency with a big
  in-house engineering team, or an explicit no-contractor / no-solicitation / region restriction.
For hold/exclude you may leave subject and message_draft as "".

## The first-touch email (only when qualification = contact)
Proof library and positioning (pick at most one or two examples that fit the angle; never imply one
project is the whole of their experience; don't default to the same example every time):
{proof_library}

How {sender} works - this MUST be clear in every email:
- {sender} is a FREELANCER based in {location}, working remotely with agencies on a contract / project basis.
  The reader must never mistake the email for a job application. Never use "hire me", "role", "position",
  "join your team", "full-time", "opportunity" or "employment".
- Say it ONCE, in the closing paragraph together with the offer - after the observation and the experience,
  so they read about their own work and the sender's work first. Never in the subject or the opening line. Mention
  {location} plainly and briefly, as a working fact, not an apology or a selling point.
- The offer: {offer}

Structure: subject -> opening with ONE verified observation -> one sentence connecting their work to the
offer -> breadth of experience + at most one or two relevant proof examples -> closing paragraph: freelance
from {location}, contract basis, no long-term commitment, plus one small fixed-price start (one page or
component set, one Shopify feature, one bounded integration, one prototype milestone, a performance
package, or a short audit){guarantee_hint} -> ONE easy question -> sign-off.
- Subject: plain and specific, usually 4-8 words, no hype, no symbols, no urgency.
- Greeting: "Hi [First name]," ONLY when decision_maker_verified is true and the route reaches that person;
  otherwise "Hi [Agency name] team,". Never guess a name.
- 90-160 words. Natural voice of one developer writing to one studio: "I came across", "I liked",
  "I wondered", "Would that ever be useful?". Vary sentence length. Plain text, short paragraphs separated by
  a blank line. No corporate filler ("I hope this finds you well", "leverage", "synergy", "cutting-edge"),
  no flattery paragraph, no technology-stack lists, no pricing, no em dashes.
- ABSOLUTELY NO LINKS: no URLs, no "www", no domain names (write the agency's NAME, never its domain),
  no email addresses, no @handles, no "see my portfolio", no attachments, no calendar links.
  If proof is useful, name the project in words and offer to share examples after a reply.
- End exactly with:
Best,
{sender}

Example structure (vary the wording - never copy):
  Subject: Development support for [Agency]
  Hi [Agency] team,
  I came across your work on [project]. I liked how you handled [honest observation].
  I'm a full-stack developer and I often help turn approved designs into working websites and product
  features. My work covers SaaS products, internal tools, ecommerce and custom integrations, so I can
  usually take on both the interface and the functionality behind it.
  I work freelance from {location}, with agencies on a contract basis, so there's no long-term
  commitment. If extra development capacity is ever useful, I'd be happy to start with one small
  fixed-price feature or page{example_guarantee}
  Would that ever be useful for your team?
  Best,
  {sender}

## Output
Return only the JSON object. Every key is required; use "" / false / [] when unknown.
Put anything {sender} should double-check in notes_for_owner (one or two short sentences).
"""

LEAD_FACT_FIELDS = [
    "company_name", "website", "geography", "employees", "founded", "review_count",
    "rating", "latest_review", "hourly_rate", "min_project_size", "services",
    "clutch_summary", "profile_url", "website_status", "website_issue",
    "social_linkedin", "social_instagram", "social_behance", "social_dribbble",
]


OFFERS = {
    "paid": ("one small, clearly scoped fixed-price task as the first step, so they can see how the work goes.",
             "", "."),
    "paid_guarantee": (
        "one small, clearly scoped fixed-price task as the first step, with a simple guarantee: if they're "
        "not happy with the result, they don't pay. State the guarantee plainly in one short clause - no "
        "\"risk-free\", no \"free\", no hype.",
        " with the guarantee that they don't pay if they're not happy with it",
        ", and if you're not happy with the result, you don't pay."),
}


LEAD_PROMPT = """\
## The lead
Research this agency and return the JSON result.

Official domain: {domain}
Today's date: {today}

Clutch data:
{lead_json}
"""


def build_rules(settings):
    """The shared, cacheable instructions - identical for every lead under the same Settings."""
    offer, guarantee_hint, example_guarantee = OFFERS.get(settings.get("offer_style"), OFFERS["paid_guarantee"])
    return RULES.format(
        location=(settings.get("sender_location") or "").strip(),
        offer=offer,
        guarantee_hint=guarantee_hint,
        example_guarantee=example_guarantee,
        sender=(settings.get("sender_name") or "").strip(),
        proof_library=(settings.get("proof_library") or core.DEFAULT_PROOF_LIBRARY).strip(),
    )


def settings_problem(settings):
    """Autopilot writes in the sender's name - refuse to run until Settings say who that is."""
    missing = [label for key, label in (("sender_name", "your name"), ("sender_location", "your location"),
                                        ("proof_library", "your proof library"))
               if not (settings.get(key) or "").strip()]
    if missing:
        names = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
        return f"Fill in {names} in Settings (Your voice) before running Autopilot."
    if "[" in (settings.get("proof_library") or ""):
        return "Your proof library still has [placeholders]. Replace them with your own projects in Settings."
    return ""


def build_lead_prompt(lead):
    facts = {k: lead.get(k) for k in LEAD_FACT_FIELDS if lead.get(k) not in (None, "")}
    if lead.get("normalized_domain"):
        # Clutch links carry ?utm/ppc tracking - give the model the clean official site.
        facts["website"] = f"https://{lead['normalized_domain']}"
    return LEAD_PROMPT.format(
        domain=lead.get("normalized_domain") or "unknown",
        today=today(),
        lead_json=json.dumps(facts, indent=2, ensure_ascii=False),
    )


def build_prompt(lead, settings):
    """Full single-message prompt (shared rules first, lead last) - what Codex receives."""
    return build_rules(settings) + "\n" + build_lead_prompt(lead)


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
def resolve_cmd(name):
    """Return argv prefix for a CLI, or None. npm .cmd shims are unwrapped to
    `node script.js` - cmd.exe mangles paths containing '&' (like this user dir)."""
    path = shutil.which(name)
    if not path:
        return None
    if path.lower().endswith((".cmd", ".bat")):
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            return None
        m = re.search(r'"%dp0%\\([^"]+\.js)"', txt)
        if not m:
            return None
        base = os.path.dirname(path)
        script = os.path.join(base, m.group(1))
        node = os.path.join(base, "node.exe")
        if not os.path.exists(node):
            node = shutil.which("node")
        return [node, script] if node and os.path.exists(script) else None
    return [path]


_engine_cache = {"at": None, "data": None}


def detect_engines(force=False):
    now = dt.datetime.now()
    if not force and _engine_cache["data"] and (now - _engine_cache["at"]).seconds < 300:
        return _engine_cache["data"]
    out = []
    for eid, meta in ENGINES.items():
        cmd = resolve_cmd(meta["bin"])
        version = ""
        if cmd:
            try:
                p = subprocess.run(cmd + ["--version"], capture_output=True, text=True,
                                   timeout=30, creationflags=_NO_WINDOW)
                version = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else ""
            except Exception as e:  # noqa: BLE001
                version = f"error: {e}"
        out.append({"id": eid, "label": meta["label"], "available": bool(cmd),
                    "version": version, "path": cmd[-1] if cmd else ""})
    _engine_cache.update(at=now, data=out)
    return out


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def build_cmd(engine, workdir, lead, settings, model="", effort=""):
    """Return (argv, codex_last_message_path, stdin_text) for one lead's session."""
    base = resolve_cmd(ENGINES[engine]["bin"])
    if not base:
        raise RuntimeError(f"{ENGINES[engine]['label']} not found on PATH")
    if engine == "claude":
        # Shared rules go in the system prompt so Claude Code's prompt cache reuses them across
        # leads. --exclude-dynamic-system-prompt-sections keeps the per-lead temp cwd out of the
        # system prompt; without it every lead's system prompt differs and nothing is cached.
        rules_path = os.path.join(workdir, "rules.md")
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write(build_rules(settings))
        cmd = base + [
            "-p", "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(RESULT_SCHEMA),
            "--append-system-prompt-file", rules_path,
            "--exclude-dynamic-system-prompt-sections",
            "--tools", "WebFetch", "WebSearch",
            "--allowedTools", "WebFetch", "WebSearch",
            "--strict-mcp-config", "--no-session-persistence",
        ]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd, None, build_lead_prompt(lead)
    # codex
    schema_path = os.path.join(workdir, "schema.json")
    last_path = os.path.join(workdir, "last.json")
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(RESULT_SCHEMA, f)
    cmd = base + ["--search", "exec", "--json", "--skip-git-repo-check", "-s", "read-only",
                  "--output-schema", schema_path, "-o", last_path, "-C", workdir]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    cmd += ["-"]
    # One input, shared rules first: OpenAI caches identical prompt prefixes automatically.
    return cmd, last_path, build_prompt(lead, settings)


# ---------------------------------------------------------------------------
# Stream parsers: one JSON event -> ([(kind, line)], final_result_or_None)
# ---------------------------------------------------------------------------
def _k(n):
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _short(s, n=220):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_claude_event(e):
    lines, result = [], None
    t = e.get("type")
    if t == "system":
        st = e.get("subtype")
        if st == "init":
            lines.append(("info", f"session started · model {e.get('model', '?')} · tools: "
                                  f"{', '.join(x for x in e.get('tools', []) if x != 'StructuredOutput')}"))
        elif st == "permission_denied":
            lines.append(("error", f"permission denied: {e.get('tool_name')} {_short(e.get('message'), 160)}"))
    elif t == "assistant":
        for c in (e.get("message") or {}).get("content") or []:
            ct = c.get("type")
            if ct == "text" and (c.get("text") or "").strip():
                lines.append(("text", _short(c["text"], 400)))
            elif ct == "tool_use":
                name, inp = c.get("name"), c.get("input") or {}
                if name == "StructuredOutput":
                    lines.append(("tool", "→ writing structured result"))
                else:
                    target = inp.get("url") or inp.get("query") or json.dumps(inp)[:160]
                    lines.append(("tool", f"→ {name} {target}"))
    elif t == "user":
        for c in (e.get("message") or {}).get("content") or []:
            if c.get("type") != "tool_result":
                continue
            content = c.get("content")
            if isinstance(content, list):
                content = " ".join(x.get("text", "") for x in content if isinstance(x, dict))
            if "Structured output provided" in str(content):
                continue
            kind = "error" if c.get("is_error") else "info"
            lines.append((kind, ("✗ " if c.get("is_error") else "← ") + _short(content, 200)))
    elif t == "rate_limit_event":
        info = e.get("rate_limit_info") or {}
        if info.get("status") not in (None, "allowed"):
            lines.append(("error", f"rate limit: {info.get('status')} ({info.get('rateLimitType')})"))
    elif t == "result":
        secs = (e.get("duration_ms") or 0) / 1000
        cost = e.get("total_cost_usd")
        u = e.get("usage") or {}
        tokens = ""
        if u:
            fresh = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            tokens = (f" · input {_k(fresh)} new + {_k(u.get('cache_read_input_tokens'))} cached"
                      f" · output {_k(u.get('output_tokens'))}")
        summary = f"finished in {secs:.0f}s · {e.get('num_turns', '?')} turns{tokens}" + \
                  (f" · ${cost:.2f}" if isinstance(cost, (int, float)) else "")
        if e.get("is_error") or e.get("subtype") != "success":
            lines.append(("error", f"{summary} · {e.get('subtype')} {_short(e.get('result'), 200)}"))
        else:
            lines.append(("result", summary))
        so = e.get("structured_output")
        if isinstance(so, dict):
            result = so
        elif e.get("result"):
            result = _extract_json(e["result"])
    return lines, result


def parse_codex_event(e):
    lines, result = [], None
    t = e.get("type")
    item = e.get("item") or {}
    it = item.get("type")
    if t == "thread.started":
        lines.append(("info", "session started"))
    elif t == "item.completed" and it == "web_search":
        action = item.get("action") or {}
        target = action.get("url") or action.get("query") or item.get("query") or ""
        verb = {"open_page": "open", "search": "search", "find_in_page": "find in page"}.get(action.get("type"), "browse")
        lines.append(("tool", f"→ web {verb} {_short(target, 180)}".rstrip()))
    elif t == "item.started" and it == "command_execution":
        lines.append(("tool", f"$ {_short(item.get('command'), 180)}"))
    elif t == "item.completed" and it == "command_execution":
        if item.get("exit_code") not in (0, None):
            lines.append(("stderr", f"exit {item.get('exit_code')}: {_short(item.get('aggregated_output'), 160)}"))
    elif t == "item.completed" and it == "reasoning":
        if (item.get("text") or "").strip():
            lines.append(("think", _short(item["text"], 300)))
    elif t == "item.completed" and it == "agent_message":
        text = item.get("text") or ""
        parsed = _extract_json(text)
        if parsed and "qualification" in parsed:
            result = parsed
            lines.append(("tool", "→ writing structured result"))
        elif text.strip():
            lines.append(("text", _short(text, 400)))
    elif t == "item.completed" and it == "error":
        lines.append(("error", _short(item.get("message"), 300)))
    elif t == "turn.completed":
        u = e.get("usage") or {}
        total_in, cached = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0
        lines.append(("result", f"finished · input {_k(total_in - cached)} new + {_k(cached)} cached"
                                f" · output {_k(u.get('output_tokens'))}"))
    elif t in ("turn.failed", "error"):
        msg = (e.get("error") or {}).get("message") if isinstance(e.get("error"), dict) else e.get("message")
        lines.append(("error", _short(msg or json.dumps(e), 300)))
    return lines, result


PARSERS = {"claude": parse_claude_event, "codex": parse_codex_event}


def _extract_json(text):
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Validation + write-back (never trust the model)
# ---------------------------------------------------------------------------
def _strip_links(text):
    for m in sorted({x["match"] for x in detect_links(text)}, key=len, reverse=True):
        text = text.replace(m, "")
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _email_ok(email, domain):
    if not email or "@" not in email or not domain:
        return False
    host = email.rsplit("@", 1)[1].lower().strip()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def _norm_email(email):
    email = (email or "").strip().strip("<>").replace("mailto:", "")
    if "@" not in email:
        return email
    local, host = email.rsplit("@", 1)
    return f"{local}@{host.lower()}"


JOB_LANGUAGE = re.compile(r"\b(full[- ]time|position|employment|join your team|hire me|job opening)\b", re.I)


def check_terms(body, settings):
    """Flags for SOP-required working terms missing from a draft (freelance, location, guarantee)."""
    flags = []
    if not re.search(r"\b(freelanc\w*|contract)\b", body, re.I):
        flags.append("freelance_missing")
    loc = (settings.get("sender_location") or "").strip()
    if loc and loc.lower() not in body.lower():
        flags.append("location_missing")
    if settings.get("offer_style", "paid_guarantee") == "paid_guarantee" and not re.search(
            r"(don'?t|do not|won'?t|no need to) (have to )?pay|not (be )?charged", body, re.I):
        flags.append("guarantee_missing")
    if JOB_LANGUAGE.search(body):
        flags.append("job_language")
    return flags


def validate_result(lead, res, engine, settings=None):
    """Turn a raw model result into (updates, ai_meta). Pure - no DB access."""
    settings = settings or core.DEFAULT_SETTINGS
    flags = []
    qual = res.get("qualification") if res.get("qualification") in ("contact", "hold", "exclude") else "hold"
    reason = (res.get("qualification_reason") or "").strip()

    subject = (res.get("subject") or "").strip()
    body = (res.get("message_draft") or "").strip().replace("\r\n", "\n")
    if detect_links(subject, body):
        subject, body = _strip_links(subject), _strip_links(body)
        flags.append("link_removed")
        if detect_links(subject, body):
            flags.append("link_still_present")

    words = len(body.split())
    if body and not (70 <= words <= 190):
        flags.append("length")
    if body and qual == "contact":
        flags += check_terms(body, settings)

    email = _norm_email(res.get("contact_email"))
    domain = lead.get("normalized_domain") or ""
    if email and not _email_ok(email, domain):
        flags.append("email_domain_mismatch")
        email = ""
    is_form = bool(res.get("contact_is_form")) and not email

    evidence = [x for x in (res.get("evidence") or [])
                if isinstance(x, dict) and str(x.get("url", "")).startswith("http") and x.get("claim")]
    if not evidence:
        flags.append("no_evidence")
    conf = res.get("confidence") if res.get("confidence") in CONFIDENCE else "low"
    if conf == "low":
        flags.append("low_confidence")

    ev_lines = [f"AI research · {engine} · accessed {today()}"]
    for x in evidence:
        ev_lines.append(f"- [{str(x.get('confidence', 'low')).upper()}] {x['claim'].strip()} — {x['url'].strip()}")
    if (res.get("notes_for_owner") or "").strip():
        ev_lines.append(f"Note: {res['notes_for_owner'].strip()}")

    route = (res.get("contact_route") or "").strip()
    has_route = bool(email or is_form or route)
    if qual == "contact" and not has_route:
        qual, reason = "hold", (reason + "; " if reason else "") + "no verified contact route"
    if qual == "contact" and (not subject or not body):
        qual, reason = "hold", (reason + "; " if reason else "") + "no draft produced"

    scores = res.get("scores") or {}
    updates = {
        "agency_profile": (res.get("agency_profile") or "").strip(),
        "dev_capability": res.get("dev_capability") if res.get("dev_capability") in DEV_CAPABILITY else "uncertain",
        "tech_signals": (res.get("tech_signals") or "").strip(),
        "relevant_work": (res.get("relevant_work") or "").strip(),
        "decision_maker": (res.get("decision_maker") or "").strip() if res.get("decision_maker_verified") else "",
        "contact_route": route,
        "contact_email": email,
        "contact_is_form": 1 if is_form else 0,
        "restrictions": (res.get("restrictions") or "").strip(),
        "evidence_notes": "\n".join(ev_lines),
        "opportunity_hypothesis": (res.get("opportunity_hypothesis") or "").strip(),
        "pitch_angle": (res.get("pitch_angle") or "").strip(),
        "confidence": conf,
    }
    for k, col in SCORE_KEYS.items():
        v = scores.get(k)
        if isinstance(v, int) and 0 <= v <= 2:
            updates[col] = v
    if qual == "contact" or (subject and body and qual != "exclude"):
        updates["subject"] = subject
        updates["message_draft"] = body

    if qual == "contact":
        updates["next_action"] = "Review AI draft & finalize"
    elif qual == "hold":
        updates["next_action"] = f"Verify: {reason}"[:200] if reason else "Verify before drafting"
    else:
        updates["next_action"] = "Review AI exclude suggestion"
    if "email_domain_mismatch" in flags:
        updates["next_action"] = "Verify recipient (AI email was off-domain)"

    soft = [f for f in flags if f != "length"]
    meta = {
        "qualification": qual,
        "reason": reason,
        "flags": flags,
        "ai_status": "needs_review" if (qual != "contact" or soft) else "done",
    }
    return updates, meta


def apply_result(con, lead, res, engine, run_id):
    """Validate and persist; returns (item_status, outcome). Commits."""
    updates, meta = validate_result(lead, res, engine, core.all_settings(con))
    updates.update({
        "ai_status": meta["ai_status"],
        "ai_engine": engine,
        "ai_last_run": now_iso(),
        "ai_flags": meta["flags"],
        "ai_suggestion": None if meta["qualification"] == "contact" else meta["qualification"],
        "ai_suggestion_reason": None if meta["qualification"] == "contact" else meta["reason"],
    })
    apply_lead_updates(con, lead["id"], updates, actor="ai", allowed=EDITABLE_FIELDS + AI_FIELDS)
    outcome = ""
    if meta["qualification"] == "contact":
        if lead["stage"] == "to_research":
            payload, code = transition_stage(con, lead["id"], {
                "stage": "drafted", "note": f"Autopilot run #{run_id} ({engine})"}, actor="ai")
            outcome = "Drafted" if code == 200 else f"Draft saved (stage: {payload.get('error')})"
        else:
            outcome = "Draft refreshed"
    else:
        outcome = f"{meta['qualification'].capitalize()}: {meta['reason'] or 'see notes'}"
    if meta["flags"]:
        outcome += " · flags: " + ", ".join(meta["flags"])
    con.commit()
    return meta["ai_status"], outcome[:300]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_procs = {}          # item_id -> Popen
_cancelled = set()   # run ids


def _log(con, run_id, item_id, kind, line):
    con.execute("INSERT INTO ai_run_log (run_id, item_id, ts, kind, line) VALUES (?,?,?,?,?)",
                (run_id, item_id, dt.datetime.now().strftime("%H:%M:%S"), kind, line))
    con.commit()


def _kill_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    else:
        proc.kill()


def _set_item(con, item_id, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE ai_run_items SET {sets} WHERE id=?", list(kw.values()) + [item_id])
    con.commit()


def _set_lead_ai_status(con, lead_id, status):
    con.execute("UPDATE leads SET ai_status=? WHERE id=?", (status, lead_id))
    con.commit()


def _process_item(run, item):
    run_id, item_id, lead_id = run["id"], item["id"], item["lead_id"]
    engine = run["engine"]
    con = connect()
    workdir = None
    try:
        if run_id in _cancelled:
            _set_item(con, item_id, status="cancelled", finished_at=now_iso(), outcome="Cancelled")
            _set_lead_ai_status(con, lead_id, "none")
            return "cancelled"
        lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        settings = core.all_settings(con)
        timeout = int(settings.get("timeout_min") or 8) * 60

        _set_item(con, item_id, status="running", started_at=now_iso())
        _set_lead_ai_status(con, lead_id, "running")
        _log(con, run_id, item_id, "info",
             f"▶ {lead['company_name']} · {lead.get('normalized_domain') or lead.get('website') or 'no website'} · "
             f"{lead.get('review_count') or 0} reviews")

        workdir = tempfile.mkdtemp(prefix=f"crm_ai_{run_id}_{item_id}_")
        cmd, last_path, stdin_text = build_cmd(engine, workdir, lead, settings,
                                               run.get("model") or "", run.get("effort") or "")
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow nesting when the API itself was started from Claude Code
        proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                env=env, creationflags=_NO_WINDOW)
        with _lock:
            _procs[item_id] = proc
        proc.stdin.write(stdin_text)
        proc.stdin.close()

        def pump_stderr():
            c2 = connect()
            try:
                for ln in proc.stderr:
                    ln = ln.rstrip()
                    if ln and "Reading prompt from stdin" not in ln:
                        _log(c2, run_id, item_id, "stderr", _short(ln, 300))
            finally:
                c2.close()
        t_err = threading.Thread(target=pump_stderr, daemon=True)
        t_err.start()
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            _kill_tree(proc)
        timer = threading.Timer(timeout, on_timeout)
        timer.start()

        parse = PARSERS[engine]
        result = None
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                _log(con, run_id, item_id, "text", _short(raw, 300))
                continue
            lines, res = parse(ev)
            for kind, line in lines:
                _log(con, run_id, item_id, kind, line)
            if res:
                result = res
        proc.wait()
        timer.cancel()
        t_err.join(timeout=5)

        if result is None and last_path and os.path.exists(last_path):
            with open(last_path, encoding="utf-8", errors="replace") as f:
                result = _extract_json(f.read())

        if run_id in _cancelled:
            _set_item(con, item_id, status="cancelled", finished_at=now_iso(), outcome="Cancelled")
            _set_lead_ai_status(con, lead_id, "none")
            _log(con, run_id, item_id, "error", "■ cancelled")
            return "cancelled"
        if not result:
            err = (f"timed out after {timeout // 60} min" if timed_out.is_set()
                   else f"no result (exit {proc.returncode})")
            raise RuntimeError(err)

        lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        status, outcome = apply_result(con, lead, result, engine, run_id)
        _set_item(con, item_id, status=status, finished_at=now_iso(), outcome=outcome,
                  result_json=json.dumps(result, ensure_ascii=False))
        _log(con, run_id, item_id, "result", f"✔ {lead['company_name']}: {outcome}")
        return status
    except Exception as e:  # noqa: BLE001
        _set_item(con, item_id, status="failed", finished_at=now_iso(), error=str(e)[:500],
                  outcome="Failed")
        _set_lead_ai_status(con, lead_id, "failed")
        core.log_event(con, lead_id, "ai", f"Autopilot run #{run_id} failed: {str(e)[:200]}")
        con.commit()
        _log(con, run_id, item_id, "error", f"✗ failed: {e}")
        return "failed"
    finally:
        with _lock:
            _procs.pop(item_id, None)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        con.close()


def _run_thread(run_id):
    con = connect()
    try:
        run = dict(con.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)).fetchone())
        items = [dict(r) for r in con.execute(
            "SELECT * FROM ai_run_items WHERE run_id=? ORDER BY id", (run_id,))]
        con.execute("UPDATE ai_runs SET status='running' WHERE id=?", (run_id,))
        con.commit()
        _log(con, run_id, None, "info",
             f"Autopilot run #{run_id} · {ENGINES[run['engine']]['label']}"
             f"{' · ' + run['model'] if run.get('model') else ''} · {len(items)} lead(s) · "
             f"{run['concurrency']} at a time")

        def work(item):
            status = _process_item(run, item)
            c = connect()
            try:
                col = "failed" if status == "failed" else "done"
                c.execute(f"UPDATE ai_runs SET {col}={col}+1 WHERE id=?", (run_id,))
                c.commit()
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=run["concurrency"] or 1) as ex:
            list(ex.map(work, items))

        final = "cancelled" if run_id in _cancelled else "done"
        con.execute("UPDATE ai_runs SET status=?, finished_at=? WHERE id=?", (final, now_iso(), run_id))
        con.commit()
        r = con.execute("SELECT done, failed, total FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        _log(con, run_id, None, "result",
             f"■ run #{run_id} {final}: {r['done']} processed, {r['failed']} failed of {r['total']}")
    except Exception as e:  # noqa: BLE001
        con.execute("UPDATE ai_runs SET status='failed', finished_at=? WHERE id=?", (now_iso(), run_id))
        con.commit()
        _log(con, run_id, None, "error", f"run crashed: {e}")
    finally:
        _cancelled.discard(run_id)
        con.close()


def start_run(con, engine, lead_ids, model="", effort="", concurrency=2):
    cur = con.execute(
        "INSERT INTO ai_runs (engine, model, effort, status, concurrency, total, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (engine, model, effort, "queued", concurrency, len(lead_ids), now_iso()))
    run_id = cur.lastrowid
    for lid in lead_ids:
        con.execute("INSERT INTO ai_run_items (run_id, lead_id, status) VALUES (?,?,?)",
                    (run_id, lid, "queued"))
        con.execute("UPDATE leads SET ai_status='queued' WHERE id=?", (lid,))
    con.commit()
    threading.Thread(target=_run_thread, args=(run_id,), daemon=True).start()
    return run_id


def cancel_run(run_id):
    _cancelled.add(run_id)
    con = connect()
    try:
        item_ids = [r["id"] for r in con.execute(
            "SELECT id FROM ai_run_items WHERE run_id=? AND status IN ('running','queued')", (run_id,))]
    finally:
        con.close()
    with _lock:
        procs = [_procs[i] for i in item_ids if i in _procs]
    for p in procs:
        _kill_tree(p)


def recover_interrupted():
    """Runs that were in flight when the server stopped can't resume - close them out."""
    con = connect()
    try:
        con.execute("UPDATE ai_run_items SET status='failed', error='interrupted (server restarted)', "
                    "finished_at=? WHERE status IN ('queued','running')", (now_iso(),))
        con.execute("UPDATE ai_runs SET status='failed', finished_at=? WHERE status IN ('queued','running')",
                    (now_iso(),))
        con.execute("UPDATE leads SET ai_status='failed' WHERE ai_status IN ('queued','running')")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def _pick_leads(con, count):
    user_id = session.get("user_id")
    owner_sql = "AND (owner_user_id IS NULL OR owner_user_id=?) " if user_id is not None else ""
    params = ([user_id] if user_id is not None else []) + [count]
    return [r["id"] for r in con.execute(
        "SELECT id FROM leads WHERE stage='to_research' "
        "AND (ai_status IS NULL OR ai_status IN ('none','failed')) "
        "AND (ai_suggestion IS NULL OR ai_suggestion='') "
        "AND website IS NOT NULL AND website<>'' "
        f"{owner_sql}"
        "ORDER BY review_count DESC, score_total DESC NULLS LAST LIMIT ?", params)]


def _run_dict(con, r):
    d = dict(r)
    d["running"] = con.execute(
        "SELECT COUNT(*) n FROM ai_run_items WHERE run_id=? AND status='running'", (r["id"],)).fetchone()["n"]
    return d


def register(app):
    @app.get("/api/autopilot/engines")
    def ap_engines():
        engines = detect_engines(force=request.args.get("refresh") == "1")
        con = connect()
        try:
            default = get_setting(con, "default_engine")
        finally:
            con.close()
        return jsonify({"engines": engines, "default": default})

    @app.post("/api/autopilot/runs")
    def ap_start():
        data = request.get_json(force=True)
        engine = data.get("engine")
        if engine not in ENGINES:
            return jsonify({"error": "engine must be 'claude' or 'codex'"}), 400
        if not resolve_cmd(ENGINES[engine]["bin"]):
            return jsonify({"error": f"{ENGINES[engine]['label']} is not installed / not on PATH"}), 400
        con = connect()
        try:
            problem = settings_problem(core.all_settings(con))
            if problem:
                return jsonify({"error": problem}), 400
            concurrency = max(1, min(4, int(data.get("concurrency") or get_setting(con, "concurrency") or 2)))
            if data.get("lead_ids"):
                ids = [int(x) for x in data["lead_ids"]]
                rows = con.execute(
                    f"SELECT id, stage, ai_status, owner_user_id FROM leads WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
                user_id = session.get("user_id")
                lead_ids = [r["id"] for r in rows
                            if r["stage"] in WORKABLE_STAGES
                            and r["ai_status"] not in ("queued", "running")
                            and (r["owner_user_id"] is None or r["owner_user_id"] == user_id)]
                skipped = len(ids) - len(lead_ids)
            else:
                lead_ids = _pick_leads(con, max(1, min(100, int(data.get("count") or 10))))
                skipped = 0
            if not lead_ids:
                return jsonify({"error": "No eligible leads (only To research / Drafted leads that aren't "
                                         "already queued can go through Autopilot)."}), 422
            run_id = start_run(con, engine, lead_ids,
                               model=(data.get("model") or get_setting(con, "default_model") or "").strip(),
                               effort=(data.get("effort") or get_setting(con, "default_effort") or "").strip(),
                               concurrency=concurrency)
        finally:
            con.close()
        return jsonify({"run_id": run_id, "queued": len(lead_ids), "skipped": skipped})

    @app.get("/api/autopilot/runs")
    def ap_runs():
        con = connect()
        try:
            runs = [_run_dict(con, r) for r in con.execute(
                "SELECT * FROM ai_runs ORDER BY id DESC LIMIT ?", (int(request.args.get("limit", 30)),))]
        finally:
            con.close()
        return jsonify({"runs": runs})

    @app.get("/api/autopilot/status")
    def ap_status():
        con = connect()
        try:
            active = [_run_dict(con, r) for r in con.execute(
                "SELECT * FROM ai_runs WHERE status IN ('queued','running') ORDER BY id")]
        finally:
            con.close()
        return jsonify({"active": active,
                        "done": sum(r["done"] + r["failed"] for r in active),
                        "total": sum(r["total"] for r in active)})

    @app.get("/api/autopilot/runs/<int:run_id>")
    def ap_run(run_id):
        con = connect()
        try:
            r = con.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)).fetchone()
            if not r:
                return jsonify({"error": "not found"}), 404
            run = _run_dict(con, r)
            run["items"] = [dict(x) for x in con.execute(
                "SELECT i.id, i.lead_id, i.status, i.started_at, i.finished_at, i.outcome, i.error, "
                "l.company_name, l.website, l.review_count, l.stage, l.ai_flags "
                "FROM ai_run_items i JOIN leads l ON l.id=i.lead_id WHERE i.run_id=? ORDER BY i.id", (run_id,))]
        finally:
            con.close()
        return jsonify(run)

    @app.get("/api/autopilot/runs/<int:run_id>/log")
    def ap_log(run_id):
        after = int(request.args.get("after", 0))
        item = request.args.get("item")
        sql = ("SELECT g.id, g.item_id, g.ts, g.kind, g.line, l.company_name FROM ai_run_log g "
               "LEFT JOIN ai_run_items i ON i.id=g.item_id LEFT JOIN leads l ON l.id=i.lead_id "
               "WHERE g.run_id=? AND g.id>?")
        params = [run_id, after]
        if item:
            sql += " AND (g.item_id=? OR g.item_id IS NULL)"
            params.append(int(item))
        sql += " ORDER BY g.id LIMIT 1000"
        con = connect()
        try:
            lines = [dict(x) for x in con.execute(sql, params)]
            status = con.execute("SELECT status FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            con.close()
        return jsonify({"lines": lines, "status": status["status"] if status else None})

    @app.post("/api/autopilot/runs/<int:run_id>/cancel")
    def ap_cancel(run_id):
        cancel_run(run_id)
        return jsonify({"ok": True})
