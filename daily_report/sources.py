"""Fetches raw rows from Salesforce, GA4 and Google Ads.

Each fetcher returns plain JSON-serialisable data so a run can be saved
(--raw-out) and re-rendered or audited later (--raw-in). Aggregation happens in
kpis.py, not here.
"""
import logging
import os
import time

import requests

from .periods import Period, local_day_bounds_utc, utc_timestamp_to_local_date

log = logging.getLogger(__name__)

# Per-call cap for Google API requests; their library defaults allow retries for
# up to an hour, which looks like a silent hang in Railway's logs.
GOOGLE_API_TIMEOUT = 60
SF_API_VERSION = os.getenv("SF_API_VERSION", "v61.0")
LATE_STAGES = ("Negotiation", "LOS")


# ------------------------------- Salesforce ------------------------------- #
class SalesforceClient:
    """Minimal REST client using the OAuth 2.0 client-credentials flow of an
    External Client App (or legacy Connected App), which
    runs as the app's configured integration user (no interactive
    login, suitable for an unattended job)."""

    def __init__(self):
        domain = os.environ["SF_DOMAIN_URL"].rstrip("/")  # e.g. https://barrister.my.salesforce.com
        resp = requests.post(
            f"{domain}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ["SF_CLIENT_ID"],
                "client_secret": os.environ["SF_CLIENT_SECRET"],
            },
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Salesforce auth failed ({resp.status_code}): {resp.text[:300]}")
        token = resp.json()
        self.instance_url = token["instance_url"]
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token['access_token']}"

    def query(self, soql: str) -> list:
        url = f"{self.instance_url}/services/data/{SF_API_VERSION}/query"
        params = {"q": soql}
        records = []
        while url:
            resp = self.session.get(url, params=params, timeout=60)
            if not resp.ok:
                raise RuntimeError(f"SOQL failed ({resp.status_code}): {resp.text[:300]}")
            body = resp.json()
            records.extend(body["records"])
            next_url = body.get("nextRecordsUrl")
            url = f"{self.instance_url}{next_url}" if next_url else None
            params = None
        return records


def _rel_name(record: dict, relationship: str):
    related = record.get(relationship)
    return related.get("Name") if related else None


def fetch_salesforce(period: Period, client: SalesforceClient | None = None) -> dict:
    sf = client or SalesforceClient()
    window_first, window_last = period.window_start, period.end
    created_from, created_to = local_day_bounds_utc(window_first, window_last)
    period_from, period_to = local_day_bounds_utc(period.start, period.end)

    leads = sf.query(
        "SELECT Id, CreatedDate, Status, LeadSource, Lead_Type__c, Property__r.Name FROM Lead "
        f"WHERE CreatedDate >= {created_from} AND CreatedDate < {created_to}"
    )
    won = sf.query(
        "SELECT Id, Name, CloseDate, Recurring_Revenue__c, One_Time_Revenue__c, Lease_Change_Type__c, "
        "Opportunity_Client_Type__c, LeadSource, Property__r.Name FROM Opportunity "
        f"WHERE IsWon = true AND CloseDate >= {window_first.isoformat()} AND CloseDate <= {window_last.isoformat()}"
    )
    # Tours run through today so the priorities section can list today's tours.
    tours = sf.query(
        "SELECT Id, ActivityDate, Subject FROM Event WHERE Subject LIKE '%Tour%' "
        f"AND ActivityDate >= {window_first.isoformat()} AND ActivityDate <= {period.run_date.isoformat()}"
    )
    pipeline = sf.query(
        "SELECT StageName, COUNT(Id) cnt, SUM(Recurring_Revenue__c) rr FROM Opportunity "
        "WHERE IsClosed = false GROUP BY StageName"
    )
    opps_created = sf.query(
        f"SELECT COUNT(Id) cnt FROM Opportunity WHERE CreatedDate >= {period_from} AND CreatedDate < {period_to}"
    )
    closed_lost = sf.query(
        "SELECT COUNT(Id) cnt FROM Opportunity WHERE IsClosed = true AND IsWon = false "
        f"AND CloseDate >= {period.start.isoformat()} AND CloseDate <= {period.end.isoformat()}"
    )
    stages = ", ".join(f"'{s}'" for s in LATE_STAGES)
    overdue = sf.query(
        f"SELECT COUNT(Id) cnt FROM Opportunity WHERE IsClosed = false AND StageName IN ({stages}) "
        f"AND CloseDate < {period.run_date.isoformat()}"
    )

    return {
        "leads": [
            {
                "created_date": utc_timestamp_to_local_date(r["CreatedDate"]).isoformat(),
                "status": r.get("Status"),
                "lead_source": r.get("LeadSource"),
                "lead_type": r.get("Lead_Type__c"),
                "property": _rel_name(r, "Property__r"),
            }
            for r in leads
        ],
        "won_opps": [
            {
                "close_date": r["CloseDate"],
                "name": r.get("Name"),
                "recurring_revenue": r.get("Recurring_Revenue__c") or 0.0,
                "one_time_revenue": r.get("One_Time_Revenue__c") or 0.0,
                "lease_change_type": r.get("Lease_Change_Type__c"),
                "client_type": r.get("Opportunity_Client_Type__c"),
                "lead_source": r.get("LeadSource"),
                "property": _rel_name(r, "Property__r"),
            }
            for r in won
        ],
        "tours": [{"date": r["ActivityDate"], "subject": r.get("Subject")} for r in tours],
        "pipeline": [
            {"stage": r["StageName"], "count": r["cnt"], "recurring_revenue": r.get("rr") or 0.0}
            for r in pipeline
        ],
        "opps_created": opps_created[0]["cnt"] if opps_created else 0,
        "closed_lost": closed_lost[0]["cnt"] if closed_lost else 0,
        "overdue_late_stage": overdue[0]["cnt"] if overdue else 0,
    }


# ---------------------------------- GA4 ----------------------------------- #
def _ga4_credentials():
    """Same precedence as main.get_ga4_credentials(), falling back to
    Application Default Credentials (e.g. the Cloud Run job's service account)."""
    refresh_token = os.getenv("GA4_REFRESH_TOKEN") or os.getenv("GOOGLE_ADS_REFRESH_TOKEN")
    scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
    if refresh_token:
        from google.oauth2.credentials import Credentials
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=os.getenv("GA4_CLIENT_ID") or os.getenv("GOOGLE_ADS_CLIENT_ID"),
            client_secret=os.getenv("GA4_CLIENT_SECRET") or os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=scopes,
        )
    import google.auth
    credentials, _ = google.auth.default(scopes=scopes)
    return credentials


def _ga4_rows(response) -> list:
    rows = []
    for row in response.rows:
        record = {h.name: v.value for h, v in zip(response.dimension_headers, row.dimension_values)}
        for h, v in zip(response.metric_headers, row.metric_values):
            record[h.name] = float(v.value)
        rows.append(record)
    return rows


def fetch_ga4(period: Period) -> dict:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, OrderBy, RunReportRequest,
    )

    property_id = os.environ["GA4_PROPERTY_ID"].strip().removeprefix("properties/")
    client = BetaAnalyticsDataClient(credentials=_ga4_credentials())
    window = DateRange(start_date=period.window_start.isoformat(), end_date=period.end.isoformat())
    in_period = DateRange(start_date=period.start.isoformat(), end_date=period.end.isoformat())
    totals = ["sessions", "totalUsers", "newUsers", "keyEvents"]

    def run(date_range, dimensions, metrics, order_metric=None, limit=100):
        request = RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[date_range],
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name=order_metric), desc=True)] if order_metric else [],
            limit=limit,
        )
        return _ga4_rows(client.run_report(request, timeout=GOOGLE_API_TIMEOUT))

    daily = run(window, ["date"], totals)
    for row in daily:  # GA4 returns YYYYMMDD
        d = row.pop("date")
        row["date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return {
        "daily": daily,
        "channels": run(in_period, ["sessionDefaultChannelGroup"], ["sessions", "keyEvents"], "sessions", 10),
        "landing_pages": run(in_period, ["landingPage"], ["sessions", "keyEvents"], "sessions", 5),
    }


# ------------------------------- Google Ads ------------------------------- #
def _google_ads_client():
    from google.ads.googleads.client import GoogleAdsClient
    config = {
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "use_proto_plus": True,
    }
    if os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"):
        config["login_customer_id"] = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID").replace("-", "")
    return GoogleAdsClient.load_from_dict(config)


def fetch_google_ads(period: Period) -> dict:
    customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"].replace("-", "").strip()
    service = _google_ads_client().get_service("GoogleAdsService")

    daily_query = (
        "SELECT segments.date, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions "
        f"FROM customer WHERE segments.date BETWEEN '{period.window_start}' AND '{period.end}'"
    )
    daily = [
        {
            "date": row.segments.date,
            "spend": row.metrics.cost_micros / 1_000_000,
            "clicks": row.metrics.clicks,
            "impressions": row.metrics.impressions,
            "conversions": row.metrics.conversions,
        }
        for row in service.search(customer_id=customer_id, query=daily_query, timeout=GOOGLE_API_TIMEOUT)
    ]

    campaign_query = (
        "SELECT campaign.name, metrics.cost_micros, metrics.clicks, metrics.conversions FROM campaign "
        f"WHERE segments.date BETWEEN '{period.start}' AND '{period.end}' AND metrics.cost_micros > 0 "
        "ORDER BY metrics.cost_micros DESC LIMIT 5"
    )
    campaigns = [
        {
            "name": row.campaign.name,
            "spend": row.metrics.cost_micros / 1_000_000,
            "clicks": row.metrics.clicks,
            "conversions": row.metrics.conversions,
        }
        for row in service.search(customer_id=customer_id, query=campaign_query, timeout=GOOGLE_API_TIMEOUT)
    ]
    return {"daily": daily, "campaigns": campaigns}


# -------------------------------------------------------------------------- #
FETCHERS = {
    "salesforce": fetch_salesforce,
    "ga4": fetch_ga4,
    "google_ads": fetch_google_ads,
}


def fetch_all(period: Period) -> dict:
    """Runs every fetcher; a failing source is recorded under 'errors' and its
    sections render as 'Data unavailable' rather than zeros."""
    raw = {"errors": {}}
    for name, fetch in FETCHERS.items():
        log.info("Fetching %s...", name)
        started = time.monotonic()
        try:
            raw[name] = fetch(period)
            log.info("Fetched %s in %.1fs", name, time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - any failure must degrade to "Data unavailable"
            log.exception("Fetching %s failed", name)
            raw["errors"][name] = f"{type(exc).__name__}: {exc}"
    return raw
