# Barrister Social Studio

Drafts social posts for every location, lets each location's reviewer put them in
their own words, and publishes them to that location's own accounts.

```
locations/<slug>/profile.yaml ─┐
Salesforce Property__c ────────┤  weekly cron        review app (Railway)         Ayrshare
photos (repo or uploaded) ─────┴─► Claude drafts ──► reviewer edits/approves ──► location's FB / IG /
reviewer's past edits ─────────────► next week's        (email sign-in link)        LinkedIn / Google
                                     voice                                           Business / X
```

1. **Every Wednesday** the `social-drafts` cron runs `python -m social_content generate`. For each
   location with `active: true` it asks Claude for next week's posts (one request per location covering
   all its platforms) and saves them as drafts. Each reviewer gets an email with a sign-in link.
2. **The reviewer** opens the link on their phone or desktop and sees only their location. For each
   post they can edit the text, pick or upload a photo, change the time, ask Claude to rewrite
   ("shorter, mention the free valet"), then **Approve** or **Reject**. Anything Claude wasn't sure of
   is listed at the top as "Please check before approving".
3. **On approval** the post goes to Ayrshare, which publishes it at its scheduled time to that
   location's own accounts. Until Ayrshare is set up, approved posts wait for the reviewer to copy
   them out and click **Mark as posted**.
4. **Claude learns each reviewer's flair.** Every draft request includes the location's recent
   "Claude wrote → reviewer published" pairs, so the drafts move toward the reviewer's voice over time.

Claude only uses facts from `locations/_brand.yaml`, the location's `profile.yaml` and its
`salesforce.yaml`. It never makes up prices, offers or tenants, and it only describes photos it was
actually shown.

## Why location folders in GitHub (not a Claude project folder)

The drafting job runs unattended on Railway, and it can't read files in a claude.ai project.
A folder per location in this repo is something the job can read, has history for every change,
and can be edited in the browser (pencil icon, or **Add file → Upload files** for photos). Reviewers
never need GitHub: they upload photos in the review app.

## Railway setup (same project as the MCP server)

1. **Database:** in the project, choose **New → Database → PostgreSQL**.
2. **Review app:** choose **New → GitHub Repo**, pick this repo, and name the service `social-studio`.
   - **Custom Start Command:** `python -m social_content serve`
   - **Networking → Generate Domain** (this is the link reviewers get)
   - **Healthcheck Path:** `/healthz`
3. **Weekly drafts:** add the repo again as a service named `social-drafts`.
   - **Custom Start Command:** `python -m social_content generate`
   - **Cron Schedule:** `0 17 * * 3` (Wednesdays 17:00 UTC = 9 or 10 AM Pacific)
   - **Restart Policy:** Never; no domain
4. **Variables** on both services:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `ANTHROPIC_API_KEY` | From console.anthropic.com → API keys (only `social-drafts` and `social-studio` need it) |
| `SECRET_KEY` | A long random string (e.g. `openssl rand -hex 32`). Same value on both services |
| `PUBLIC_BASE_URL` | The `social-studio` domain, e.g. `https://social-studio-production.up.railway.app` |
| `ADMIN_EMAILS` | Comma-separated people who can see and approve every location (marketing) |
| `RESEND_API_KEY`, `REPORT_FROM` | Same as the daily brief service; used for reviewer emails. Use `${{daily-report.RESEND_API_KEY}}`. Reviewers are on other addresses, so `REPORT_FROM` must be on a domain verified in Resend (see `daily_report/README.md`), not `onboarding@resend.dev` |
| `AYRSHARE_API_KEY` | Optional, turns on auto-publishing (below) |
| `AYRSHARE_PROFILE_P44` (one per location code) | Optional, that location's Ayrshare profile key |

Optional: `CONTENT_MODEL` (default `claude-opus-5-5`), `CONTENT_EFFORT` (default `medium`),
`MAX_PHOTOS_PER_RUN` (default 8), `LOGIN_LINK_DAYS` (default 7).

## Publishing with Ayrshare

Each location has its own Facebook page, Instagram, LinkedIn page and Google Business Profile. Getting
your own app approved by Meta, LinkedIn and Google separately takes weeks, so this uses
[Ayrshare](https://www.ayrshare.com), which already has those approvals and supports one "profile"
per location (Business plan):

1. In Ayrshare, create one profile per location and have someone with access log in to that location's
   accounts on it (Ayrshare can send a link to the location manager to do this).
2. Copy the API key into `AYRSHARE_API_KEY` and each profile's key into `AYRSHARE_PROFILE_<CODE>`,
   e.g. `AYRSHARE_PROFILE_P44`.
3. Locations without a profile key keep working in copy-and-paste mode.

Instagram posts must have a photo. Photos are served from `PUBLIC_BASE_URL/media/...` so the networks
can fetch them. Those URLs are public but can't be guessed.

## Run it locally

```bash
pip install -r requirements.txt
python -m social_content seed-from-salesforce          # refresh salesforce.yaml for every location
python -m social_content generate --location p44-comerica-bank-building --dry-run   # print, don't save
python -m social_content generate --location p44-comerica-bank-building --no-email  # save to the local DB
SECRET_KEY=dev ADMIN_EMAILS=you@barrister-suites.com python -m social_content serve # http://localhost:8000
python -m unittest discover -s tests -t .
```

Without `RESEND_API_KEY`/SMTP set locally, get a sign-in link with
`python -c "from social_content.notify import login_link; print(login_link('you@barrister-suites.com'))"`.

## Cost

One Claude request per active location per week, with up to 8 photos: roughly $0.10–0.25 per
location per week on Claude Opus 5.5, so about $3–6 a week for all 23 locations. Each "Rewrite"
click is one smaller request.
