"""Loads location profiles from locations/<slug>/profile.yaml.

Each location folder holds:
  profile.yaml   facts, voice, platforms and reviewer emails (see locations/README.md)
  salesforce.yaml  address and amenities copied from Salesforce Property__c
  photos/        optional JPG/PNG/WEBP files; more can be uploaded in the review app
  photos.yaml    optional {filename: caption} so Claude knows what a photo shows
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import LOCATIONS_DIR, PLATFORMS

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Location:
    slug: str
    code: str
    name: str
    active: bool
    profile: dict
    reviewers: list = field(default_factory=list)
    platforms: dict = field(default_factory=dict)   # platform key -> posts per week
    folder: Path = None

    @property
    def ayrshare_profile_key(self) -> str:
        """Per-location Ayrshare profile key, kept in Railway variables, not the repo."""
        return os.getenv(f"AYRSHARE_PROFILE_{self.code.upper()}", "").strip()

    def repo_photos(self) -> list[tuple[str, Path, str]]:
        """(filename, path, caption) for photos committed under photos/."""
        folder = self.folder / "photos"
        if not folder.is_dir():
            return []
        captions = {}
        captions_file = self.folder / "photos.yaml"
        if captions_file.exists():
            captions = yaml.safe_load(captions_file.read_text()) or {}
        return [(p.name, p, str(captions.get(p.name, "")))
                for p in sorted(folder.iterdir()) if p.suffix.lower() in PHOTO_EXTS]


def load_locations(include_inactive: bool = True) -> dict[str, Location]:
    out = {}
    for folder in sorted(LOCATIONS_DIR.iterdir()) if LOCATIONS_DIR.is_dir() else []:
        path = folder / "profile.yaml"
        if folder.name.startswith("_") or not path.exists():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        facts = folder / "salesforce.yaml"   # written by seed-from-salesforce; never hand-edited
        if facts.exists():
            data["salesforce_facts"] = yaml.safe_load(facts.read_text()) or {}
        platforms = {k: int(v) for k, v in (data.get("platforms") or {}).items() if int(v or 0) > 0}
        unknown = set(platforms) - set(PLATFORMS)
        if unknown:
            raise ValueError(f"{path}: unknown platform(s) {sorted(unknown)}; use {sorted(PLATFORMS)}")
        loc = Location(
            slug=folder.name,
            code=str(data.get("code") or folder.name.split("-")[0]).upper(),
            name=data.get("name") or folder.name,
            active=bool(data.get("active", False)),
            profile=data,
            reviewers=[e.strip().lower() for e in data.get("reviewers") or [] if e and e.strip()],
            platforms=platforms,
            folder=folder,
        )
        if include_inactive or loc.active:
            out[loc.slug] = loc
    return out


def brand() -> dict:
    """Company-wide voice and rules from locations/_brand.yaml."""
    path = LOCATIONS_DIR / "_brand.yaml"
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
