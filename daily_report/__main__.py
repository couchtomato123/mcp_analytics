"""Entry point: python -m daily_report [--date YYYY-MM-DD] [--dry-run] ..."""
import argparse
import json
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

from .kpis import build_report
from .mailer import send_email
from .periods import build_period, local_hour, today_local
from .render import render_html, render_text, subject
from .sources import fetch_all

DEFAULT_RECIPIENTS = "leasing@barrister-suites.com"


def main(argv=None) -> int:
    load_dotenv()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Barrister Daily Marketing + Sales Brief")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="Report on this single day (YYYY-MM-DD) instead of the scheduled period")
    parser.add_argument("--to", default=os.getenv("REPORT_RECIPIENTS", DEFAULT_RECIPIENTS),
                        help="Comma-separated recipients (default: REPORT_RECIPIENTS or %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="Write the email to --html-out instead of sending it")
    parser.add_argument("--html-out", default="daily_report.html", help="Where --dry-run writes the HTML")
    parser.add_argument("--raw-out", help="Save the fetched source data as JSON (for audits)")
    parser.add_argument("--raw-in", help="Render from a saved --raw-out file instead of calling the APIs")
    parser.add_argument("--run-date", type=date.fromisoformat, help=argparse.SUPPRESS)  # testing: pretend today is this
    args = parser.parse_args(argv)

    # Railway cron is UTC-only, so it fires at both 14:00 and 15:00 UTC and
    # this guard keeps only the run that lands at 7 AM Pacific (PDT or PST).
    send_hour = os.getenv("REPORT_SEND_HOUR")
    scheduled = not (args.date or args.dry_run or args.raw_in or args.run_date)
    if scheduled and send_hour and local_hour() != int(send_hour):
        logging.info("Not %s:00 in Los Angeles yet/anymore; skipping this run", send_hour)
        return 0

    period = build_period(args.run_date or today_local(), args.date)
    if args.raw_in:
        with open(args.raw_in) as f:
            raw = json.load(f)
    else:
        raw = fetch_all(period)
    if args.raw_out:
        with open(args.raw_out, "w") as f:
            json.dump(raw, f, indent=2, default=str)

    report = build_report(raw, period)
    html, text, subj = render_html(report), render_text(report), subject(report)
    recipients = [r.strip() for r in args.to.split(",") if r.strip()]

    sent, send_error = False, None
    if args.dry_run:
        with open(args.html_out, "w") as f:
            f.write(html)
        logging.info("Dry run: wrote %s", args.html_out)
    else:
        try:
            send_email(subj, html, text, recipients)
            sent = True
        except Exception as exc:  # noqa: BLE001 - logged below, then fail the job
            logging.exception("Sending the email failed")
            send_error = f"{type(exc).__name__}: {exc}"

    sf = raw.get("salesforce") or {}
    # One structured line per run (spec §9); Cloud Logging indexes it as JSON.
    print(json.dumps({
        "event": "daily_report_run",
        "status": "failed" if send_error else ("partial" if raw.get("errors") else "ok"),
        "period": [d.isoformat() for d in period.days],
        "recipients": recipients,
        "email_sent": sent,
        "dry_run": args.dry_run,
        "row_counts": {
            "sf_leads": len(sf.get("leads", [])),
            "sf_won_opps": len(sf.get("won_opps", [])),
            "sf_tours": len(sf.get("tours", [])),
            "ga4_daily": len((raw.get("ga4") or {}).get("daily", [])),
            "google_ads_daily": len((raw.get("google_ads") or {}).get("daily", [])),
        },
        "source_errors": raw.get("errors", {}),
        "send_error": send_error,
    }))
    # Only a failed send fails the job, so a retry never double-sends a report
    # that went out with one source marked unavailable.
    return 1 if send_error else 0


if __name__ == "__main__":
    sys.exit(main())
