"""Run:  python -m unittest tests.test_mcp   (from the crm folder)

Uses a temporary SQLite file even when crm/.env points at Neon.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import mcp_server  # noqa: E402


class McpLeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig = (core.USING_POSTGRES, core.DATABASE_URL, core.DB_PATH, os.environ.get("MCP_USER_EMAIL"))
        core.USING_POSTGRES = False
        core.DATABASE_URL = ""
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        os.environ.pop("MCP_USER_EMAIL", None)
        core.init_db()

    def tearDown(self):
        core.USING_POSTGRES, core.DATABASE_URL, core.DB_PATH, email = self.orig
        if email is None:
            os.environ.pop("MCP_USER_EMAIL", None)
        else:
            os.environ["MCP_USER_EMAIL"] = email
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_user(self, name, email):
        db = core.connect()
        try:
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash, created_at) VALUES (?,?,?,?)",
                (name, email, "x", core.now_iso()),
            )
            db.commit()
            return cur.lastrowid
        finally:
            db.close()

    def test_single_user_can_create_update_and_list(self):
        self.add_user("Alex Example", "alex@example.com")
        created = json.loads(mcp_server.create_lead(
            "Acme Studio",
            assignee_email="alex@example.com",
            website="https://acme.example",
            geography="Lisbon",
            campaign="chatgpt",
        ))
        self.assertIn("id", created)
        self.assertEqual(created["stage"], "to_research")

        updated = json.loads(mcp_server.update_lead(created["id"], {"contact_email": "hi@acme.example", "subject": "Hello"}))
        self.assertTrue(updated["ok"])

        listed = json.loads(mcp_server.list_leads(q="Acme"))
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["leads"][0]["campaign"], "chatgpt")
        self.assertEqual(listed["leads"][0]["geography"], "Lisbon")

        full = json.loads(mcp_server.get_lead(created["id"]))
        self.assertEqual(full["contact_email"], "hi@acme.example")
        self.assertEqual(full["normalized_domain"], "acme.example")
        self.assertIsNone(full["owner_user_id"])
        self.assertEqual(full["assignee_email"], "alex@example.com")

    def test_duplicate_website_is_rejected_until_forced(self):
        self.add_user("Alex Example", "alex@example.com")
        first = json.loads(mcp_server.create_lead("Acme Studio", "alex@example.com", website="https://acme.example"))
        dup = json.loads(mcp_server.create_lead("Acme Again", "alex@example.com", website="https://www.acme.example/about"))
        self.assertEqual(dup["error"], "duplicate")
        self.assertEqual(dup["existing"]["id"], first["id"])
        forced = json.loads(mcp_server.create_lead("Acme Again", "alex@example.com", website="https://www.acme.example/about", force=True))
        self.assertIn("id", forced)
        self.assertNotEqual(forced["id"], first["id"])

    def test_mcp_leads_require_an_assignee_and_stay_open(self):
        alex = self.add_user("Alex Example", "alex@example.com")
        sam = self.add_user("Sam Owner", "sam@example.com")
        missing_actor = json.loads(mcp_server.create_lead("Needs An Actor", assignee_email="alex@example.com"))
        self.assertIn("MCP_USER_EMAIL", missing_actor["error"])

        os.environ["MCP_USER_EMAIL"] = "alex@example.com"
        db = core.connect()
        try:
            payload, code = core.create_lead(db, {"company_name": "No Assignee"}, user_id=alex)
            self.assertEqual(code, 400)
            self.assertEqual(payload["error"], "assignee required")
        finally:
            db.close()

        unknown = json.loads(mcp_server.create_lead("Missing Person", assignee_email="nobody@example.com"))
        self.assertIn("No CRM user", unknown["error"])

        created = json.loads(mcp_server.create_lead("Shared Lead", assignee_email="sam@example.com"))
        db = core.connect()
        try:
            row = db.execute(
                "SELECT owner_user_id, assignee_user_id, created_by_user_id FROM leads WHERE id=?",
                (created["id"],),
            ).fetchone()
            self.assertIsNone(row["owner_user_id"])
            self.assertEqual(row["assignee_user_id"], sam)
            self.assertEqual(row["created_by_user_id"], alex)
        finally:
            db.close()

        os.environ["MCP_USER_EMAIL"] = "sam@example.com"
        updated = json.loads(mcp_server.update_lead(created["id"], {"geography": "Porto", "assignee_email": "alex@example.com"}))
        self.assertTrue(updated["ok"])
        moved = json.loads(mcp_server.set_stage(created["id"], "archived"))
        self.assertTrue(moved["ok"])
        gone = json.loads(mcp_server.delete_lead(created["id"]))
        self.assertTrue(gone["ok"])
        self.assertEqual(json.loads(mcp_server.get_lead(created["id"]))["error"], "not found")

    def test_finalized_still_requires_a_link_free_draft(self):
        self.add_user("Alex Example", "alex@example.com")
        created = json.loads(mcp_server.create_lead("Draft Me", assignee_email="alex@example.com"))
        blocked = json.loads(mcp_server.set_stage(created["id"], "finalized"))
        self.assertIn("subject and message body", blocked["error"])

        mcp_server.update_lead(created["id"], {"subject": "See https://example.com", "message_draft": "Hello there."})
        linked = json.loads(mcp_server.set_stage(created["id"], "finalized"))
        self.assertIn("no links", linked["error"])

        mcp_server.update_lead(created["id"], {"subject": "A short note", "message_draft": "Hello there."})
        ok = json.loads(mcp_server.set_stage(created["id"], "finalized"))
        self.assertEqual(ok["stage"], "finalized")


if __name__ == "__main__":
    unittest.main()
