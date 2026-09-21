"""Entry point: search all configured routes/dates, email any newly-seen
Business class Velocity Points reward availability."""
from __future__ import annotations

import logging
import os
import sys

from .config import load_config
from .dates import generate_dates
from .notify import send_email
from .search import RewardSearchError, VirginAustraliaRewardSearch, polite_sleep
from .state import load_seen, save_seen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("monitor")


def run(config_path: str = "config.yaml") -> int:
    config = load_config(config_path)
    dates = generate_dates(
        config.search.months, config.search.day_step, config.search.years_ahead
    )

    test_mode = os.environ.get("TEST_MODE") == "true"
    if test_mode:
        config.routes = config.routes[:1]
        dates = dates[:1]
        log.info("TEST_MODE: limited to a single route/date for fast selector debugging.")

    log.info("Checking %d route(s) x %d date(s) = %d searches", len(config.routes), len(dates), len(config.routes) * len(dates))

    seen = load_seen(config.state_file)
    found_new = []
    found_all = []
    errors = []

    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    with VirginAustraliaRewardSearch(config.booking_url, config.debug_dir, headless=headless) as searcher:
        for route in config.routes:
            for date in dates:
                try:
                    results = searcher.search(
                        route.origin, route.destination, date,
                        config.search.cabin, config.search.adults,
                        always_dump_debug=test_mode,
                    )
                except RewardSearchError as e:
                    log.warning("Search failed for %s->%s on %s: %s", route.origin, route.destination, date, e)
                    errors.append(str(e))
                    polite_sleep(config.search.min_delay_seconds, config.search.max_delay_seconds)
                    continue

                for r in results:
                    found_all.append(r)
                    if r.key() not in seen:
                        found_new.append(r)
                        seen.add(r.key())

                if results:
                    log.info("%s->%s on %s: %d result(s)", route.origin, route.destination, date, len(results))

                polite_sleep(config.search.min_delay_seconds, config.search.max_delay_seconds)

    to_notify = found_new if config.notification.only_notify_new else found_all
    if to_notify:
        log.info("Sending email for %d result(s)", len(to_notify))
        send_email(
            subject=f"Virgin Australia: {len(to_notify)} Business reward seat(s) found",
            results=to_notify,
        )
    else:
        log.info("No new results to notify about.")

    save_seen(config.state_file, seen)

    if errors and not found_all:
        log.error("All %d searches failed; nothing found. See debug/ for details.", len(errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
