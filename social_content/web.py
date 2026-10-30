"""Review app: reviewers sign in by email link, edit drafts, approve or reject them."""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from . import db, generator, notify, publisher
from .auth import allowed_locations, read_token, secret_key
from .config import PLATFORMS, TZ
from .locations import load_locations

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(PLATFORMS=PLATFORMS)
templates.env.filters["local"] = lambda ts, fmt="%a %b %d, %-I:%M %p": (
    (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(TZ).strftime(fmt) if ts else "")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _locations():
    return load_locations()


def _user(request: Request) -> str | None:
    return request.session.get("email")


def _require(request: Request, slug: str):
    """Returns (locations, location) or raises a redirect/403 response."""
    email = _user(request)
    if not email:
        raise _Redirect(f"/login?next={request.url.path}")
    locations = _locations()
    if slug not in allowed_locations(email, locations):
        raise _Forbidden()
    return locations, locations[slug]


class _Redirect(Exception):
    def __init__(self, url):
        self.url = url


class _Forbidden(Exception):
    pass


def _guard(handler):
    async def wrapped(request: Request):
        try:
            return await handler(request)
        except _Redirect as r:
            return RedirectResponse(r.url, status_code=303)
        except _Forbidden:
            return PlainTextResponse("You don't review this location.", status_code=403)
    return wrapped


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


# ---------------------------------- auth ---------------------------------- #
async def login(request: Request):
    nxt = _safe_next(request.query_params.get("next"))
    if request.method == "GET":
        return templates.TemplateResponse(request, "login.html", {"sent": False, "next": nxt})
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    if allowed_locations(email, _locations()):
        try:
            notify.send_login_link(email)
        except Exception:  # noqa: BLE001 - shown as the same page; details in the log
            log.exception("Sending the sign-in link to %s failed", email)
    # Same answer either way, so the page can't be used to discover reviewer emails.
    return templates.TemplateResponse(request, "login.html", {"sent": True, "next": nxt})


async def auth(request: Request):
    email = read_token(request.query_params.get("token", ""))
    if not email or not allowed_locations(email, _locations()):
        return templates.TemplateResponse(request, "login.html", {"sent": False, "expired": True, "next": "/"},
                                          status_code=400)
    request.session["email"] = email
    return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# -------------------------------- screens --------------------------------- #
async def home(request: Request):
    email = _user(request)
    if not email:
        return RedirectResponse("/login", status_code=303)
    locations = _locations()
    slugs = allowed_locations(email, locations)
    if len(slugs) == 1:
        return RedirectResponse(f"/l/{slugs[0]}", status_code=303)
    waiting = {}
    for p in db.list_posts(statuses=("draft", "failed"), limit=5000):
        waiting[p["location"]] = waiting.get(p["location"], 0) + 1
    rows = [(locations[s], waiting.get(s, 0)) for s in slugs]
    return templates.TemplateResponse(request, "home.html", {"email": email, "rows": rows})


@_guard
async def location_queue(request: Request):
    slug = request.path_params["slug"]
    locations, loc = _require(request, slug)
    since = (datetime.now(TZ) - timedelta(days=35)).date()
    posts = db.list_posts(slug, since=since)
    posts.sort(key=lambda p: p["scheduled_at"])
    weeks, overdue = {}, set()
    for p in posts:
        if p["status"] == "draft" and db.aware(p["scheduled_at"]) < db.now():
            overdue.add(p["id"])
        weeks.setdefault(p["week_of"], []).append(p)
    return templates.TemplateResponse(request, "location.html", {
        "email": _user(request), "loc": loc, "weeks": sorted(weeks.items(), reverse=True),
        "auto": publisher.auto_publish_enabled(loc), "overdue": overdue,
        "many": len(allowed_locations(_user(request), locations)) > 1,
    })


@_guard
async def post_detail(request: Request):
    post = db.get_post(int(request.path_params["post_id"]))
    if not post:
        return PlainTextResponse("Not found", status_code=404)
    locations, loc = _require(request, post["location"])
    message = request.query_params.get("msg")
    if request.method == "POST":
        message = await _apply_action(request, loc, post)
        return RedirectResponse(f"/p/{post['id']}?msg={message}", status_code=303)
    return templates.TemplateResponse(request, "post.html", {
        "email": _user(request), "loc": loc, "post": post, "platform": PLATFORMS[post["platform"]],
        "photos": db.list_photos(loc.slug), "auto": publisher.auto_publish_enabled(loc), "msg": message,
        "blocking": generator.live_checks(post),
    })


async def _apply_action(request: Request, loc, post) -> str:
    form = await request.form()
    action = form.get("action")
    email = _user(request)
    if post["status"] in ("scheduled", "published") and action != "mark_posted":
        return "already sent; change it in Ayrshare or on the platform"
    edits = {}
    if action in ("save", "reject", "rewrite", "approve"):
        edits = {"text": str(form.get("text", post["text"])).strip(),
                 "photo_id": str(form.get("photo_id") or "") or None,
                 "alt_text": str(form.get("alt_text", "")).strip() or None}
        when = form.get("scheduled_at")
        if when:
            local = datetime.fromisoformat(str(when)).replace(tzinfo=TZ)
            edits["scheduled_at"] = local.astimezone(timezone.utc)
    if action == "save":
        db.update_post(post["id"], **edits)
        return "saved"
    if action == "reject":
        db.update_post(post["id"], status="rejected", reviewer=email,
                       reviewer_note=str(form.get("note", "")).strip() or None, **edits)
        return "rejected"
    if action == "reopen":
        db.update_post(post["id"], status="draft")
        return "reopened"
    if action == "rewrite":
        instruction = str(form.get("instruction", "")).strip()
        if not instruction:
            return "say what to change"
        db.update_post(post["id"], **edits)
        try:
            out = generator.rewrite(loc, db.get_post(post["id"]), instruction)
        except Exception:  # noqa: BLE001
            log.exception("Rewrite of post %s failed", post["id"])
            return "rewrite failed, try again"
        db.update_post(post["id"], text=out["text"].strip(), needs_check=out.get("needs_check") or [])
        return "rewritten"
    if action == "approve":
        db.update_post(post["id"], reviewer=email, **edits)
        current = db.get_post(post["id"])
        if generator.live_checks(current):
            return "not approved yet, fix the items above"
        try:
            result = publisher.publish(loc, current)
        except Exception as exc:  # noqa: BLE001
            log.exception("Publishing post %s failed", post["id"])
            result = {"status": "failed", "publish_error": f"{type(exc).__name__}: {exc}"}
        db.update_post(post["id"], **result)
        return {"approved": "approved", "scheduled": "scheduled", "published": "published"}.get(
            result["status"], "publishing failed")
    if action == "mark_posted":
        db.update_post(post["id"], status="published", published_at=db.now(), reviewer=email)
        return "marked as posted"
    return "nothing changed"


@_guard
async def photo_library(request: Request):
    slug = request.path_params["slug"]
    locations, loc = _require(request, slug)
    msg = request.query_params.get("msg")
    if request.method == "POST":
        form = await request.form()
        if form.get("action") == "archive":
            photo = db.get_photo(str(form.get("photo_id")))
            if photo and photo["location"] == slug:
                db.update_photo(photo["id"], archived=True)
            return RedirectResponse(f"/l/{slug}/photos?msg=removed", status_code=303)
        count = 0
        for upload in form.getlist("files"):
            raw = await upload.read()
            if not raw or len(raw) > MAX_UPLOAD_BYTES:
                continue
            try:
                db.add_upload(slug, upload.filename or "photo.jpg", raw, str(form.get("caption", "")).strip(),
                              _user(request))
                count += 1
            except Exception:  # noqa: BLE001 - not an image Pillow can read
                log.warning("Skipped upload %s for %s", upload.filename, slug)
        return RedirectResponse(f"/l/{slug}/photos?msg={count} uploaded", status_code=303)
    return templates.TemplateResponse(request, "photos.html", {
        "email": _user(request), "loc": loc, "photos": db.list_photos(slug), "msg": msg})


async def media(request: Request):
    """Public on purpose: Ayrshare and the social networks fetch photos from here."""
    photo_id = request.path_params["photo_id"].removesuffix(".jpg")
    photo = db.get_photo(photo_id)
    if not photo:
        return PlainTextResponse("Not found", status_code=404)
    raw = db.photo_bytes(photo, _locations())
    if photo["source"] == "repo":
        raw, _ = db.shrink_image(raw)
    return Response(raw, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


async def healthz(request: Request):
    return PlainTextResponse("ok")


def create_app() -> Starlette:
    db.sync_repo_photos(_locations())
    https_only = os.getenv("PUBLIC_BASE_URL", "").startswith("https://")
    return Starlette(
        routes=[
            Route("/", home),
            Route("/login", login, methods=["GET", "POST"]),
            Route("/auth", auth),
            Route("/logout", logout, methods=["POST"]),
            Route("/l/{slug}", location_queue),
            Route("/l/{slug}/photos", photo_library, methods=["GET", "POST"]),
            Route("/p/{post_id:int}", post_detail, methods=["GET", "POST"]),
            Route("/media/{photo_id}", media),
            Route("/healthz", healthz),
        ],
        middleware=[Middleware(SessionMiddleware, secret_key=secret_key(), max_age=30 * 86400,
                               same_site="lax", https_only=https_only)],
    )
