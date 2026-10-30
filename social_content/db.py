"""Storage for drafts and photos: Postgres on Railway (DATABASE_URL), SQLite locally."""
import hashlib
import io
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine, select, update)

metadata = MetaData()

posts = Table(
    "social_posts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("location", String(80), nullable=False, index=True),
    Column("platform", String(32), nullable=False),
    Column("week_of", Date, nullable=False, index=True),   # Monday of the posting week
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("pillar", String(200)),
    Column("draft_text", Text, nullable=False),             # what Claude wrote, kept to learn from edits
    Column("text", Text, nullable=False),                   # current text, edited by the reviewer
    Column("photo_id", String(64)),
    Column("alt_text", Text),
    Column("needs_check", JSON),                            # facts Claude wants a human to confirm
    Column("status", String(16), nullable=False, default="draft", index=True),
    Column("reviewer", String(200)),
    Column("reviewer_note", Text),
    Column("publish_ref", String(200)),
    Column("publish_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
)

photos = Table(
    "social_photos", metadata,
    Column("id", String(64), primary_key=True),
    Column("location", String(80), nullable=False, index=True),
    Column("filename", String(200), nullable=False),
    Column("caption", Text),
    Column("source", String(16), nullable=False),           # "repo" or "upload"
    Column("data", LargeBinary),                            # uploads only; repo photos are read from disk
    Column("content_type", String(64), nullable=False, default="image/jpeg"),
    Column("archived", Boolean, nullable=False, default=False),
    Column("uploaded_by", String(200)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True)),
)

_engine = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(ts):
    """SQLite hands back naive datetimes; treat them as UTC."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def engine():
    global _engine
    if _engine is None:
        url = os.getenv("DATABASE_URL", "sqlite:///social_content.db")
        # Railway hands out postgres:// URLs; SQLAlchemy wants an explicit driver.
        url = url.replace("postgres://", "postgresql+psycopg://", 1).replace(
            "postgresql://", "postgresql+psycopg://", 1)
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        metadata.create_all(_engine)
    return _engine


def reset_engine():
    """Tests point DATABASE_URL somewhere new and call this."""
    global _engine
    _engine = None


# --------------------------------- posts ---------------------------------- #
def insert_posts(rows: list[dict]) -> None:
    ts = now()
    with engine().begin() as conn:
        for row in rows:
            conn.execute(posts.insert().values(
                status="draft", created_at=ts, updated_at=ts, text=row["draft_text"], **row))


def get_post(post_id: int):
    with engine().connect() as conn:
        return conn.execute(select(posts).where(posts.c.id == post_id)).mappings().first()


def update_post(post_id: int, **values) -> None:
    with engine().begin() as conn:
        conn.execute(update(posts).where(posts.c.id == post_id).values(updated_at=now(), **values))


def list_posts(location: str = None, statuses=None, since=None, limit: int = 500) -> list:
    q = select(posts)
    if location:
        q = q.where(posts.c.location == location)
    if statuses:
        q = q.where(posts.c.status.in_(statuses))
    if since:
        q = q.where(posts.c.week_of >= since)
    q = q.order_by(posts.c.scheduled_at.desc()).limit(limit)
    with engine().connect() as conn:
        return list(conn.execute(q).mappings())


def week_exists(location: str, week_of) -> bool:
    with engine().connect() as conn:
        return conn.execute(select(posts.c.id).where(
            posts.c.location == location, posts.c.week_of == week_of).limit(1)).first() is not None


def edited_examples(location: str, limit: int = 6) -> list:
    """Approved posts the reviewer changed: Claude's draft next to the final text."""
    q = (select(posts.c.platform, posts.c.draft_text, posts.c.text)
         .where(posts.c.location == location,
                posts.c.status.in_(("approved", "scheduled", "published")),
                posts.c.text != posts.c.draft_text)
         .order_by(posts.c.updated_at.desc()).limit(limit))
    with engine().connect() as conn:
        return list(conn.execute(q).mappings())


# --------------------------------- photos --------------------------------- #
def _repo_photo_id(location: str, filename: str) -> str:
    return hashlib.sha256(f"{location}/{filename}".encode()).hexdigest()[:32]


def sync_repo_photos(locations) -> None:
    """Registers photos committed under locations/<slug>/photos/ (idempotent)."""
    with engine().begin() as conn:
        known = {r.id for r in conn.execute(select(photos.c.id).where(photos.c.source == "repo"))}
        for loc in locations.values():
            for filename, path, caption in loc.repo_photos():
                pid = _repo_photo_id(loc.slug, filename)
                if pid in known:
                    conn.execute(update(photos).where(photos.c.id == pid).values(caption=caption or None))
                    continue
                conn.execute(photos.insert().values(
                    id=pid, location=loc.slug, filename=filename, caption=caption or None, source="repo",
                    content_type=_content_type(filename), created_at=now()))


def add_upload(location: str, filename: str, raw: bytes, caption: str, uploaded_by: str) -> str:
    data, content_type = shrink_image(raw)
    pid = uuid.uuid4().hex
    with engine().begin() as conn:
        conn.execute(photos.insert().values(
            id=pid, location=location, filename=filename, caption=caption or None, source="upload",
            data=data, content_type=content_type, uploaded_by=uploaded_by, created_at=now()))
    return pid


def list_photos(location: str, include_archived: bool = False) -> list:
    q = select(photos.c.id, photos.c.location, photos.c.filename, photos.c.caption, photos.c.source,
               photos.c.last_used_at, photos.c.archived).where(photos.c.location == location)
    if not include_archived:
        q = q.where(photos.c.archived.is_(False))
    with engine().connect() as conn:
        return list(conn.execute(q.order_by(photos.c.created_at)).mappings())


def get_photo(photo_id: str):
    with engine().connect() as conn:
        return conn.execute(select(photos).where(photos.c.id == photo_id)).mappings().first()


def update_photo(photo_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(update(photos).where(photos.c.id == photo_id).values(**values))


def photo_bytes(photo, locations) -> bytes:
    if photo["source"] == "upload":
        return photo["data"]
    loc = locations.get(photo["location"])
    path = loc.folder / "photos" / photo["filename"] if loc else None
    return path.read_bytes() if path and path.exists() else b""


def _content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")


def shrink_image(raw: bytes, max_side: int = 2048, quality: int = 85) -> tuple[bytes, str]:
    """Re-encodes as JPEG no larger than max_side, honouring EXIF rotation."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"
