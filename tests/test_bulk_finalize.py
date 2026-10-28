"""Run:  python -m unittest discover -s tests   (from the crm folder)"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import app as crm  # noqa: E402

BODY = "Hi Acme team,\n\nI work freelance from Portugal.\n\nBest,\nAlex Example"


class BulkFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_db = core.DB_PATH
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        core.init_db()
        self.con = core.connect()
        self.client = crm.app.test_client()
        self.client.post("/api/auth/register", json={
            "name": "Test User",
            "email": "test@example.com",
            "password": "secret123",
        })

    def tearDown(self):
        self.con.close()
        core.DB_PATH = self.orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lead(self, name, **kw):
        vals = {"company_name": name, "stage": "drafted", "subject": "Development support",
                "message_draft": BODY, "ai_status": "done"}
        vals.update(kw)
        cols = ",".join(vals)
        cur = self.con.execute(f"INSERT INTO leads ({cols}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
        self.con.commit()
        return cur.lastrowid

    def stage(self, lead_id):
        return self.con.execute("SELECT stage FROM leads WHERE id=?", (lead_id,)).fetchone()["stage"]

    def test_rules(self):
        ok = self.lead("Clean")
        link = self.lead("Has link", message_draft=BODY + " see www.me.dev")
        flagged = self.lead("Flagged", ai_status="needs_review")
        research = self.lead("Not drafted", stage="to_research")
        r = self.client.post("/api/leads/bulk_finalize", json={"ids": [ok, link, flagged, research]}).get_json()
        self.assertEqual([x["id"] for x in r["finalized"]], [ok])
        self.assertEqual({x["id"] for x in r["skipped"]}, {link, flagged, research})
        self.assertEqual(self.stage(ok), "finalized")
        self.assertEqual(self.stage(link), "drafted")
        self.assertEqual(self.stage(flagged), "drafted")
        self.assertEqual(self.stage(research), "to_research")

    def test_include_flagged(self):
        flagged = self.lead("Flagged", ai_status="needs_review")
        r = self.client.post("/api/leads/bulk_finalize", json={"ids": [flagged], "include_flagged": True}).get_json()
        self.assertEqual(len(r["finalized"]), 1)
        self.assertEqual(self.stage(flagged), "finalized")


if __name__ == "__main__":
    unittest.main()
