"""Run with: python -m unittest discover -s tests -t ."""
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["LOCATIONS_DIR"] = f"{TMP}/locations"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_EMAILS"] = "boss@example.com"
os.environ.pop("AYRSHARE_API_KEY", None)

from social_content import auth, db, generator, publisher  # noqa: E402
from social_content.locations import load_locations  # noqa: E402
from social_content.seed import clean_html, facts_from_property, write_profiles  # noqa: E402

PROPERTY = {
    "Name": "P44 - Comerica Bank Building",
    "Billing_Street__c": "<p><strong><u>15303 Ventura Blvd., 9th Floor</u></strong></p>",
    "Billing_City__c": "<p><strong><u>Sherman Oaks</u></strong></p>",
    "Billing_State__c": "<p>CA</p>", "Billing_Postal_Code__c": "<p>91403<span>﻿</span></p>",
    "Amenities__c": "<p>•\tBanks</p><p>Theaters &amp; Health Clubs</p><p><br></p>",
    "Points_Of_Interest__c": None, "Description__c": None, "Parking_Availability__c": True,
}


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "navy").save(buf, "JPEG")
    return buf.getvalue()


class FakeClient:
    """Stands in for anthropic.Anthropic(); returns canned JSON."""

    def __init__(self, payload):
        self.payload, self.requests = payload, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        payload = self.payload(kwargs) if callable(self.payload) else self.payload
        msg = SimpleNamespace(stop_reason="end_turn", stop_details=None,
                              usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                              content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        cm = mock.MagicMock()
        cm.__enter__.return_value.get_final_message.return_value = msg
        return cm


def setUpModule():
    shutil.rmtree(f"{TMP}/locations", ignore_errors=True)
    write_profiles([PROPERTY])
    folder = Path(f"{TMP}/locations/p44-comerica-bank-building")
    profile = (folder / "profile.yaml").read_text()
    profile = profile.replace("active: false", "active: true").replace(
        "reviewers: []", "reviewers:\n  - Jane@Example.com").replace("  linkedin: 1\n", "  linkedin: 1\n  x: 1\n")
    (folder / "profile.yaml").write_text(profile)
    (folder / "photos" / "lobby.jpg").write_bytes(jpeg())
    (folder / "photos.yaml").write_text("lobby.jpg: Lobby with reception desk\n")
    db.reset_engine()


class SeedTests(unittest.TestCase):
    def test_clean_html(self):
        self.assertEqual(clean_html("<p>•\tA &amp; B</p><p><br></p><p>C</p>"), ["A & B", "C"])

    def test_facts(self):
        facts = facts_from_property(PROPERTY)
        self.assertEqual(facts["address"], "15303 Ventura Blvd., 9th Floor, Sherman Oaks, CA 91403")
        self.assertEqual(facts["amenities"], ["Banks", "Theaters & Health Clubs"])
        self.assertNotIn("nearby", facts)

    def test_rerun_keeps_profile_edits(self):
        write_profiles([PROPERTY])
        loc = load_locations()["p44-comerica-bank-building"]
        self.assertEqual(loc.reviewers, ["jane@example.com"])
        self.assertEqual(loc.profile["salesforce_facts"]["city"], "Sherman Oaks")


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.locations = load_locations()
        self.loc = self.locations["p44-comerica-bank-building"]
        db.sync_repo_photos(self.locations)
        self.photo_id = db.list_photos(self.loc.slug)[0]["id"]

    def test_slots_spread_over_week_in_pacific_time(self):
        slots = generator.plan_slots(self.loc, date(2026, 11, 2))
        self.assertEqual(len(slots), 7)  # 2 fb + 2 ig + 1 li + 1 gbp + 1 x
        fb = [s for s in slots if s.platform == "facebook"]
        self.assertEqual([s.scheduled_at.isoformat() for s in fb],
                         ["2026-11-03T18:00:00+00:00", "2026-11-05T18:00:00+00:00"])  # 10 AM PST

    def test_draft_week_validates_output(self):
        def payload(req):
            slots = generator.plan_slots(self.loc, date(2026, 11, 2))
            posts = [{"slot": s.index, "text": f"Post {s.index}", "photo_id": "", "alt_text": "", "needs_check": []}
                     for s in slots]
            x_slot = next(s.index for s in slots if s.platform == "x")
            posts[0]["photo_id"] = "made-up-id"            # not a photo we sent
            posts[2]["photo_id"] = self.photo_id           # instagram, valid
            posts[2]["alt_text"] = "Reception desk"
            posts[x_slot]["text"] = "x" * 300              # X allows 280
            return {"posts": posts}

        client = FakeClient(payload)
        rows = generator.draft_week(self.loc, date(2026, 11, 2), self.locations, client=client)
        self.assertEqual(len(rows), 7)
        self.assertIsNone(rows[0]["photo_id"])
        self.assertEqual(rows[2]["photo_id"], self.photo_id)
        ig = {**rows[3], "text": rows[3]["draft_text"]}
        self.assertEqual(generator.live_checks(ig), ["Instagram needs a photo. Pick one below or upload one."])
        x_row = next(r for r in rows if r["platform"] == "x")
        self.assertTrue(any("280" in c for c in generator.live_checks({**x_row, "text": x_row["draft_text"]})))

        req = client.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["fallbacks"], "default")
        content = req["messages"][0]["content"]
        self.assertIn("Sherman Oaks", content[0]["text"])
        self.assertNotIn("jane@example.com", content[0]["text"].lower())  # reviewers aren't sent to Claude
        self.assertTrue(any(b["type"] == "image" for b in content))
        self.assertTrue(any("Lobby with reception desk" in b.get("text", "") for b in content))

    def test_edits_feed_back_as_examples(self):
        db.insert_posts([{"location": self.loc.slug, "platform": "facebook", "week_of": date(2026, 10, 26),
                          "scheduled_at": db.now(), "pillar": "x", "draft_text": "Stiff corporate draft",
                          "photo_id": None, "alt_text": None, "needs_check": []}])
        post = db.list_posts(self.loc.slug)[0]
        db.update_post(post["id"], text="Hey neighbors! Come say hi.", status="approved")
        msgs = generator.build_request(self.loc, generator.plan_slots(self.loc, date(2026, 11, 2)), [],
                                       self.locations)
        self.assertIn("DRAFT:\nStiff corporate draft\nPUBLISHED:\nHey neighbors! Come say hi.",
                      msgs[0]["content"][0]["text"])


class AuthTests(unittest.TestCase):
    def test_reviewer_sees_only_their_location(self):
        locations = load_locations()
        self.assertEqual(auth.allowed_locations("JANE@example.com", locations), ["p44-comerica-bank-building"])
        self.assertEqual(auth.allowed_locations("stranger@example.com", locations), [])
        self.assertEqual(auth.allowed_locations("boss@example.com", locations), list(locations))

    def test_token_round_trip(self):
        self.assertEqual(auth.read_token(auth.make_token("Jane@example.com")), "jane@example.com")
        self.assertIsNone(auth.read_token("garbage"))


class PublisherTests(unittest.TestCase):
    def test_manual_without_ayrshare(self):
        loc = load_locations()["p44-comerica-bank-building"]
        self.assertEqual(publisher.publish(loc, {"id": 1}), {"status": "approved"})

    @mock.patch.dict(os.environ, {"AYRSHARE_API_KEY": "k", "AYRSHARE_PROFILE_P44": "prof",
                                  "PUBLIC_BASE_URL": "https://studio.example.com"})
    @mock.patch("social_content.publisher.requests.post")
    def test_ayrshare_schedules_future_post(self, post):
        post.return_value = mock.Mock(ok=True, status_code=200, headers={"content-type": "application/json"},
                                      json=lambda: {"status": "scheduled", "id": "abc"})
        loc = load_locations()["p44-comerica-bank-building"]
        from datetime import datetime, timezone
        result = publisher.publish(loc, {"id": 1, "text": "Hi", "platform": "google_business", "photo_id": "p1",
                                         "scheduled_at": datetime(2030, 1, 1, 18, tzinfo=timezone.utc)})
        self.assertEqual(result["status"], "scheduled")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["platforms"], ["gmb"])
        self.assertEqual(body["scheduleDate"], "2030-01-01T18:00:00Z")
        self.assertEqual(body["mediaUrls"], ["https://studio.example.com/media/p1.jpg"])
        self.assertEqual(post.call_args.kwargs["headers"]["Profile-Key"], "prof")


class WebTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        from social_content.web import create_app

        self.client = TestClient(create_app())
        self.locations = load_locations()
        db.insert_posts([{"location": "p44-comerica-bank-building", "platform": "x", "week_of": date(2026, 11, 2),
                          "scheduled_at": db.now(), "pillar": "Neighborhood", "draft_text": "Draft text",
                          "photo_id": None, "alt_text": None, "needs_check": ["Is valet free?"]}])
        self.post = db.list_posts("p44-comerica-bank-building", statuses=("draft",))[0]

    def sign_in(self, email="jane@example.com"):
        r = self.client.get(f"/auth?token={auth.make_token(email)}&next=/l/p44-comerica-bank-building",
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_requires_sign_in(self):
        r = self.client.get("/l/p44-comerica-bank-building", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])

    def test_login_does_not_reveal_reviewers(self):
        with mock.patch("social_content.notify.send_email") as send:
            a = self.client.post("/login", data={"email": "jane@example.com"})
            b = self.client.post("/login", data={"email": "nobody@example.com"})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(a.text, b.text)

    def test_review_flow(self):
        self.sign_in()
        page = self.client.get("/l/p44-comerica-bank-building")
        self.assertIn("Draft text", page.text)
        detail = self.client.get(f"/p/{self.post['id']}")
        self.assertIn("Is valet free?", detail.text)

        self.client.post(f"/p/{self.post['id']}", data={"action": "approve", "text": "My own words",
                                                        "photo_id": "", "alt_text": ""})
        saved = db.get_post(self.post["id"])
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(saved["text"], "My own words")
        self.assertEqual(saved["draft_text"], "Draft text")
        self.assertEqual(saved["reviewer"], "jane@example.com")

        self.client.post(f"/p/{self.post['id']}", data={"action": "mark_posted"})
        self.assertEqual(db.get_post(self.post["id"])["status"], "published")

    def test_too_long_is_not_approved(self):
        self.sign_in()
        self.client.post(f"/p/{self.post['id']}", data={"action": "approve", "text": "y" * 281})
        self.assertEqual(db.get_post(self.post["id"])["status"], "draft")

    def test_other_reviewer_forbidden(self):
        loc_dir = Path(os.environ["LOCATIONS_DIR"]) / "p99-other"
        loc_dir.mkdir(exist_ok=True)
        (loc_dir / "profile.yaml").write_text("code: P99\nname: Other\nreviewers: [sam@example.com]\n")
        try:
            self.sign_in("sam@example.com")
            self.assertEqual(self.client.get(f"/p/{self.post['id']}").status_code, 403)
        finally:
            shutil.rmtree(loc_dir)

    def test_upload_and_public_media(self):
        self.sign_in()
        r = self.client.post("/l/p44-comerica-bank-building/photos", data={"caption": "Terrace"},
                             files=[("files", ("t.png", jpeg(), "image/png"))])
        self.assertEqual(r.status_code, 200)
        photo = [p for p in db.list_photos("p44-comerica-bank-building") if p["caption"] == "Terrace"][0]
        self.client.cookies.clear()
        media = self.client.get(f"/media/{photo['id']}.jpg")
        self.assertEqual(media.headers["content-type"], "image/jpeg")
        self.assertTrue(media.content.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
