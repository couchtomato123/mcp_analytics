"""Platform rules, content pillars and settings read from the environment."""
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("CONTENT_TZ", "America/Los_Angeles"))
ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_DIR = Path(os.getenv("LOCATIONS_DIR", ROOT / "locations"))

MODEL = os.getenv("CONTENT_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CONTENT_EFFORT", "medium")
MAX_PHOTOS_PER_RUN = int(os.getenv("MAX_PHOTOS_PER_RUN", "8"))


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    max_chars: int
    hashtags: str          # guidance for the model
    style: str             # guidance for the model
    needs_photo: bool
    hour: int              # default local posting hour
    ayrshare: str          # Ayrshare platform name


PLATFORMS = {p.key: p for p in [
    Platform("facebook", "Facebook", 2000, "0-3 hashtags at the end",
             "Friendly and local, 40-120 words, one clear call to action.", False, 10, "facebook"),
    Platform("instagram", "Instagram", 2200, "5-10 relevant local and industry hashtags at the end",
             "Visual-first: the first line must hook; short paragraphs; emojis sparingly.", True, 12, "instagram"),
    Platform("linkedin", "LinkedIn", 3000, "2-4 professional hashtags at the end",
             "Professional, value-led for founders, attorneys and small firms; 60-150 words; no emojis.",
             False, 9, "linkedin"),
    Platform("google_business", "Google Business Profile", 1500, "no hashtags",
             "Local-search friendly: name the neighborhood and building, 40-90 words, end with how to book a tour.",
             False, 11, "gmb"),
    Platform("x", "X", 280, "0-2 hashtags, all inside the 280 characters",
             "One punchy sentence or two; must fit in 280 characters including any link.", False, 13, "twitter"),
]}

# Rotated week by week so each location's feed stays varied.
PILLARS = [
    "Space spotlight: a specific office, conference room, lounge or view shown in a photo",
    "Neighborhood: nearby restaurants, transit, freeways, landmarks named in the profile",
    "Who works here: the kinds of businesses the location suits (attorneys, startups, remote teams), no invented clients",
    "Service highlight: virtual office, mail handling, receptionist, meeting rooms by the hour, coworking",
    "Practical tip: productivity, hybrid work or running a small firm, tied back to the space",
    "Invitation: book a tour or a day office, with the booking link",
]

# Spread a platform's weekly posts over these weekdays (0 = Monday).
POSTING_DAYS = [1, 3, 0, 2, 4, 5, 6]

STATUSES = ("draft", "approved", "scheduled", "published", "rejected", "failed")
