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

    def test_duplicate_website_is_rejected_until_forced(self):
        self.add_user("Alex Example", "alex@example.com")
        first = json.loads(mcp_server.create_lead("Acme Studio", website="https://acme.example"))
        dup = json.loads(mcp_server.create_lead("Acme Again", website="https://www.acme.example/about"))
        self.assertEqual(dup["error"], "duplicate")
        self.assertEqual(dup["existing"]["id"], first["id"])
        forced = json.loads(mcp_server.create_lead("Acme Again", website="https://www.acme.example/about", force=True))
        self.assertIn("id", forced)
        self.assertNotEqual(forced["id"], first["id"])

    def test_multiple_users_require_an_email_and_honor_ownership(self):
        owner = self.add_user("Alex Example", "alex@example.com")
        self.add_user("Sam Owner", "sam@example.com")
        missing = json.loads(mcp_server.create_lead("Needs An Actor"))
        self.assertIn("MCP_USER_EMAIL", missing["error"])

        os.environ["MCP_USER_EMAIL"] = "alex@example.com"
        created = json.loads(mcp_server.create_lead("Owner Only"))
        db = core.connect()
        try:
            row = db.execute("SELECT owner_user_id FROM leads WHERE id=?", (created["id"],)).fetchone()
            self.assertEqual(row["owner_user_id"], owner)
        finally:
            db.close()

        os.environ["MCP_USER_EMAIL"] = "sam@example.com"
        denied = json.loads(mcp_server.update_lead(created["id"], {"geography": "Porto"}))
        self.assertEqual(denied["error"], "Only the lead owner can edit or delete this lead.")
        deleted = json.loads(mcp_server.delete_lead(created["id"]))
        self.assertEqual(deleted["error"], "Only the lead owner can edit or delete this lead.")

        os.environ["MCP_USER_EMAIL"] = "alex@example.com"
        moved = json.loads(mcp_server.set_stage(created["id"], "archived"))
        self.assertTrue(moved["ok"])
        self.assertEqual(moved["stage"], "archived")
        gone = json.loads(mcp_server.delete_lead(created["id"]))
        self.assertTrue(gone["ok"])
        self.assertEqual(json.loads(mcp_server.get_lead(created["id"]))["error"], "not found")

    def test_finalized_still_requires_a_link_free_draft(self):
        self.add_user("Alex Example", "alex@example.com")
        created = json.loads(mcp_server.create_lead("Draft Me"))
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
