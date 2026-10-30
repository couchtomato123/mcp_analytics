"""python -m social_content {generate,serve,seed-from-salesforce}"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from . import db
from .config import TZ


def cmd_generate(args) -> int:
    from . import generator, notify
    from .locations import load_locations

    locations = load_locations()
    db.sync_repo_photos(locations)
    if args.location:
        missing = set(args.location) - set(locations)
        if missing:
            logging.error("Unknown location(s): %s", ", ".join(sorted(missing)))
            return 1
        targets = [locations[s] for s in args.location]
    else:
        targets = [loc for loc in locations.values() if loc.active]
    week_of = args.week or generator.next_monday(datetime.now(TZ).date())
    if week_of.weekday() != 0:
        logging.error("--week must be a Monday")
        return 1
    logging.info("Drafting week of %s for %s location(s)", week_of, len(targets))

    failures, summary = [], {}
    for loc in targets:
        if not args.force and not args.dry_run and db.week_exists(loc.slug, week_of):
            logging.info("%s: week of %s already drafted; skipping (use --force to add another set)",
                         loc.slug, week_of)
            continue
        try:
            rows = generator.draft_week(loc, week_of, locations)
        except Exception:  # noqa: BLE001 - one location failing shouldn't stop the rest
            logging.exception("%s: drafting failed", loc.slug)
            failures.append(loc.slug)
            continue
        summary[loc.slug] = len(rows)
        if args.dry_run:
            print(json.dumps(rows, indent=2, default=str))
            continue
        db.insert_posts(rows)
        logging.info("%s: saved %s drafts", loc.slug, len(rows))
        if rows and not args.no_email:
            try:
                notify.send_drafts_ready(loc, rows)
            except Exception:  # noqa: BLE001 - drafts are saved; the email is a nicety
                logging.exception("%s: emailing reviewers failed", loc.slug)

    print(json.dumps({"event": "social_drafts_run", "week_of": week_of.isoformat(), "drafts": summary,
                      "failed": failures, "dry_run": args.dry_run}))
    return 1 if failures else 0


def cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*")
    return 0


def cmd_seed(args) -> int:
    from .seed import seed_from_salesforce, write_profiles

    if args.from_json:
        with open(args.from_json) as f:
            created = write_profiles(json.load(f))
    else:
        created = seed_from_salesforce()
    logging.info("Refreshed Salesforce facts; created %s new profile(s): %s", len(created), ", ".join(created))
    return 0


def main(argv=None) -> int:
    load_dotenv()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m social_content", description="Barrister social content studio")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Draft next week's posts for every active location")
    gen.add_argument("--location", action="append", help="Only this location folder (repeatable; ignores active:)")
    gen.add_argument("--week", type=date.fromisoformat, help="Monday of the week to draft (default: next Monday)")
    gen.add_argument("--dry-run", action="store_true", help="Print the drafts instead of saving and emailing")
    gen.add_argument("--force", action="store_true", help="Draft even if the week already has drafts")
    gen.add_argument("--no-email", action="store_true", help="Save drafts without emailing reviewers")
    gen.set_defaults(func=cmd_generate)

    srv = sub.add_parser("serve", help="Run the review web app")
    srv.set_defaults(func=cmd_serve)

    seed = sub.add_parser("seed-from-salesforce", help="Create/refresh location folders from Property__c")
    seed.add_argument("--from-json", help="Use saved Property__c records instead of calling Salesforce")
    seed.set_defaults(func=cmd_seed)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
