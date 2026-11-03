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
        self.orig = (core.USING_POSTGRES, core.DATABASE_URL, core.DB_PATH)
        core.USING_POSTGRES = False
        core.DATABASE_URL = ""
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        core.init_db()
        self.client = crm.app.test_client()

    def tearDown(self):
        core.USING_POSTGRES, core.DATABASE_URL, core.DB_PATH = self.orig
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

    def _use_api_key(self, email="sam@example.com", key="test-key"):
        self._saved_env = (
            os.environ.get("CRM_API_KEY"),
            os.environ.get("CRM_API_USER_EMAIL"),
        )
        os.environ["CRM_API_KEY"] = key
        os.environ["CRM_API_USER_EMAIL"] = email
        self.addCleanup(self._restore_api_env)

    def _restore_api_env(self):
        key, email = self._saved_env
        if key is None:
            os.environ.pop("CRM_API_KEY", None)
        else:
            os.environ["CRM_API_KEY"] = key
        if email is None:
            os.environ.pop("CRM_API_USER_EMAIL", None)
        else:
            os.environ["CRM_API_USER_EMAIL"] = email

    def test_api_key_creates_updates_and_deletes_as_configured_user(self):
        self.register()
        sam_id = self.register_other().get("/api/auth/me").get_json()["user"]["id"]
        self._use_api_key()
        api = crm.app.test_client()
        headers = {"Authorization": "Bearer test-key"}

        created = api.post("/api/leads", json={"company_name": "From ChatGPT"}, headers=headers)
        self.assertEqual(created.status_code, 200)
        lead_id = created.get_json()["id"]
        con = core.connect()
        try:
            row = con.execute(
                "SELECT created_by_user_id, owner_user_id FROM leads WHERE id=?",
                (lead_id,),
            ).fetchone()
            self.assertEqual(row["owner_user_id"], sam_id)
            self.assertEqual(row["created_by_user_id"], sam_id)
        finally:
            con.close()

        updated = api.patch(f"/api/leads/{lead_id}", json={"geography": "Lisbon"}, headers=headers)
        self.assertEqual(updated.status_code, 200)
        deleted = api.delete(f"/api/leads/{lead_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)

    def test_api_key_cannot_edit_another_owners_lead(self):
        self.register()
        lead_id = self.client.post("/api/leads", json={"company_name": "Owner Only"}).get_json()["id"]
        self.register_other()
        self._use_api_key()
        api = crm.app.test_client()
        headers = {"Authorization": "Bearer test-key"}
        patch = api.patch(f"/api/leads/{lead_id}", json={"geography": "Porto"}, headers=headers)
        self.assertEqual(patch.status_code, 403)
        delete = api.delete(f"/api/leads/{lead_id}", headers=headers)
        self.assertEqual(delete.status_code, 403)

    def test_api_key_does_not_open_other_routes(self):
        self.register()
        self._use_api_key(email="alex@example.com")
        api = crm.app.test_client()
        headers = {"Authorization": "Bearer test-key"}
        self.assertEqual(api.get("/api/meta", headers=headers).status_code, 401)
        self.assertEqual(api.get("/api/leads", headers=headers).status_code, 401)
        self.assertEqual(
            api.post("/api/leads/1/stage", json={"stage": "archived"}, headers=headers).status_code,
            401,
        )

    def test_wrong_api_key_is_rejected(self):
        self.register_other()
        self._use_api_key()
        api = crm.app.test_client()
        res = api.post(
            "/api/leads",
            json={"company_name": "Nope"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error"], "invalid api key")

    def test_api_key_requires_a_real_user(self):
        self.register()
        self._use_api_key(email="missing@example.com")
        api = crm.app.test_client()
        res = api.post(
            "/api/leads",
            json={"company_name": "Nope"},
            headers={"Authorization": "Bearer test-key"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertIn("No CRM user", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
