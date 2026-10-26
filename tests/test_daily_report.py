"""Run with: python -m unittest discover -s tests"""
import copy
import unittest
from datetime import date

from daily_report.kpis import build_report
from daily_report.periods import build_period, local_day_bounds_utc
from daily_report.render import UNAVAILABLE, render_html, subject
from daily_report.sources import fetch_salesforce


def lead(day, status="New", source="Google Ads", lead_type="Physical Tenant", prop="P44 - Comerica Bank Building"):
    return {"created_date": day, "status": status, "lead_source": source, "lead_type": lead_type, "property": prop}


def won(day, rr, change="New Client", one_time=0.0, name="Deal"):
    return {"close_date": day, "name": name, "recurring_revenue": rr, "one_time_revenue": one_time,
            "lease_change_type": change, "client_type": "Virtual Client", "lead_source": None,
            "property": "P08 - The Torrance"}


def make_raw():
    """Thursday 2026-10-15 is the reported day; Oct 8-14 is the baseline."""
    baseline = [f"2026-10-{d:02d}" for d in range(8, 15)]
    return {
        "errors": {},
        "salesforce": {
            "leads": [lead(d) for d in baseline] + [
                lead("2026-10-15", status="Qualified"),
                lead("2026-10-15", source="Facebook", lead_type="Virtual Client"),
                lead("2026-10-15", source="Google"),
            ],
            "won_opps": [
                won("2026-10-15", 200.0, name="New office"),
                won("2026-10-15", 1500.0, change="Auto Renew", name="Renewal"),
                won("2026-10-15", 0.0, change=None, one_time=297.5, name="Day pass"),
                won("2026-10-10", 700.0),
            ],
            "tours": [{"date": "2026-10-15", "subject": "Tour"}, {"date": "2026-10-16", "subject": "P44 Tour"}],
            "pipeline": [{"stage": "LOS", "count": 2, "recurring_revenue": 900.0},
                         {"stage": "Qualification", "count": 5, "recurring_revenue": 100.0}],
            "opps_created": 3, "closed_lost": 1, "overdue_late_stage": 4,
        },
        "ga4": {
            "daily": [{"date": d, "sessions": 100.0, "totalUsers": 90.0, "newUsers": 80.0, "keyEvents": 5.0}
                      for d in baseline + ["2026-10-15"]],
            "channels": [{"sessionDefaultChannelGroup": "Paid Search", "sessions": 100.0, "keyEvents": 5.0}],
            "landing_pages": [{"landingPage": "/", "sessions": 60.0, "keyEvents": 2.0}],
        },
        "google_ads": {
            "daily": [{"date": d, "spend": 70.0, "clicks": 10, "impressions": 500, "conversions": 1.0}
                      for d in baseline] + [{"date": "2026-10-15", "spend": 100.0, "clicks": 20, "impressions": 900,
                                             "conversions": 5.0}],
            "campaigns": [{"name": "Search <Brand>", "spend": 100.0, "clicks": 20, "conversions": 5.0}],
        },
    }


class PeriodTests(unittest.TestCase):
    def test_tuesday_reports_monday(self):
        p = build_period(date(2026, 10, 13))
        self.assertEqual(p.days, (date(2026, 10, 12),))
        self.assertEqual(p.baseline_days[0], date(2026, 10, 5))
        self.assertEqual(len(p.baseline_days), 7)

    def test_monday_rolls_up_weekend(self):
        p = build_period(date(2026, 10, 19))
        self.assertEqual(p.days, (date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)))
        self.assertEqual(p.baseline_days[-1], date(2026, 10, 15))
        self.assertEqual(p.label(), "Fri, Oct 16 – Sun, Oct 18, 2026")

    def test_explicit_date(self):
        p = build_period(date(2026, 10, 19), date(2026, 10, 1))
        self.assertEqual(p.days, (date(2026, 10, 1),))
        self.assertEqual(p.run_date, date(2026, 10, 2))

    def test_pacific_day_bounds_follow_dst(self):
        self.assertEqual(local_day_bounds_utc(date(2026, 10, 15), date(2026, 10, 15)),
                         ("2026-10-15T07:00:00Z", "2026-10-16T07:00:00Z"))
        self.assertEqual(local_day_bounds_utc(date(2026, 11, 2), date(2026, 11, 2)),
                         ("2026-11-02T08:00:00Z", "2026-11-03T08:00:00Z"))


class FakeSalesforce:
    def __init__(self):
        self.queries = []

    def query(self, soql):
        self.queries.append(soql)
        if soql.startswith("SELECT Id, CreatedDate"):
            # 00:14 UTC on the 16th is still the evening of the 15th in Los Angeles.
            return [{"CreatedDate": "2026-10-16T00:14:18.000+0000", "Status": "New", "LeadSource": None,
                     "Lead_Type__c": "Service Only", "Property__r": None}]
        if "FROM Opportunity WHERE IsWon" in soql:
            return [{"CloseDate": "2026-10-15", "Name": "X", "Recurring_Revenue__c": None, "One_Time_Revenue__c": 35,
                     "Lease_Change_Type__c": "New Client", "Opportunity_Client_Type__c": "One Time User",
                     "LeadSource": "LiquidSpace", "Property__r": {"Name": "P98 - Corporate Point Calabasas"}}]
        if "FROM Event" in soql:
            return [{"ActivityDate": "2026-10-15", "Subject": "Tour"}]
        if "GROUP BY StageName" in soql:
            return [{"StageName": "LOS", "cnt": 29, "rr": None}]
        return [{"cnt": 7}]


class SalesforceTransformTests(unittest.TestCase):
    def test_rows_are_normalised_to_pacific_dates(self):
        fake = FakeSalesforce()
        sf = fetch_salesforce(build_period(date(2026, 10, 16)), client=fake)
        self.assertEqual(sf["leads"][0]["created_date"], "2026-10-15")
        self.assertIsNone(sf["leads"][0]["property"])
        self.assertEqual(sf["won_opps"][0]["recurring_revenue"], 0.0)
        self.assertEqual(sf["won_opps"][0]["property"], "P98 - Corporate Point Calabasas")
        self.assertEqual(sf["pipeline"], [{"stage": "LOS", "count": 29, "recurring_revenue": 0.0}])
        self.assertEqual((sf["opps_created"], sf["closed_lost"], sf["overdue_late_stage"]), (7, 7, 7))
        lead_query = fake.queries[0]
        self.assertIn("CreatedDate >= 2026-10-08T07:00:00Z AND CreatedDate < 2026-10-16T07:00:00Z", lead_query)
        self.assertIn("ActivityDate <= 2026-10-16", fake.queries[2])  # tours through today


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.period = build_period(date(2026, 10, 16))

    def test_kpis(self):
        r = build_report(make_raw(), self.period)
        t = r["totals"]
        self.assertEqual(t["new_leads"], 3)
        self.assertEqual(t["qualified_leads"], 1)
        self.assertEqual(t["tours"], 1)  # today's tour is not yesterday's
        self.assertEqual(t["signed_deals"], 3)
        self.assertEqual(t["new_business_deals"], 1)
        self.assertEqual(t["new_mrr"], 1700.0)
        self.assertEqual(r["new_business_mrr"], 200.0)
        self.assertEqual(r["one_time_revenue"], 297.5)
        self.assertEqual(t["paid_leads"], 1)  # 'Google' (organic) is not Google Ads
        self.assertEqual(r["paid"]["true_cpl"], 100.0)
        self.assertEqual(r["paid"]["baseline_cpl"], 70.0)
        cmp = {row["key"]: row for row in r["comparison"]}
        self.assertAlmostEqual(cmp["signed_deals"]["expected"], 1 / 7)

    def test_priorities_use_today(self):
        r = build_report(make_raw(), self.period)
        self.assertIn("1 tour on the calendar today. Confirm attendance.", r["priorities"])
        self.assertTrue(any("4 Negotiation/LOS" in b for b in r["priorities"]))
        self.assertLessEqual(len(r["happened"]), 5)
        self.assertLessEqual(len(r["priorities"]), 5)

    def test_discrepancy_flagged_when_platform_exceeds_2x(self):
        raw = make_raw()
        r = build_report(raw, self.period)  # 5 platform conversions vs 1 Salesforce lead
        self.assertTrue(r["paid"]["discrepancy"])
        self.assertIn("differ by more than 2x", render_html(r))
        raw["google_ads"]["daily"][-1]["conversions"] = 2.0  # exactly 2x
        self.assertFalse(build_report(raw, self.period)["paid"]["discrepancy"])
        raw["google_ads"]["daily"][-1]["conversions"] = 0.4  # 2.5x lower
        self.assertTrue(build_report(raw, self.period)["paid"]["discrepancy"])

    def test_unavailable_source_is_not_zero(self):
        raw = make_raw()
        del raw["google_ads"]
        raw["errors"]["google_ads"] = "PermissionDenied: nope"
        r = build_report(raw, self.period)
        self.assertIsNone(r["totals"]["ad_spend"])
        self.assertIsNone(r["paid"]["ads"])
        html = render_html(r)
        self.assertIn(UNAVAILABLE, html)
        self.assertIn("Google Ads data was unavailable", html)
        self.assertNotIn("Google Ads spent", html)

    def test_salesforce_down_keeps_ga4(self):
        raw = make_raw()
        del raw["salesforce"]
        r = build_report(raw, self.period)
        self.assertIsNone(r["totals"]["new_leads"])
        self.assertEqual(r["totals"]["sessions"], 100.0)
        self.assertIsNone(r["paid"]["true_cpl"])
        render_html(r)

    def test_monday_breaks_out_each_day(self):
        raw = make_raw()
        raw["salesforce"]["leads"] += [lead("2026-10-16"), lead("2026-10-17"), lead("2026-10-18")]
        r = build_report(raw, build_period(date(2026, 10, 19)))
        self.assertEqual([d["new_leads"] for d in r["per_day"]], [1, 1, 1])
        self.assertEqual(r["totals"]["new_leads"], 3)
        self.assertIn("By day", render_html(r))

    def test_render(self):
        r = build_report(make_raw(), self.period)
        html = render_html(r)
        self.assertEqual(subject(r), "Barrister Daily Marketing + Sales Brief | Thu, Oct 15, 2026")
        self.assertIn("Search &lt;Brand&gt;", html)  # escaped
        self.assertIn("never used to calculate CPL", html)  # CPL caveat always shown
        for section in ("at a Glance", "vs 7-Day Average", "Lead Sources", "Product Demand", "Property Performance",
                        "Sales Pipeline", "Paid Marketing", "What Happened Yesterday", "Today&#x27;s Priorities"):
            self.assertIn(section, html)

    def test_report_does_not_mutate_raw(self):
        raw = make_raw()
        before = copy.deepcopy(raw)
        build_report(raw, self.period)
        self.assertEqual(raw, before)


class SettingsTests(unittest.TestCase):
    def test_missing_variable_is_named_in_errors(self):
        from unittest import mock
        from daily_report import sources
        env = {k: v for k, v in __import__("os").environ.items() if k != "GA4_PROPERTY_ID"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.dict(sources.FETCHERS, {"ga4": sources.fetch_ga4}, clear=True):
            raw = sources.fetch_all(build_period(date(2026, 10, 16)))
        self.assertIn("GA4_PROPERTY_ID is not set", raw["errors"]["ga4"])


class ScheduleGuardTests(unittest.TestCase):
    def test_skips_the_utc_run_that_is_not_7am_pacific(self):
        from unittest import mock
        from daily_report import __main__ as cli
        with mock.patch.dict("os.environ", {"REPORT_SEND_HOUR": "7"}), \
                mock.patch.object(cli, "local_hour", return_value=8), \
                mock.patch.object(cli, "fetch_all") as fetch:
            self.assertEqual(cli.main([]), 0)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
