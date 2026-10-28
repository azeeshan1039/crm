"""Run:  python -m unittest discover -s tests   (from the crm folder)"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import app as crm  # noqa: E402


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_db = core.DB_PATH
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        core.init_db()
        self.client = crm.app.test_client()

    def tearDown(self):
        core.DB_PATH = self.orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self):
        return self.client.post("/api/auth/register", json={
            "name": "Alex Example",
            "email": "alex@example.com",
            "password": "secret123",
        })

    def register_other(self):
        other = crm.app.test_client()
        other.post("/api/auth/register", json={
            "name": "Sam Owner",
            "email": "sam@example.com",
            "password": "secret123",
        })
        return other

    def test_register_logs_user_in(self):
        res = self.register()
        self.assertEqual(res.status_code, 201)
        me = self.client.get("/api/auth/me").get_json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"]["email"], "alex@example.com")

    def test_login_required_after_logout(self):
        self.register()
        self.client.post("/api/auth/logout")
        res = self.client.get("/api/meta")
        self.assertEqual(res.status_code, 401)

    def test_login_with_password(self):
        self.register()
        self.client.post("/api/auth/logout")
        res = self.client.post("/api/auth/login", json={
            "email": "alex@example.com",
            "password": "secret123",
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.client.get("/api/auth/me").get_json()["authenticated"])

    def test_lead_tracks_creator_owner_and_updater(self):
        user_id = self.register().get_json()["user"]["id"]
        res = self.client.post("/api/leads", json={"company_name": "Acme Studio"})
        self.assertEqual(res.status_code, 200)
        lead_id = res.get_json()["id"]
        con = core.connect()
        try:
            row = con.execute(
                "SELECT created_by_user_id, owner_user_id, updated_by_user_id FROM leads WHERE id=?",
                (lead_id,),
            ).fetchone()
            self.assertEqual(row["created_by_user_id"], user_id)
            self.assertEqual(row["owner_user_id"], user_id)
            self.assertEqual(row["updated_by_user_id"], user_id)

            patch = self.client.patch(f"/api/leads/{lead_id}", json={"geography": "Lisbon"})
            self.assertEqual(patch.status_code, 200)
            row = con.execute("SELECT updated_by_user_id FROM leads WHERE id=?", (lead_id,)).fetchone()
            self.assertEqual(row["updated_by_user_id"], user_id)
        finally:
            con.close()

    def test_only_owner_can_edit_or_delete_owned_lead(self):
        self.register()
        res = self.client.post("/api/leads", json={"company_name": "Owner Only"})
        lead_id = res.get_json()["id"]

        other = self.register_other()
        patch = other.patch(f"/api/leads/{lead_id}", json={"geography": "Porto"})
        self.assertEqual(patch.status_code, 403)
        delete = other.delete(f"/api/leads/{lead_id}")
        self.assertEqual(delete.status_code, 403)

        owner_delete = self.client.delete(f"/api/leads/{lead_id}")
        self.assertEqual(owner_delete.status_code, 200)
        con = core.connect()
        try:
            row = con.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone()
            self.assertIsNone(row)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
