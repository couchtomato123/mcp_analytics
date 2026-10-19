# Barrister Daily Marketing + Sales Brief

Every weekday at 7:00 AM Pacific this job pulls Salesforce, GA4 and Google Ads
data, computes the funnel KPIs from the build spec, and emails the brief to
`leasing@barrister-suites.com`.

- **Tue–Fri** cover the previous day.
- **Monday** covers Fri + Sat + Sun as one total, with a by-day breakdown.
- Every period is compared with the 7 days before it, scaled to the period's length.

## Run it

```bash
pip install -r requirements.txt
python -m daily_report --dry-run --html-out brief.html   # fetch live data, write HTML, send nothing
python -m daily_report --date 2026-10-01 --dry-run       # any historical day (spec §10 step 1)
python -m daily_report --raw-out run.json                # send, and keep the source rows for audit
python -m daily_report --raw-in run.json --dry-run       # re-render a saved run without calling APIs
python -m unittest discover -s tests -t .
```

Each run prints one JSON line (`"event": "daily_report_run"`) with the status,
row counts per source and any source errors. In Cloud Run this lands in Cloud
Logging, which gives the audit log required by spec §9.

## Scheduler: Cloud Run Job + Cloud Scheduler

`deploy/daily_report.sh` builds this repo's Dockerfile into a Cloud Run Job
and creates a Cloud Scheduler trigger (`0 7 * * 1-5`, `America/Los_Angeles`).

- **Why this setup:** Scheduler has a retry-backed SLA and native time-zone and
  DST support. GitHub Actions cron is UTC-only and often runs 10–60 minutes
  late. Railway cron is UTC-only too.
- **Cost:** essentially free.
  - Cloud Scheduler: first 3 jobs per billing account are free.
  - Cloud Run Jobs: about 22 one-minute runs a month, inside the free tier.
  - Secret Manager: about $0.40 a month for 7 secrets.
- **Retries:** the job exits non-zero only when the email fails to send, and
  then retries once. A run with one source down still sends, with that
  section marked "Data unavailable", and is never re-sent.

## Credentials

| Variable | Where it comes from |
|---|---|
| `SF_DOMAIN_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` | Salesforce External Client App with **Client Credentials Flow** enabled and a read-only integration user set as "Run As" (see below) |
| `GA4_PROPERTY_ID` | GA4 property ID. Auth reuses `GOOGLE_ADS_REFRESH_TOKEN` (same as `main.py`), or the job's service account if no refresh token is set; add that service account as a Viewer on the property |
| `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_DEVELOPER_TOKEN` (optional `GOOGLE_ADS_LOGIN_CUSTOMER_ID` for an MCC) | Same values the MCP server already uses |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `REPORT_FROM` | Any SMTP relay. For Google Workspace: `smtp.gmail.com:587` with an app password for the sending mailbox. SendGrid (`smtp.sendgrid.net`, user `apikey`) and Amazon SES SMTP also work |
| `REPORT_RECIPIENTS` | Comma-separated. Defaults to `leasing@barrister-suites.com` |

The Salesforce *SObject Read* connector in Claude is tied to an interactive
user session, so it can't drive an unattended job. It was used to verify every
query here against live data. The job needs its own External Client App (spec §7 says Connected App, which Salesforce no longer allows creating).

### Salesforce External Client App setup

Since Spring '26, Salesforce no longer lets orgs create new Connected Apps
without a Support case. Use an **External Client App** instead. It uses the
same `/services/oauth2/token` client-credentials call, so no code changes.

1. In Setup, open **External Client App Manager → New External Client App**.
   Set Distribution State to **Local**.
2. Under **API (Enable OAuth Settings)**, tick **Enable OAuth** and add the
   scope **Manage user data via APIs (`api`)**. A callback URL is required but
   unused; any HTTPS URL works. Then, further down under **Flow
   Enablement**, tick **Enable Client Credentials Flow** and confirm the
   dialog. This must be done before step 3, or the Policies checkbox stays
   greyed out. On an existing app, it's under **Settings → Edit → OAuth
   Settings → Flow Enablement**.
3. Open the app's **Policies** tab and click **Edit**. Under **OAuth
   Policies → OAuth Flows and External Client App Enhancements**, tick
   **Enable Client Credentials Flow** and set **Run As** to a dedicated
   integration user (the free "Salesforce Integration" license works) with
   read-only access to Lead, Opportunity, Event and Property__c. If the
   checkbox is still greyed out, your admin profile lacks the
   External Client App edit-policies permission.
4. On the **Settings** tab, open **OAuth Settings → Consumer Key and Secret**
   and copy them into `SF_CLIENT_ID` and `SF_CLIENT_SECRET`.
5. Set `SF_DOMAIN_URL` to the org's My Domain URL. The client-credentials
   flow rejects `login.salesforce.com`.

## Where this differs from the spec (verified against live data, Oct 2026)

- **New business uses `Lease_Change_Type__c = 'New Client'`, not
  `Opportunity_Change__c`.** `Opportunity_Change__c` is empty on all 401
  Closed Won deals in the last 90 days. `Lease_Change_Type__c` is populated:
  166 New Client, 120 Auto Renew, 20 Renewal Negotiated, 18 Switch Up, and so
  on. This is configurable with `NEW_BUSINESS_TYPES`.
- **Google Ads CPL counts `LeadSource = 'Google Ads'` only.** `Google`
  (55 leads in 30 days) looks like organic search and is excluded.
  Configurable with `GOOGLE_ADS_LEAD_SOURCES`.
- **Some Closed Won deals have no `Lease_Change_Type__c`.** These are one-time
  day-pass bookings with $0 recurring revenue. They count as signed deals
  (`IsWon = true`) but not as new business. The email shows their one-time
  revenue separately.
- **Days use Pacific time, not the API user's time zone.** Lead `CreatedDate`
  is bucketed by Pacific calendar day rather than SOQL `YESTERDAY`, so the
  count doesn't depend on the integration user's time-zone setting.
- **Product demand** uses `Lead.Lead_Type__c` for leads and
  `Opportunity_Client_Type__c` for deals.
- **"What Happened" and "Today's Priorities"** come from rules applied to the
  fetched numbers, such as large moves vs the 7-day pace, uncontacted leads,
  overdue late-stage deals, and CPL spikes. No narrative is invented.
