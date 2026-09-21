# Ticket-Monitor

Checks [virginaustralia.com](https://www.virginaustralia.com/au/en/) for
one-way Business class reward seats redeemable with Velocity Points, on a
schedule, and emails you the date and points cost when it finds new
availability.

Configured routes (edit in `config.yaml`): one-way, Paris/Rome/Milan/
Barcelona/Madrid to Perth/Brisbane, in May and September.

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

## ⚠️ Current status — read before relying on this

**The search wizard itself is fully working**, confirmed against the real
site across many debug-artifact round trips: the Velocity Points toggle,
origin/destination entry, one-way trip type, calendar date selection,
guest review, and submitting the search ("Let's fly") all reliably work
in `monitor/search.py`.

**Blocked at the last step.** Submitting a search hands off to a
*separate* booking-engine domain (Sabre-powered, storefront "VADX") that
loads a script from **Incapsula**, a bot-detection/WAF service. That page
has consistently failed to render past its own loading spinner —
confirmed identical (down to the byte) across multiple runs even after
waiting a full 90 seconds, which isn't how a real user's experience would
ever look. `_fill_search_form` raises a clear, explicitly-labelled error
when this happens (rather than silently reporting "no results"), and
`_parse_results` (the fare-modal parsing logic) has never actually been
exercised against real results yet as a result.

**I haven't tried to defeat this**, and don't intend to — no fingerprint
spoofing, proxy rotation, or stealth plugins to hide that it's a
headless/automated browser. That crosses from "personal reward
monitoring" into active evasion, which is out of scope for what I'll
automate here.

**The one thing actually worth trying:** run it from your own machine
instead of GitHub Actions (see "Running locally" below). GitHub Actions'
IP ranges are shared/datacenter and commonly pre-flagged by services like
Incapsula regardless of browser behavior; a residential IP might fare
differently. No guarantee, but it's the one meaningful variable left to
test. Set `HEADLESS=false` for that run so you can watch the actual
browser and see whether a CAPTCHA/challenge appears (something a
post-hoc screenshot might not fully capture).

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

Email setup (`SMTP_*`/`EMAIL_TO`) isn't required just to test whether the
site itself works — the code only tries to send an email if it actually
finds a result.

```bash
git clone https://github.com/HarryP37/Ticket-Monitor.git
cd Ticket-Monitor
pip install -r requirements.txt
playwright install chromium

# Quick single-route test, browser visible so you can watch it:
TEST_MODE=true HEADLESS=false python -m monitor.main
```

For a real run with email notifications, add the SMTP env vars:

```bash
export SMTP_HOST=... SMTP_PORT=587 SMTP_USER=... SMTP_PASS=... EMAIL_TO=...
python -m monitor.main
```

## Changing the schedule

Edit the `cron` line in `.github/workflows/reward-check.yml`. It's daily by
default; for weekly (every Monday), use `"0 20 * * 1"`.
