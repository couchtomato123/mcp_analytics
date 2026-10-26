"""Turns the raw rows from sources.py into the report model rendered by render.py.

Every number here is computed from fetched rows. When a source failed, its
values are None and the section shows "Data unavailable" (spec §9) instead of
a zero.
"""
import os
from collections import Counter, defaultdict
from datetime import date

from .periods import Period

# Lease_Change_Type__c values that count as new business. The spec suggested
# Opportunity_Change__c, but that field is empty on every Closed Won deal in
# the last 90 days; Lease_Change_Type__c is populated ('New Client', 'Auto
# Renew', 'Switch Down', ...).
NEW_BUSINESS_TYPES = {
    s.strip() for s in os.getenv("NEW_BUSINESS_TYPES", "New Client").split(",") if s.strip()
}
# Salesforce LeadSource values attributed to Google Ads spend. 'Google' (no
# 'Ads') is organic search and is deliberately excluded.
GOOGLE_ADS_LEAD_SOURCES = {
    s.strip() for s in os.getenv("GOOGLE_ADS_LEAD_SOURCES", "Google Ads").split(",") if s.strip()
}
STAGE_ORDER = ["Qualification", "Needs Analysis", "Proposal", "Negotiation", "LOS"]
DISCREPANCY_RATIO = 2.0
UNSPECIFIED = "(not set)"

# (key, label, source, is_currency)
KPI_DEFS = [
    ("new_leads", "New leads", "salesforce", False),
    ("qualified_leads", "Qualified leads", "salesforce", False),
    ("tours", "Tours", "salesforce", False),
    ("signed_deals", "Signed deals (all Closed Won)", "salesforce", False),
    ("new_business_deals", "…of which new business", "salesforce", False),
    ("new_mrr", "New monthly revenue", "salesforce", True),
    ("sessions", "Website sessions (GA4)", "ga4", False),
    ("ad_spend", "Google Ads spend", "google_ads", True),
    ("paid_leads", "Google Ads leads (Salesforce)", "salesforce", False),
]
PRIMARY_KPIS = ["new_leads", "qualified_leads", "tours", "signed_deals", "new_mrr"]


def _by_date(rows, key):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return grouped


def daily_kpis(raw: dict, day: date) -> dict:
    d = day.isoformat()
    out = {key: None for key, *_ in KPI_DEFS}
    sf = raw.get("salesforce")
    if sf is not None:
        leads = [l for l in sf["leads"] if l["created_date"] == d]
        won = [o for o in sf["won_opps"] if o["close_date"] == d]
        out.update(
            new_leads=len(leads),
            qualified_leads=sum(1 for l in leads if l["status"] == "Qualified"),
            tours=None if sf["tours"] is None else sum(1 for t in sf["tours"] if t["date"] == d),
            signed_deals=len(won),
            new_business_deals=sum(1 for o in won if o["lease_change_type"] in NEW_BUSINESS_TYPES),
            new_mrr=sum(o["recurring_revenue"] for o in won),
            paid_leads=sum(1 for l in leads if l["lead_source"] in GOOGLE_ADS_LEAD_SOURCES),
        )
    ga4 = raw.get("ga4")
    if ga4 is not None:
        out["sessions"] = sum(r["sessions"] for r in ga4["daily"] if r["date"] == d)
    ads = raw.get("google_ads")
    if ads is not None:
        out["ad_spend"] = sum(r["spend"] for r in ads["daily"] if r["date"] == d)
    return out


def _sum_days(per_day: list[dict], key: str):
    values = [day[key] for day in per_day]
    return None if any(v is None for v in values) else sum(values)


def _pct_change(actual, expected):
    if actual is None or expected is None or expected == 0:
        return None
    return (actual - expected) / expected


def _ads_totals(raw: dict, days) -> dict | None:
    ads = raw.get("google_ads")
    if ads is None:
        return None
    wanted = {d.isoformat() for d in days}
    rows = [r for r in ads["daily"] if r["date"] in wanted]
    return {
        "spend": sum(r["spend"] for r in rows),
        "clicks": sum(r["clicks"] for r in rows),
        "impressions": sum(r["impressions"] for r in rows),
        # None when conversions weren't fetched (e.g. a hand-built --raw-in file)
        "conversions": None if any(r.get("conversions") is None for r in rows)
        else sum(r["conversions"] for r in rows),
    }


def _discrepancy(platform_conversions, sf_leads) -> bool:
    """Spec §9: flag when the ad platform and Salesforce disagree by > ~2x."""
    if platform_conversions is None or sf_leads is None:
        return False
    if sf_leads == 0:
        return platform_conversions >= DISCREPANCY_RATIO
    ratio = platform_conversions / sf_leads
    return ratio > DISCREPANCY_RATIO or ratio < 1 / DISCREPANCY_RATIO


def build_report(raw: dict, period: Period) -> dict:
    errors = raw.get("errors", {})
    available = {name: raw.get(name) is not None for name in ("salesforce", "ga4", "google_ads")}
    period_days = [daily_kpis(raw, d) for d in period.days]
    baseline_days = [daily_kpis(raw, d) for d in period.baseline_days]
    n = len(period.days)

    comparison = []
    totals = {}
    for key, label, source, is_currency in KPI_DEFS:
        actual = _sum_days(period_days, key)
        baseline_total = _sum_days(baseline_days, key)
        expected = None if baseline_total is None else baseline_total / len(baseline_days) * n
        totals[key] = actual
        comparison.append({
            "key": key, "label": label, "currency": is_currency,
            "actual": actual, "expected": expected, "change": _pct_change(actual, expected),
        })

    sf = raw.get("salesforce")
    period_dates = {d.isoformat() for d in period.days}
    leads = [l for l in sf["leads"] if l["created_date"] in period_dates] if sf else []
    won = [o for o in sf["won_opps"] if o["close_date"] in period_dates] if sf else []

    report = {
        "period": period,
        "errors": errors,
        "available": available,
        "totals": totals,
        "comparison": comparison,
        "per_day": [{"date": d, **k} for d, k in zip(period.days, period_days)] if period.is_multi_day else [],
        "deal_mix": _deal_mix(won) if sf else None,
        "one_time_revenue": sum(o["one_time_revenue"] for o in won) if sf else None,
        "new_business_mrr": sum(o["recurring_revenue"] for o in won if o["lease_change_type"] in NEW_BUSINESS_TYPES) if sf else None,
        "deals": sorted(won, key=lambda o: (-o["recurring_revenue"], -o["one_time_revenue"])) if sf else None,
        "lead_sources": _lead_sources(leads) if sf else None,
        "web_traffic": raw.get("ga4"),
        "product_demand": _product_demand(leads, won) if sf else None,
        "properties": _properties(leads, won) if sf else None,
        "pipeline": _pipeline(sf) if sf else None,
        "paid": _paid(raw, period, totals),
    }
    report["happened"] = _what_happened(report)
    report["priorities"] = _priorities(report, raw, period, leads)
    return report


def _deal_mix(won: list) -> list:
    counts = Counter(o["lease_change_type"] or UNSPECIFIED for o in won)
    return counts.most_common()


def _lead_sources(leads: list) -> list:
    rows = defaultdict(lambda: {"leads": 0, "qualified": 0})
    for l in leads:
        row = rows[l["lead_source"] or UNSPECIFIED]
        row["leads"] += 1
        row["qualified"] += l["status"] == "Qualified"
    return sorted(({"source": k, **v} for k, v in rows.items()), key=lambda r: (-r["leads"], r["source"]))


def _product_demand(leads: list, won: list) -> list:
    rows = defaultdict(lambda: {"leads": 0, "deals": 0, "new_mrr": 0.0})
    for l in leads:
        rows[l["lead_type"] or UNSPECIFIED]["leads"] += 1
    for o in won:
        row = rows[o["client_type"] or UNSPECIFIED]
        row["deals"] += 1
        row["new_mrr"] += o["recurring_revenue"]
    return sorted(({"product": k, **v} for k, v in rows.items()), key=lambda r: (-r["leads"], -r["deals"]))


def _properties(leads: list, won: list) -> list:
    rows = defaultdict(lambda: {"leads": 0, "qualified": 0, "deals": 0, "new_mrr": 0.0})
    for l in leads:
        row = rows[l["property"] or UNSPECIFIED]
        row["leads"] += 1
        row["qualified"] += l["status"] == "Qualified"
    for o in won:
        row = rows[o["property"] or UNSPECIFIED]
        row["deals"] += 1
        row["new_mrr"] += o["recurring_revenue"]
    return sorted(({"property": k, **v} for k, v in rows.items()),
                  key=lambda r: (-r["leads"] - r["deals"], r["property"]))


def _pipeline(sf: dict) -> dict:
    order = {stage: i for i, stage in enumerate(STAGE_ORDER)}
    stages = sorted(sf["pipeline"], key=lambda r: order.get(r["stage"], len(order)))
    return {
        "stages": stages,
        "open_count": sum(r["count"] for r in stages),
        "open_mrr": sum(r["recurring_revenue"] for r in stages),
        "created": sf["opps_created"],
        "closed_lost": sf["closed_lost"],
        "overdue_late_stage": sf["overdue_late_stage"],
    }


def _paid(raw: dict, period: Period, totals: dict) -> dict:
    ads = _ads_totals(raw, period.days)
    baseline_ads = _ads_totals(raw, period.baseline_days)
    sf_leads = totals["paid_leads"]
    paid = {"ads": ads, "sf_leads": sf_leads, "lead_sources": sorted(GOOGLE_ADS_LEAD_SOURCES),
            "campaigns": raw["google_ads"]["campaigns"] if raw.get("google_ads") else None,
            "true_cpl": None, "baseline_cpl": None, "cpc": None, "discrepancy": False}
    if ads is not None:
        paid["cpc"] = ads["spend"] / ads["clicks"] if ads["clicks"] else None
        paid["discrepancy"] = _discrepancy(ads["conversions"], sf_leads)
        if sf_leads:
            paid["true_cpl"] = ads["spend"] / sf_leads
    sf = raw.get("salesforce")
    if baseline_ads is not None and sf is not None:
        baseline_dates = {d.isoformat() for d in period.baseline_days}
        baseline_leads = sum(1 for l in sf["leads"]
                             if l["created_date"] in baseline_dates and l["lead_source"] in GOOGLE_ADS_LEAD_SOURCES)
        if baseline_leads:
            paid["baseline_cpl"] = baseline_ads["spend"] / baseline_leads
    return paid


# ------------------------------ narrative -------------------------------- #
SOURCE_NAMES = {"salesforce": "Salesforce", "ga4": "Google Analytics", "google_ads": "Google Ads"}


def money(value: float) -> str:
    whole = abs(value) >= 100 or float(value).is_integer()
    return f"${value:,.0f}" if whole else f"${value:,.2f}"


def _what_happened(report: dict) -> list:
    bullets = []
    for source, ok in report["available"].items():
        if not ok:
            bullets.append(f"{SOURCE_NAMES[source]} data was unavailable for this run, so its sections are blank.")
    if "salesforce_tours" in report["errors"]:
        bullets.append("Tour counts were unavailable because Salesforce wouldn't return Events; "
                       "all other Salesforce figures are complete.")

    deals = report["deals"]
    if deals is not None:
        nb = report["totals"]["new_business_deals"]
        if deals:
            top = deals[0]
            value = (f"{money(top['recurring_revenue'])}/mo" if top["recurring_revenue"]
                     else f"{money(top['one_time_revenue'])} one-time")
            bullets.append(
                f"{len(deals)} deal{'s' if len(deals) != 1 else ''} closed: {money(report['totals']['new_mrr'])}"
                f" new monthly revenue plus {money(report['one_time_revenue'])} one-time; "
                f"{nb} {'was' if nb == 1 else 'were'} new business. Largest: {top['name']} ({value})."
            )
        else:
            bullets.append("No deals closed.")

    movers = []
    for row in report["comparison"]:
        if row["key"] not in ("new_leads", "qualified_leads", "tours", "sessions", "ad_spend"):
            continue
        change, actual, expected = row["change"], row["actual"], row["expected"]
        if change is None or abs(change) < 0.4:
            continue
        if row["key"] in ("new_leads", "qualified_leads", "tours") and abs(actual - expected) < 2:
            continue
        movers.append((abs(change), row))
    for _, row in sorted(movers, key=lambda m: -m[0])[:2]:
        fmt = money if row["currency"] else (lambda v: f"{v:,.0f}")
        direction = "above" if row["change"] > 0 else "below"
        bullets.append(
            f"{row['label']}: {fmt(row['actual'])}, {abs(row['change']):.0%} {direction} "
            f"the 7-day average pace ({fmt(row['expected'])})."
        )

    sources = report["lead_sources"]
    if sources:
        total = report["totals"]["new_leads"]
        bullets.append(f"Top lead source: {sources[0]['source']} ({sources[0]['leads']} of {total} leads).")

    paid = report["paid"]
    if paid["discrepancy"]:
        bullets.append(
            f"Google Ads reported {paid['ads']['conversions']:,.0f} conversions but Salesforce shows "
            f"{paid['sf_leads']} Google Ads leads; the two disagree by more than {DISCREPANCY_RATIO:.0f}x."
        )
    return bullets[:5]


def _priorities(report: dict, raw: dict, period: Period, leads: list) -> list:
    bullets = []
    sf = raw.get("salesforce")
    if sf is not None:
        today = period.run_date.isoformat()
        tours_today = sum(1 for t in sf["tours"] or [] if t["date"] == today)
        if tours_today:
            bullets.append(f"{tours_today} tour{'s' if tours_today != 1 else ''} on the calendar today. Confirm attendance.")
        untouched = sum(1 for l in leads if l["status"] == "New")
        if untouched:
            bullets.append(f"Follow up on {untouched} lead{'s' if untouched != 1 else ''} from this period still in 'New' status.")
        overdue = report["pipeline"]["overdue_late_stage"]
        if overdue:
            bullets.append(
                f"{overdue} Negotiation/LOS opportunit{'ies are' if overdue != 1 else 'y is'} past the close date. "
                "Update the close date or close them out."
            )

    paid = report["paid"]
    ads = paid["ads"]
    if ads is not None and paid["sf_leads"] is not None:
        if ads["spend"] > 0 and paid["sf_leads"] == 0:
            bullets.append(f"Google Ads spent {money(ads['spend'])} with no Salesforce-attributed leads. Review campaigns and lead tagging.")
        elif paid["true_cpl"] and paid["baseline_cpl"] and paid["true_cpl"] > 1.5 * paid["baseline_cpl"]:
            bullets.append(
                f"Google Ads true CPL rose to {money(paid['true_cpl'])} vs {money(paid['baseline_cpl'])} over the prior 7 days. Review spend."
            )
    if paid["discrepancy"]:
        bullets.append("Reconcile Google Ads conversion tracking with Salesforce LeadSource tagging.")
    return bullets[:5] or ["Nothing urgent flagged by the data."]
