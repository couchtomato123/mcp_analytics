"""Email magic-link sign-in. No passwords: a reviewer is anyone listed in a
location's profile.yaml, plus ADMIN_EMAILS, who can see every location."""
import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

LINK_MAX_AGE = int(os.getenv("LOGIN_LINK_DAYS", "7")) * 86400


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt="social-login")


def secret_key() -> str:
    key = os.getenv("SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("SECRET_KEY is not set. Add a long random value to this service's variables in Railway.")
    return key


def admins() -> set[str]:
    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def allowed_locations(email: str, locations: dict) -> list[str]:
    email = (email or "").strip().lower()
    if not email:
        return []
    if email in admins():
        return list(locations)
    return [slug for slug, loc in locations.items() if email in loc.reviewers]


def make_token(email: str) -> str:
    return _serializer().dumps(email.strip().lower())


def read_token(token: str) -> str | None:
    try:
        return _serializer().loads(token, max_age=LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
