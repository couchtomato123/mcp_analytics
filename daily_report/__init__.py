"""Barrister Daily Marketing + Sales Brief.

A scheduled job (see deploy/daily_report.sh) that pulls the reporting period's
Salesforce, GA4 and Google Ads data, computes the funnel KPIs and emails an
executive summary. Run with `python -m daily_report --help`.
"""
