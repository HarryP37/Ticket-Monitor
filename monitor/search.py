"""Playwright adapter for Virgin Australia's "Book with Velocity Points"
reward flight search.

IMPORTANT — read this before relying on the tool:

This adapter was written without live access to virginaustralia.com (the
environment that generated it has its outbound network access blocked for
that domain). The locators below are a best-effort implementation using
resilient, role/label-based Playwright queries rather than brittle CSS
class names, but they are UNVERIFIED against the real site.

The first run(s) should be triggered manually (workflow_dispatch) and the
"debug" artifact (screenshot + HTML dump, written on any failure or on an
unrecognised page state) should be inspected to correct the locators in
`_open_booking_widget`, `_fill_search_form`, and `_parse_results` below.

Everything in this module is intentionally isolated from the rest of the
pipeline (config, dates, notify, state) so it can be fixed in place without
touching anything else.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

CHROMIUM_PATH_CANDIDATES = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]


@dataclasses.dataclass
class FlightResult:
    origin: str
    destination: str
    date: str
    cabin: str
    flight_number: str
    points: int | None
    taxes_fees: str | None
    available: bool

    def key(self) -> str:
        return f"{self.origin}-{self.destination}-{self.date}-{self.cabin}-{self.flight_number}"


class RewardSearchError(RuntimeError):
    """Raised when the search couldn't be completed (site changed, blocked, etc.)."""


def _find_chromium() -> str | None:
    for path in CHROMIUM_PATH_CANDIDATES:
        if Path(path).exists():
            return path
    return None  # let Playwright fall back to its own managed install


class VirginAustraliaRewardSearch:
    def __init__(self, booking_url: str, debug_dir: str, headless: bool = True):
        self.booking_url = booking_url
        self.debug_dir = Path(debug_dir)
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._page: Page | None = None

    def __enter__(self) -> "VirginAustraliaRewardSearch":
        self._playwright = sync_playwright().start()
        launch_kwargs = dict(
            headless=self.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        chromium_path = _find_chromium()
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        self._page = self._browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-AU",
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _dump_debug(self, label: str) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        base = self.debug_dir / f"{stamp}_{label}"
        try:
            self._page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        try:
            base.with_suffix(".html").write_text(self._page.content())
        except Exception:
            pass

    def _open_booking_widget(self) -> None:
        page = self._page
        page.goto(self.booking_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2000)

        # Dismiss a cookie-consent banner if present. Site copy/labels vary,
        # so try a few common phrasings before giving up on this step.
        for text in ["Accept all", "Accept All Cookies", "Accept", "I Agree"]:
            try:
                btn = page.get_by_role("button", name=text, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=3000)
                    break
            except Exception:
                continue

        # Switch the booking widget from "cash" fares to Velocity Points
        # redemption mode. Adjust this label if the real site phrases it
        # differently (e.g. "Redeem Points", "Use Velocity Points").
        try:
            toggle = page.get_by_text("Use Points", exact=False)
            if toggle.count() > 0:
                toggle.first.click(timeout=5000)
        except Exception:
            raise RewardSearchError(
                "Could not find/click the 'Use Points' toggle on the booking widget. "
                "The site's wording or layout has likely changed -- inspect the debug "
                "screenshot/HTML and update _open_booking_widget()."
            )

    def _fill_search_form(self, origin: str, destination: str, date: dt.date, cabin: str, adults: int) -> None:
        page = self._page
        try:
            page.get_by_label("From", exact=False).fill(origin, timeout=5000)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")

            page.get_by_label("To", exact=False).fill(destination, timeout=5000)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")

            date_str = date.strftime("%d %b %Y")  # e.g. "05 May 2027"
            page.get_by_label("Departure date", exact=False).fill(date_str, timeout=5000)

            if cabin.lower() == "business":
                page.get_by_text("Business", exact=False).first.click(timeout=5000)
            elif cabin.lower() == "premium_economy":
                page.get_by_text("Premium Economy", exact=False).first.click(timeout=5000)

            page.get_by_role("button", name="Search", exact=False).click(timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError as e:
            raise RewardSearchError(
                f"Timed out filling the search form for {origin}->{destination} on {date}: {e}"
            )

    def _parse_results(self, origin: str, destination: str, date: dt.date, cabin: str) -> list[FlightResult]:
        page = self._page
        results: list[FlightResult] = []

        no_availability_phrases = [
            "No flights available",
            "No reward seats",
            "Sorry, no flights",
            "not available for this route",
        ]
        page_text = page.inner_text("body")
        if any(phrase.lower() in page_text.lower() for phrase in no_availability_phrases):
            return results

        # Best-effort: look for fare cards that mention the requested cabin
        # and a "pts" / "points" amount. This is the part most likely to
        # need rework once the real markup is known.
        cards = page.locator("[class*='fare'], [class*='flight-result'], [class*='fareCard']")
        count = cards.count()
        for i in range(count):
            card = cards.nth(i)
            try:
                text = card.inner_text()
            except Exception:
                continue
            if cabin.replace("_", " ").lower() not in text.lower():
                continue
            points = None
            for token in text.replace(",", "").split():
                if token.isdigit() and len(token) >= 4:
                    points = int(token)
                    break
            flight_number = ""
            for line in text.splitlines():
                line = line.strip()
                if line[:2].isalpha() and line[2:].strip().isdigit():
                    flight_number = line
                    break
            results.append(
                FlightResult(
                    origin=origin,
                    destination=destination,
                    date=date.isoformat(),
                    cabin=cabin,
                    flight_number=flight_number or f"unknown-{i}",
                    points=points,
                    taxes_fees=None,
                    available=True,
                )
            )
        return results

    def search(self, origin: str, destination: str, date: dt.date, cabin: str, adults: int) -> list[FlightResult]:
        try:
            self._open_booking_widget()
            self._fill_search_form(origin, destination, date, cabin, adults)
            return self._parse_results(origin, destination, date, cabin)
        except RewardSearchError:
            self._dump_debug(f"error_{origin}_{destination}_{date.isoformat()}")
            raise
        except Exception as e:
            self._dump_debug(f"unexpected_{origin}_{destination}_{date.isoformat()}")
            raise RewardSearchError(f"Unexpected failure searching {origin}->{destination} on {date}: {e}")


def polite_sleep(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))
