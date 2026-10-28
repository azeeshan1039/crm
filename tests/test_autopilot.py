"""Run:  python -m unittest discover -s tests   (from the crm folder)"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import autopilot  # noqa: E402


GOOD_RESULT = {
    "qualification": "contact",
    "qualification_reason": "",
    "verified_active": True,
    "agency_profile": "Boutique brand + web studio, 5 people.",
    "dev_capability": "partner_led",
    "tech_signals": "Webflow mentioned on services page",
    "relevant_work": "Rebrand + site for a fintech startup (2026).",
    "decision_maker": "Jane Doe, Founder",
    "decision_maker_verified": True,
    "contact_route": "Email on contact page",
    "contact_email": "Hello@ACME.com",
    "contact_is_form": False,
    "restrictions": "",
    "evidence": [{"claim": "Founded by Jane Doe", "url": "https://acme.com/about", "confidence": "high"}],
    "opportunity_hypothesis": "They may value implementation help because dev is partner-led.",
    "pitch_angle": "Design to development",
    "confidence": "high",
    "scores": {"agency_fit": 2, "size_maturity": 2, "recent_activity": 2, "dev_gap": 2,
               "work_relevance": 1, "access": 2, "personalization": 2},
    "subject": "Development support for Acme",
    "message_draft": (
        "Hi Jane,\n\nI came across your rebrand for the fintech startup and liked how calm the product pages feel. "
        "I'm a full-stack developer and I often help agencies turn approved designs into working websites and "
        "product features, across SaaS, ecommerce and custom integrations, so I can usually take on both the "
        "interface and the functionality behind it.\n\nI work freelance from Portugal, with agencies on a "
        "contract basis, so there's no long-term commitment. If extra development capacity is ever useful, "
        "I'd be happy to start with one small fixed-price page, and if you're not happy with the result, you "
        "don't pay.\n\nWould that ever be useful for your team?\n\nBest,\nAlex Example"),
    "notes_for_owner": "",
}

# Stand-in for the owner's Settings (a fresh install ships them blank).
SETTINGS = dict(core.DEFAULT_SETTINGS, sender_name="Alex Example", sender_location="Portugal",
                proof_library="Positioning: full-stack developer.\n- Shopify agency -> Example Store (cart work)")

LEAD = {"id": 1, "company_name": "Acme", "normalized_domain": "acme.com", "stage": "to_research"}


class ParserTests(unittest.TestCase):
    def test_claude_tool_and_result(self):
        lines, res = autopilot.parse_claude_event({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://acme.com", "prompt": "x"}}]}})
        self.assertEqual(lines, [("tool", "→ WebFetch https://acme.com")])
        self.assertIsNone(res)

        lines, res = autopilot.parse_claude_event({
            "type": "result", "subtype": "success", "is_error": False, "duration_ms": 6000,
            "num_turns": 3, "total_cost_usd": 0.04, "result": "{}", "structured_output": {"title": "x"}})
        self.assertEqual(res, {"title": "x"})
        self.assertEqual(lines[0][0], "result")

    def test_claude_permission_denied_is_error(self):
        lines, _ = autopilot.parse_claude_event(
            {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "message": "nope"})
        self.assertEqual(lines[0][0], "error")

    def test_codex_web_search_and_final_json(self):
        lines, _ = autopilot.parse_codex_event({"type": "item.completed", "item": {
            "type": "web_search", "query": "https://acme.com",
            "action": {"type": "open_page", "url": "https://acme.com"}}})
        self.assertEqual(lines, [("tool", "→ web open https://acme.com")])

        import json
        _, res = autopilot.parse_codex_event({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(GOOD_RESULT)}})
        self.assertEqual(res["qualification"], "contact")

        # a progress message that happens to be JSON-ish but not the result
        lines, res = autopilot.parse_codex_event({"type": "item.completed", "item": {
            "type": "agent_message", "text": "I'll open the page."}})
        self.assertIsNone(res)
        self.assertEqual(lines[0][0], "text")


class ValidateTests(unittest.TestCase):
    def test_good_result(self):
        updates, meta = autopilot.validate_result(LEAD, dict(GOOD_RESULT), "claude")
        self.assertEqual(meta["qualification"], "contact")
        self.assertEqual(meta["ai_status"], "done")
        self.assertEqual(updates["contact_email"], "Hello@acme.com")  # SOP 7.1: lowercase domain only
        self.assertEqual(updates["sc_access"], 2)
        self.assertIn("https://acme.com/about", updates["evidence_notes"])

    def test_links_are_stripped_and_flagged(self):
        r = dict(GOOD_RESULT, message_draft="Hi team, see https://me.dev/work and www.me.io or me.com. "
                                            + " ".join(["word"] * 100))
        updates, meta = autopilot.validate_result(LEAD, r, "claude")
        self.assertIn("link_removed", meta["flags"])
        self.assertEqual(core.detect_links(updates["message_draft"]), [])
        self.assertEqual(meta["ai_status"], "needs_review")

    def test_off_domain_email_dropped(self):
        r = dict(GOOD_RESULT, contact_email="jane@gmail.com")
        updates, meta = autopilot.validate_result(LEAD, r, "codex")
        self.assertEqual(updates["contact_email"], "")
        self.assertIn("email_domain_mismatch", meta["flags"])

    def test_subdomain_email_ok(self):
        updates, _ = autopilot.validate_result(LEAD, dict(GOOD_RESULT, contact_email="a@mail.acme.com"), "claude")
        self.assertEqual(updates["contact_email"], "a@mail.acme.com")

    def test_contact_without_route_becomes_hold(self):
        r = dict(GOOD_RESULT, contact_email="", contact_is_form=False, contact_route="")
        _, meta = autopilot.validate_result(LEAD, r, "claude")
        self.assertEqual(meta["qualification"], "hold")
        self.assertIn("no verified contact route", meta["reason"])

    def test_contact_without_draft_becomes_hold(self):
        _, meta = autopilot.validate_result(LEAD, dict(GOOD_RESULT, message_draft=""), "claude")
        self.assertEqual(meta["qualification"], "hold")

    def test_unverified_decision_maker_blanked(self):
        updates, _ = autopilot.validate_result(LEAD, dict(GOOD_RESULT, decision_maker_verified=False), "claude")
        self.assertEqual(updates["decision_maker"], "")

    def test_terms_present_no_flags(self):
        self.assertEqual(autopilot.check_terms(GOOD_RESULT["message_draft"], SETTINGS), [])

    def test_missing_freelance_location_guarantee_flagged(self):
        body = "Hi team,\n\nI'm a developer. Happy to start with a small paid task.\n\nBest,\nAlex"
        flags = autopilot.check_terms(body, SETTINGS)
        for f in ("freelance_missing", "location_missing", "guarantee_missing"):
            self.assertIn(f, flags)

    def test_guarantee_not_required_for_plain_paid(self):
        body = "I work freelance from Portugal on a contract basis. Happy to start with one fixed-price task."
        settings = dict(SETTINGS, offer_style="paid")
        self.assertEqual(autopilot.check_terms(body, settings), [])

    def test_job_language_flagged(self):
        body = "I work freelance from Portugal and would love a full-time position. You don't pay if unhappy."
        self.assertIn("job_language", autopilot.check_terms(body, SETTINGS))

    def test_missing_terms_make_draft_need_review(self):
        r = dict(GOOD_RESULT, message_draft="Hi Acme team,\n\n" + " ".join(["word"] * 110) + "\n\nBest,\nAlex")
        _, meta = autopilot.validate_result(LEAD, r, "claude")
        self.assertEqual(meta["qualification"], "contact")
        self.assertEqual(meta["ai_status"], "needs_review")
        self.assertIn("freelance_missing", meta["flags"])

    def test_prompt_carries_terms(self):
        p = autopilot.build_prompt({"company_name": "Acme", "normalized_domain": "acme.com"}, SETTINGS)
        self.assertIn("FREELANCER based in Portugal", p)
        self.assertIn("you don't pay", p)
        self.assertNotIn("{", p.split("## The lead")[0])  # no unformatted placeholders in the rules

    def test_rules_identical_across_leads_for_caching(self):
        a = {"company_name": "Acme", "normalized_domain": "acme.com", "review_count": 10}
        b = {"company_name": "Beta Studio", "normalized_domain": "beta.studio", "review_count": 3}
        rules = autopilot.build_rules(SETTINGS)
        self.assertEqual(rules, autopilot.build_rules(dict(SETTINGS)))
        for lead in (a, b):
            self.assertTrue(autopilot.build_prompt(lead, SETTINGS).startswith(rules))
        for leaky in ("acme", "beta", core.today()):
            self.assertNotIn(leaky, rules.lower())
        self.assertIn("Official domain: acme.com", autopilot.build_lead_prompt(a))

    def test_claude_cmd_puts_rules_in_system_prompt(self):
        if not autopilot.resolve_cmd("claude"):
            self.skipTest("claude CLI not installed")
        tmp = tempfile.mkdtemp()
        try:
            lead = {"company_name": "Acme", "normalized_domain": "acme.com"}
            cmd, _, stdin = autopilot.build_cmd("claude", tmp, lead, SETTINGS)
            self.assertIn("--exclude-dynamic-system-prompt-sections", cmd)
            rules_file = cmd[cmd.index("--append-system-prompt-file") + 1]
            with open(rules_file, encoding="utf-8") as f:
                self.assertEqual(f.read(), autopilot.build_rules(SETTINGS))
            self.assertTrue(stdin.startswith("## The lead"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_blank_sender_settings_block_autopilot(self):
        self.assertIn("your name", autopilot.settings_problem(core.DEFAULT_SETTINGS))
        self.assertIn("placeholders", autopilot.settings_problem(
            dict(SETTINGS, proof_library=core.DEFAULT_PROOF_LIBRARY)))
        self.assertEqual(autopilot.settings_problem(SETTINGS), "")

    def test_exclude_keeps_no_draft(self):
        r = dict(GOOD_RESULT, qualification="exclude", qualification_reason="dead site")
        updates, meta = autopilot.validate_result(LEAD, r, "claude")
        self.assertNotIn("message_draft", updates)
        self.assertEqual(meta["ai_status"], "needs_review")


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig = core.DB_PATH
        src = os.path.join(os.path.dirname(self.orig), "crm.db")
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        if os.path.exists(src):
            shutil.copy(src, core.DB_PATH)

    def tearDown(self):
        core.DB_PATH = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_lead(self, con):
        cur = con.execute("INSERT INTO leads (company_name, normalized_domain, website, stage) "
                          "VALUES ('Acme','acme.com','https://acme.com','to_research')")
        con.commit()
        return cur.lastrowid

    def test_migration_idempotent(self):
        core.init_db()
        core.init_db()
        con = core.connect()
        cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
        con.close()
        for c, _ in core.LEAD_MIGRATIONS:
            self.assertIn(c, cols)

    def test_ai_can_only_reach_drafted(self):
        core.init_db()
        con = core.connect()
        lid = self._new_lead(con)
        for stage in ("finalized", "contacted", "do_not_contact", "archived", "won"):
            payload, code = core.transition_stage(con, lid, {"stage": stage, "confirm_sent": True,
                                                             "suppression_reason": "x"}, actor="ai")
            self.assertEqual(code, 403, stage)
        con.close()

    def test_apply_result_drafts_lead(self):
        core.init_db()
        con = core.connect()
        lid = self._new_lead(con)
        lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone())
        status, outcome = autopilot.apply_result(con, lead, dict(GOOD_RESULT), "claude", 1)
        row = con.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
        con.close()
        self.assertEqual(row["stage"], "drafted")
        self.assertEqual(status, "done")
        self.assertTrue(outcome.startswith("Drafted"))
        self.assertEqual(row["score_total"], 13)
        self.assertEqual(row["ai_engine"], "claude")


if __name__ == "__main__":
    unittest.main()
