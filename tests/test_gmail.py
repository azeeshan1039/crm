"""Run:  python -m unittest discover -s tests   (from the crm folder)"""
import os
import re
import sys
import base64
import shutil
import tempfile
import unittest
from email import message_from_bytes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import gmail  # noqa: E402

BODY = "Hi Acme team,\n\nI work freelance from Portugal.\n\nBest,\nAlex Example"


class NeverSendsTests(unittest.TestCase):
    def test_no_send_call_in_source(self):
        with open(gmail.__file__, encoding="utf-8") as f:
            src = f.read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertNotRegex(code, r"/send\b|messages/send|drafts/send|\.send\(")
        # the only Gmail API endpoint used is drafts.create
        self.assertEqual(set(re.findall(r"https://gmail\.googleapis\.com\S*", code)),
                         {"https://gmail.googleapis.com/gmail/v1/users/me/drafts\""})

    def test_scope_is_compose_only(self):
        self.assertEqual([s for s in gmail.SCOPES if "gmail" in s], ["https://www.googleapis.com/auth/gmail.compose"])


class MessageTests(unittest.TestCase):
    def _decode(self, raw):
        return message_from_bytes(base64.urlsafe_b64decode(raw))

    def test_plain_text_with_recipient(self):
        m = self._decode(gmail.build_message("me@gmail.com", "Alex Example", "hello@acme.com", "Hi", BODY))
        self.assertEqual(m["To"], "hello@acme.com")
        self.assertIn("Alex Example", m["From"])
        self.assertEqual(m.get_content_type(), "text/plain")
        self.assertIn("freelance from Portugal", m.get_payload(decode=True).decode())

    def test_form_only_has_blank_to(self):
        m = self._decode(gmail.build_message("me@gmail.com", "", "", "Hi", BODY))
        self.assertIsNone(m["To"])


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_db = core.DB_PATH
        core.DB_PATH = os.path.join(self.tmp, "crm.db")
        core.init_db()
        self.con = core.connect()
        self.calls = []
        self.orig_create = gmail.create_draft

        def fake_create(con, account, to, subject, body, sender_name=""):
            self.calls.append((account, to, subject))
            return {"id": f"r-{len(self.calls)}"}
        gmail.create_draft = fake_create

    def tearDown(self):
        gmail.create_draft = self.orig_create
        self.con.close()
        core.DB_PATH = self.orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lead(self, name, **kw):
        vals = {"company_name": name, "stage": "finalized", "subject": "Development support",
                "message_draft": BODY, "contact_email": "", "contact_is_form": 0}
        vals.update(kw)
        cols = ",".join(vals)
        cur = self.con.execute(f"INSERT INTO leads ({cols}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
        self.con.commit()
        return cur.lastrowid

    def test_batch_rules(self):
        ok = self.lead("Addressed", contact_email="hello@addressed.com")
        form = self.lead("Form only", contact_is_form=1)
        self.lead("Has link", contact_email="a@b.com", message_draft=BODY + " see www.me.dev")
        self.lead("No route")
        self.lead("Already drafted", contact_email="x@y.com", gmail_draft_id="r-old", gmail_account="me@gmail.com")
        self.lead("Not finalized", stage="drafted", contact_email="z@z.com")

        out = gmail.draft_batch(self.con, "me@gmail.com")
        created = {c["company"] for c in out["created"]}
        skipped = {s["company"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(created, {"Addressed", "Form only"})
        self.assertEqual(out["addressed"], 1)
        self.assertEqual(out["form_only"], 1)
        self.assertIn("link", skipped["Has link"])
        self.assertIn("no verified email", skipped["No route"])
        self.assertIn("already has a draft", skipped["Already drafted"])
        self.assertNotIn("Not finalized", created | set(skipped))
        self.assertIn(("me@gmail.com", "", "Development support"), self.calls)  # form-only -> blank To

        row = self.con.execute("SELECT * FROM leads WHERE id=?", (ok,)).fetchone()
        self.assertEqual(row["stage"], "finalized")  # stays Finalized until the human sends
        self.assertEqual(row["gmail_account"], "me@gmail.com")
        self.assertTrue(row["gmail_draft_id"].startswith("r-"))
        ev = self.con.execute("SELECT detail FROM events WHERE lead_id=? AND type='gmail'", (form,)).fetchone()
        self.assertIn("Not sent", ev["detail"])

    def test_reconnect_stops_batch(self):
        self.lead("A", contact_email="a@a.com")
        self.lead("B", contact_email="b@b.com")

        def expired(*a, **k):
            raise gmail.GmailError("signed out", reconnect=True)
        gmail.create_draft = expired
        out = gmail.draft_batch(self.con, "me@gmail.com")
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(out["created"], [])

    def test_unconnected_account_needs_reconnect(self):
        with self.assertRaises(gmail.GmailError) as cm:
            gmail.access_token(self.con, "nobody@gmail.com")
        self.assertTrue(cm.exception.reconnect)


if __name__ == "__main__":
    unittest.main()
