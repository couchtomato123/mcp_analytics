"""Creates a folder per active Salesforce Property__c and refreshes its facts.

salesforce.yaml is rewritten on every run. profile.yaml is only created when
missing, so reviewers, voice and platform choices are never overwritten.
"""
import html
import logging
import re

import yaml

from .config import LOCATIONS_DIR
from .locations import slugify

log = logging.getLogger(__name__)

SOQL = ("SELECT Id, Name, Billing_Street__c, Billing_City__c, Billing_State__c, Billing_Postal_Code__c, "
        "Amenities__c, Points_Of_Interest__c, Description__c, Parking_Availability__c "
        "FROM Property__c WHERE Property_Status__c = true ORDER BY Name")

PROFILE_TEMPLATE = """\
# {name}: edit freely. Facts from Salesforce live in salesforce.yaml next to this file.
code: {code}
name: {name}
active: false            # true = draft posts for this location every week

# People at this location who edit and approve posts (they sign in with this email).
reviewers: []

# Posts per week on each platform. Remove a line to skip that platform.
platforms:
  facebook: 2
  instagram: 2
  linkedin: 1
  google_business: 1

# How this location sounds. A sentence or two from the reviewer helps a lot,
# e.g. "Warm and neighborly; we know every tenant by name. Lots of film/TV people."
voice: ""

# Facts Claude may use that aren't in Salesforce: current offers, events,
# what makes this floor special, the reviewer's name to sign posts with.
extra_facts: []

booking_link: ""         # tour or day-office booking URL for this location
"""


def clean_html(value) -> list[str]:
    """Salesforce rich text -> list of plain lines, bullets stripped."""
    if not value:
        return []
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", str(value))
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("﻿", "")
    lines = [re.sub(r"^[\s•\-\*]+", "", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def facts_from_property(rec: dict) -> dict:
    one = lambda field: " ".join(clean_html(rec.get(field)))  # noqa: E731
    address = ", ".join(x for x in (one("Billing_Street__c"), one("Billing_City__c"),
                                    f"{one('Billing_State__c')} {one('Billing_Postal_Code__c')}".strip()) if x)
    facts = {
        "salesforce_name": rec["Name"],
        "address": address,
        "city": one("Billing_City__c"),
        "parking_available": bool(rec.get("Parking_Availability__c")),
        "amenities": clean_html(rec.get("Amenities__c")),
        "nearby": clean_html(rec.get("Points_Of_Interest__c")),
        "description": clean_html(rec.get("Description__c")),
    }
    return {k: v for k, v in facts.items() if v not in ("", [], None)}


def split_name(sf_name: str) -> tuple[str, str]:
    """'P44 - Comerica Bank Building' -> ('P44', 'Comerica Bank Building')."""
    code, _, name = sf_name.partition(" - ")
    return (code.strip(), name.strip()) if name else (slugify(sf_name)[:8].upper(), sf_name.strip())


def write_profiles(records: list[dict]) -> list[str]:
    LOCATIONS_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for rec in records:
        code, name = split_name(rec["Name"])
        folder = LOCATIONS_DIR / slugify(f"{code} {name}")
        (folder / "photos").mkdir(parents=True, exist_ok=True)
        keep = folder / "photos" / ".gitkeep"
        if not any(folder.joinpath("photos").iterdir()):
            keep.touch()
        header = "# Copied from Salesforce Property__c by `python -m social_content seed-from-salesforce`.\n" \
                 "# Edits here are overwritten; fix the facts in Salesforce or add to profile.yaml.\n"
        (folder / "salesforce.yaml").write_text(
            header + yaml.safe_dump(facts_from_property(rec), sort_keys=False, allow_unicode=True, width=110))
        profile = folder / "profile.yaml"
        if not profile.exists():
            profile.write_text(PROFILE_TEMPLATE.format(code=code, name=name))
            created.append(folder.name)
    return created


def seed_from_salesforce() -> list[str]:
    from daily_report.sources import SalesforceClient

    records = SalesforceClient().query(SOQL)
    log.info("Salesforce returned %s active properties", len(records))
    return write_profiles(records)
