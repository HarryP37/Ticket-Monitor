# Ticket-Monitor

Checks [virginaustralia.com](https://www.virginaustralia.com/au/en/) for
Business class reward seats redeemable with Velocity Points, on a schedule,
and emails you when it finds new availability.

Configured routes (edit in `config.yaml`): Paris, Rome, Milan, Barcelona and
Madrid to Perth and Brisbane, in May and September.

## How it works

1. `monitor/dates.py` builds the list of candidate travel dates from
   `config.yaml` (which months, how finely to sample, how far ahead).
2. `monitor/search.py` drives a headless Chromium browser (Playwright)
   against the VA booking widget for each route/date and pulls out any
   Business-cabin reward results.
3. `monitor/state.py` remembers (in `data/seen.json`, committed back to the
   repo by the workflow) which route/date/flight combos have already been
   emailed, so you don't get the same alert every single day a seat stays
   available.
4. `monitor/notify.py` emails you (via SMTP) whenever a search turns up
   something new.
5. `.github/workflows/reward-check.yml` runs the whole thing daily via
   GitHub Actions — no server of your own required.

## ⚠️ Important limitations — read before relying on this

**The site-scraping selectors are unverified.** This was built in an
environment whose outbound network access to virginaustralia.com was
blocked by organizational policy, so I was not able to load the real page
and confirm field names, labels, or result markup. `monitor/search.py` is
written defensively (resilient label/role-based Playwright queries, not
brittle CSS classes) and dumps a screenshot + HTML snapshot to `debug/`
(uploaded as a workflow artifact) on any failure or unexpected page state.

**Before trusting the schedule**, trigger the workflow manually once
(Actions tab → "Check Virgin Australia reward availability" → Run
workflow), then check the run's debug artifact if it didn't find results
you expected. Send me (or paste into an editor) the screenshot/HTML and
I can correct the selectors in `_open_booking_widget`, `_fill_search_form`,
and `_parse_results` — those three functions are the only site-specific
part of the codebase.

**Bot detection is a real risk.** Major airline booking engines commonly
run anti-bot systems (Akamai, PerimeterX, etc.) that can block automated
browsers outright, including from GitHub Actions' shared IP ranges. If
runs start failing with a CAPTCHA/"unusual traffic" page in the debug
artifact, that's what's happening. I deliberately haven't built in any
evasion (proxy rotation, fingerprint spoofing) — that crosses from
"personal reward monitoring" into something I won't automate. If you hit
this wall, the practical fallback is running the script from your own
machine's cron instead of GitHub Actions (`python -m monitor.main` works
identically locally — see below).

**These routes likely mean partner reward seats, not VA metal.** Virgin
Australia doesn't fly Europe–Australia directly, so any results here would
be Velocity partner awards (e.g. Etihad, Singapore Airlines) that VA's own
site is able to search and sell. If VA's website doesn't expose search for
these origins at all, the tool will consistently find nothing — that's a
site limitation, not a bug, and worth confirming manually on the site once.

**Scraping ToS.** Checking your own frequent-flyer program for reward
availability at a modest, rate-limited frequency (daily/weekly) is a
common personal use case, but it may still be against Virgin Australia's
website terms of use. Use at your own judgement/risk.

## Setup

1. **Repo secrets** (Settings → Secrets and variables → Actions):
   - `SMTP_HOST`, `SMTP_PORT` (e.g. `587`), `SMTP_USER`, `SMTP_PASS`
   - `EMAIL_FROM` (optional, defaults to `SMTP_USER`)
   - `EMAIL_TO` — where alerts get sent

   For Gmail: enable 2FA on the account, then create an
   [App Password](https://myaccount.google.com/apppasswords) and use that
   as `SMTP_PASS` (host `smtp.gmail.com`, port `587`).

2. **Edit `config.yaml`** to adjust routes, months, cabin, or how many
   dates per month get sampled (`day_step`).

3. **Trigger a manual run** (Actions tab → Run workflow) to confirm it
   works before relying on the daily schedule.

## Running locally

```bash
pip install -r requirements.txt
playwright install chromium
export SMTP_HOST=... SMTP_PORT=587 SMTP_USER=... SMTP_PASS=... EMAIL_TO=...
python -m monitor.main
```

## Changing the schedule

Edit the `cron` line in `.github/workflows/reward-check.yml`. It's daily by
default; for weekly (every Monday), use `"0 20 * * 1"`.
