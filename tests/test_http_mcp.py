"""Run:  python -m unittest tests.test_http_mcp   (from the crm folder)

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
import app as crm  # noqa: E402
import mcp_server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


class HttpMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig = (
            core.USING_POSTGRES,
            core.DATABASE_URL,
            core.DB_PATH,
            os.environ.get("MCP_USER_EMAIL"),
            os.environ.get("CRM_API_USER_EMAIL"),
        )
        core.USING_POSTGRES = False
        core.DATABASE_URL = ""
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        os.environ.pop("MCP_USER_EMAIL", None)
        os.environ["CRM_API_USER_EMAIL"] = "sam@example.com"
        core.init_db()
        db = core.connect()
        try:
            db.execute(
                "INSERT INTO users (name, email, password_hash, created_at) VALUES (?,?,?,?)",
                ("Sam", "sam@example.com", "x", core.now_iso()),
            )
            db.commit()
        finally:
            db.close()
        self.asgi = mcp_server.build_asgi(crm.app, host="0.0.0.0")

    def tearDown(self):
        core.USING_POSTGRES, core.DATABASE_URL, core.DB_PATH, mcp_email, api_email = self.orig
        if mcp_email is None:
            os.environ.pop("MCP_USER_EMAIL", None)
        else:
            os.environ["MCP_USER_EMAIL"] = mcp_email
        if api_email is None:
            os.environ.pop("CRM_API_USER_EMAIL", None)
        else:
            os.environ["CRM_API_USER_EMAIL"] = api_email
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mcp_creates_a_lead_and_flask_stays_on_the_same_port(self):
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        with TestClient(self.asgi) as client:
            me = client.get("/api/auth/me")
            self.assertEqual(me.status_code, 200)
            self.assertFalse(me.json()["authenticated"])
            self.assertEqual(client.get("/api/meta").status_code, 401)

            opened = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            })
            self.assertEqual(opened.status_code, 200, opened.text)
            session = opened.headers["mcp-session-id"]
            headers["mcp-session-id"] = session

            ready = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            })
            self.assertIn(ready.status_code, (200, 202), ready.text)

            called = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "create_lead",
                    "arguments": {
                        "company_name": "HTTP MCP Co",
                        "assignee_email": "sam@example.com",
                        "website": "https://example.com",
                    },
                },
            })
            self.assertEqual(called.status_code, 200, called.text)
            body = called.json()
            self.assertNotIn("error", body)
            text = body["result"]["content"][0]["text"]
            created = json.loads(text)
            self.assertTrue(created.get("id"), created)

        db = core.connect()
        try:
            row = db.execute(
                "SELECT company_name, stage, owner_user_id, assignee_user_id FROM leads WHERE id=?",
                (created["id"],),
            ).fetchone()
            user = db.execute("SELECT id FROM users WHERE email=?", ("sam@example.com",)).fetchone()
            self.assertEqual(row["company_name"], "HTTP MCP Co")
            self.assertEqual(row["stage"], "to_research")
            self.assertIsNone(row["owner_user_id"])
            self.assertEqual(row["assignee_user_id"], user["id"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
