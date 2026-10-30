"""Drafts a week of posts for a location with Claude.

One request per location covers every platform for the week, so the posts
read as a set instead of repeating each other. The request carries the
location's facts, a sample of its photos, its recent posts (to avoid repeats)
and examples of how its reviewer edited earlier drafts (to pick up their voice).
"""
import base64
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import anthropic
import yaml

from . import db
from .config import EFFORT, MAX_PHOTOS_PER_RUN, MODEL, PILLARS, PLATFORMS, POSTING_DAYS, TZ
from .locations import Location, brand

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You write social media posts for one location of Barrister Executive Suites, an operator of \
executive suites, private offices, coworking, virtual offices and meeting rooms in Southern California.

Your drafts go to a staff member at that location who edits them before anything is published, so write \
something they would be glad to post with light edits.

Rules:
- Use only facts given in the brand notes and the location profile. Never invent prices, discounts, \
availability, awards, statistics, tenant names, reviews or events. If a post would be stronger with a fact you \
don't have, write it without the fact and add a short question to needs_check (e.g. "Is the terrace open to \
day-pass guests?").
- Only pick a photo_id from the photos shown. Describe what the photo actually shows; don't claim it shows \
something you can't see. Use an empty string when no photo fits.
- Follow each platform's length, hashtag and style guidance. Never exceed the character limit.
- Match the reviewer's edits shown in the examples: their wording, tone and what they cut are the house style \
for this location.
- Don't repeat the angle, opening line or call to action of the recent posts listed.
- Write alt_text for every post with a photo: one plain sentence describing the image for screen readers."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "text": {"type": "string"},
                    "photo_id": {"type": "string"},
                    "alt_text": {"type": "string"},
                    "needs_check": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["slot", "text", "photo_id", "alt_text", "needs_check"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["posts"],
    "additionalProperties": False,
}

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "needs_check": {"type": "array", "items": {"type": "string"}}},
    "required": ["text", "needs_check"],
    "additionalProperties": False,
}


@dataclass
class Slot:
    index: int
    platform: str
    scheduled_at: datetime     # UTC
    pillar: str


class GenerationError(RuntimeError):
    pass


def next_monday(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())


def plan_slots(loc: Location, week_of: date) -> list[Slot]:
    """Which platform posts on which day this week, and the content pillar for each."""
    slots, i = [], 0
    offset = week_of.isocalendar().week  # rotate pillars from week to week
    for key, per_week in loc.platforms.items():
        platform = PLATFORMS[key]
        for n in range(min(per_week, len(POSTING_DAYS))):
            day = week_of + timedelta(days=POSTING_DAYS[n])
            local = datetime.combine(day, time(platform.hour), tzinfo=TZ)
            slots.append(Slot(i, key, local.astimezone(timezone.utc), PILLARS[(offset + i) % len(PILLARS)]))
            i += 1
    return slots


def _image_block(raw: bytes) -> dict:
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    img.thumbnail((1024, 1024))  # plenty for Claude to judge the scene; keeps tokens down
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def pick_photos(loc: Location) -> list:
    """Least recently used first, so the whole library gets rotated through."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows = db.list_photos(loc.slug)
    rows.sort(key=lambda r: db.aware(r["last_used_at"]) or epoch)
    return rows[:MAX_PHOTOS_PER_RUN]


def build_request(loc: Location, slots: list[Slot], photos: list, locations: dict) -> list:
    profile = {k: v for k, v in loc.profile.items() if k not in ("reviewers", "active", "platforms")}
    recent = db.list_posts(loc.slug, statuses=("draft", "approved", "scheduled", "published"), limit=20)
    examples = db.edited_examples(loc.slug)

    parts = [
        "## Brand notes\n" + yaml.safe_dump(brand(), sort_keys=False, allow_unicode=True),
        "## Location profile\n" + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
    ]
    if examples:
        parts.append("## How this location's reviewer edited earlier drafts\n" + "\n\n".join(
            f"[{e['platform']}]\nDRAFT:\n{e['draft_text']}\nPUBLISHED:\n{e['text']}" for e in examples))
    if recent:
        parts.append("## Recent posts (don't repeat these)\n" + "\n---\n".join(
            f"[{r['platform']}] {r['text']}" for r in recent))
    lines = []
    for s in slots:
        p = PLATFORMS[s.platform]
        when = s.scheduled_at.astimezone(TZ).strftime("%A %b %d")
        lines.append(f"- slot {s.index}: {p.label} on {when}. Pillar: {s.pillar}. Max {p.max_chars} characters; "
                     f"{p.hashtags}. {p.style}" + (" Must use a photo." if p.needs_photo else ""))
    parts.append("## Posts to write this week (one per slot)\n" + "\n".join(lines))

    content = [{"type": "text", "text": "\n\n".join(parts)}]
    if photos:
        content.append({"type": "text", "text": "## Photos you can use"})
        for ph in photos:
            raw = db.photo_bytes(ph, locations)
            if not raw:
                continue
            content.append({"type": "text", "text": f"photo_id: {ph['id']}"
                                                    + (f" (caption: {ph['caption']})" if ph["caption"] else "")})
            content.append(_image_block(raw))
    else:
        content.append({"type": "text", "text": "No photos are available yet; use an empty photo_id."})
    return [{"role": "user", "content": content}]


def _call(client, messages, schema, max_tokens=32000) -> dict:
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=messages,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise GenerationError(f"Claude declined the request: {getattr(msg.stop_details, 'explanation', '')}")
    if msg.stop_reason == "max_tokens":
        raise GenerationError("Claude's response was cut off at max_tokens")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    log.info("Claude usage: %s in / %s out", msg.usage.input_tokens, msg.usage.output_tokens)
    return json.loads(text)


def draft_week(loc: Location, week_of: date, locations: dict, client=None) -> list[dict]:
    """Returns post rows ready for db.insert_posts (nothing is written here)."""
    slots = plan_slots(loc, week_of)
    if not slots:
        return []
    photos = pick_photos(loc)
    photo_ids = {p["id"] for p in photos}
    client = client or anthropic.Anthropic()
    result = _call(client, build_request(loc, slots, photos, locations), OUTPUT_SCHEMA)

    by_slot = {p["slot"]: p for p in result["posts"]}
    rows = []
    for s in slots:
        out = by_slot.get(s.index)
        if not out:
            log.warning("%s: Claude skipped slot %s (%s)", loc.slug, s.index, s.platform)
            continue
        rows.append(post_row(loc, s, week_of, out, photo_ids))
    used = {r["photo_id"] for r in rows if r["photo_id"]}
    for pid in used:
        db.update_photo(pid, last_used_at=db.now())
    return rows


def post_row(loc: Location, slot: Slot, week_of: date, out: dict, photo_ids: set) -> dict:
    text = out["text"].strip()
    checks = list(out.get("needs_check") or [])
    photo_id = out.get("photo_id") if out.get("photo_id") in photo_ids else None
    return {
        "location": loc.slug, "platform": slot.platform, "week_of": week_of, "scheduled_at": slot.scheduled_at,
        "pillar": slot.pillar, "draft_text": text, "photo_id": photo_id,
        "alt_text": out.get("alt_text") if photo_id else None, "needs_check": checks,
    }


def live_checks(post) -> list[str]:
    """Problems that block approval, recomputed whenever the post changes."""
    platform = PLATFORMS[post["platform"]]
    checks = []
    if len(post["text"]) > platform.max_chars:
        checks.append(f"{len(post['text'])} characters; {platform.label} allows {platform.max_chars}. "
                      "Shorten before approving.")
    if platform.needs_photo and not post["photo_id"]:
        checks.append(f"{platform.label} needs a photo. Pick one below or upload one.")
    return checks


def rewrite(loc: Location, post, instruction: str, client=None) -> dict:
    """Rewrites one post following the reviewer's instruction."""
    platform = PLATFORMS[post["platform"]]
    profile = {k: v for k, v in loc.profile.items() if k not in ("reviewers", "active", "platforms")}
    prompt = (
        "## Brand notes\n" + yaml.safe_dump(brand(), sort_keys=False, allow_unicode=True)
        + "\n## Location profile\n" + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
        + f"\n## Current {platform.label} post\n{post['text']}\n"
        + f"\n## Platform rules\nMax {platform.max_chars} characters; {platform.hashtags}. {platform.style}\n"
        + f"\n## The reviewer asks\n{instruction}\n\nRewrite the post accordingly and return the full new text."
    )
    return _call(client or anthropic.Anthropic(), [{"role": "user", "content": prompt}], REWRITE_SCHEMA,
                 max_tokens=16000)
