"""Sends approved posts to the location's social accounts.

With AYRSHARE_API_KEY set and an AYRSHARE_PROFILE_<CODE> key for the location,
an approved post is handed to Ayrshare, which holds it until its scheduled time
and posts it to the account linked for that location. Without them the post
stays "approved" and the reviewer copies it out, posts it and clicks
"Mark as posted".
"""
import logging
import os
from datetime import timedelta, timezone

import requests

from . import db
from .config import PLATFORMS

log = logging.getLogger(__name__)

AYRSHARE_URL = "https://api.ayrshare.com/api/post"


def auto_publish_enabled(loc) -> bool:
    return bool(os.getenv("AYRSHARE_API_KEY") and loc.ayrshare_profile_key)


def public_photo_url(photo_id: str) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL is not set, so Ayrshare can't fetch the photo.")
    return f"{base}/media/{photo_id}.jpg"


def publish(loc, post) -> dict:
    """Returns the db fields to update on the post."""
    if not auto_publish_enabled(loc):
        return {"status": "approved"}
    body = {"post": post["text"], "platforms": [PLATFORMS[post["platform"]].ayrshare]}
    if post["photo_id"]:
        body["mediaUrls"] = [public_photo_url(post["photo_id"])]
    scheduled = db.aware(post["scheduled_at"])
    # A slot that has already passed goes out now instead of being rejected.
    if scheduled > db.now() + timedelta(minutes=5):
        body["scheduleDate"] = scheduled.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.post(
        AYRSHARE_URL, json=body, timeout=60,
        headers={"Authorization": f"Bearer {os.environ['AYRSHARE_API_KEY']}",
                 "Profile-Key": loc.ayrshare_profile_key},
    )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not resp.ok or data.get("status") not in ("success", "scheduled"):
        log.error("Ayrshare rejected post %s: %s %s", post["id"], resp.status_code, resp.text[:500])
        return {"status": "failed", "publish_error": f"Ayrshare {resp.status_code}: {resp.text[:500]}"}
    ref = str(data.get("id") or data.get("postIds") or "")
    if "scheduleDate" in body:
        return {"status": "scheduled", "publish_ref": ref, "publish_error": None}
    return {"status": "published", "publish_ref": ref, "publish_error": None, "published_at": db.now()}
