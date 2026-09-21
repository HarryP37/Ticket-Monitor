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

        # Dismiss the cookie-consent banner (a "CookieYes"-style widget;
        # confirmed button text is "Accept and close", nested in <span><p>).
        for text in ["Accept and close", "Accept all", "Accept", "I Agree"]:
            try:
                btn = page.get_by_role("button", name=text, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=3000)
                    break
            except Exception:
                continue

        # Switch the booking widget from "cash" fares to Velocity Points
        # redemption mode. Confirmed: a role="switch" element with
        # aria-label/accessible-name exactly "Use Velocity Points" (an
        # earlier version of this code looked for "Use Points", which is
        # NOT a substring of "Use Velocity Points" and never matched).
        try:
            toggle = page.get_by_role("switch", name="Use Velocity Points")
            if toggle.count() == 0:
                toggle = page.locator('[aria-label="Use Velocity Points"]')
            toggle.first.click(timeout=5000)
        except Exception:
            raise RewardSearchError(
                "Could not find/click the 'Use Velocity Points' toggle on the booking "
                "widget. The site's wording or layout has likely changed -- inspect the "
                "debug screenshot/HTML and update _open_booking_widget()."
            )
        page.wait_for_timeout(500)

        # NOTE: no "One way"/"Return" control is visible on the homepage
        # widget at this stage (confirmed from a live capture) -- only the
        # points toggle plus the From/To fields below. Trip type, if
        # selectable at all, is presumably chosen on whatever screen comes
        # next after From/To are filled; see the TODO in _fill_search_form.

    def _fill_location(self, input_id: str, value: str) -> None:
        """Fill an origin/destination field and, if the site pops up an
        autocomplete suggestion list, click the matching suggestion --
        confirmed these are plain <input> elements (ids
        book-a-trip-panel-origin-input / -destination-input), so a bare
        .fill() may not register as a "real" selection the way typing +
        picking a suggestion does."""
        page = self._page
        field = page.locator(f"#{input_id}")
        field.click(timeout=5000)
        field.fill("", timeout=5000)
        field.type(value, delay=60)
        page.wait_for_timeout(800)

        for locator in (
            page.get_by_role("option").first,
            page.locator("[role='option'], [class*='suggestion'], [class*='autocomplete'] li").first,
        ):
            try:
                if locator.count() > 0:
                    locator.click(timeout=3000)
                    return
            except Exception:
                continue
        page.keyboard.press("Enter")

    def _fill_search_form(self, origin: str, destination: str, date: dt.date, cabin: str, adults: int) -> None:
        page = self._page

        # These two are the only steps confirmed to exist on the homepage
        # widget, so a failure here is a real, hard failure.
        try:
            self._fill_location("book-a-trip-panel-origin-input", origin)
            self._fill_location("book-a-trip-panel-destination-input", destination)
        except PlaywrightTimeoutError as e:
            raise RewardSearchError(
                f"Could not fill origin/destination for {origin}->{destination}: {e}"
            )
        page.wait_for_timeout(1500)

        # TODO: everything below (trip type, date, cabin class, submit) is
        # UNCONFIRMED -- the homepage widget doesn't expose these fields, so
        # they likely live on a subsequent screen reached after From/To are
        # filled (possibly a full navigation to a search-results/booking
        # page). Deliberately best-effort/non-fatal for now: log and move on
        # rather than raising, so whatever screen we land on gets captured
        # by the debug dump for the next round of fixes instead of us
        # aborting before seeing it.
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        for text in ["One way", "One Way", "Oneway"]:
            try:
                option = page.get_by_text(text, exact=False)
                if option.count() > 0:
                    option.first.click(timeout=3000)
                    break
            except Exception:
                continue

        date_str = date.strftime("%d %b %Y")  # e.g. "05 May 2027"
        for label in ["Departure date", "Departing on", "Date"]:
            try:
                field = page.get_by_label(label, exact=False)
                if field.count() > 0:
                    field.first.fill(date_str, timeout=3000)
                    break
            except Exception:
                continue

        if cabin.lower() == "business":
            cabin_text = "Business"
        elif cabin.lower() == "premium_economy":
            cabin_text = "Premium Economy"
        else:
            cabin_text = None
        if cabin_text:
            try:
                option = page.get_by_text(cabin_text, exact=False)
                if option.count() > 0:
                    option.first.click(timeout=3000)
            except Exception:
                pass

        for name in ["Search", "Search flights", "Find flights"]:
            try:
                btn = page.get_by_role("button", name=name, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=5000)
                    break
            except Exception:
                continue

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

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

    def search(
        self, origin: str, destination: str, date: dt.date, cabin: str, adults: int,
        always_dump_debug: bool = False,
    ) -> list[FlightResult]:
        try:
            self._open_booking_widget()
            self._fill_search_form(origin, destination, date, cabin, adults)
            results = self._parse_results(origin, destination, date, cabin)
            if always_dump_debug:
                self._dump_debug(f"ok_{origin}_{destination}_{date.isoformat()}")
            return results
        except RewardSearchError:
            self._dump_debug(f"error_{origin}_{destination}_{date.isoformat()}")
            raise
        except Exception as e:
            self._dump_debug(f"unexpected_{origin}_{destination}_{date.isoformat()}")
            raise RewardSearchError(f"Unexpected failure searching {origin}->{destination} on {date}: {e}")


def polite_sleep(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))
