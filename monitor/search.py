"""Playwright adapter for Virgin Australia's "Book with Velocity Points"
reward flight search.

IMPORTANT — read this before relying on the tool:

This adapter was built without live access to virginaustralia.com (the
environment that generated it has its outbound network access blocked for
that domain), so it was reverse-engineered from a debug HTML capture plus
a manual walkthrough of the real site (screenshots) rather than direct
testing. Confirmed as accurate: the "Use Velocity Points" toggle, the
origin/destination field IDs, the 3-step modal wizard (Route -> Select
dates -> Add guests -> Let's fly), and the results page's fixed-order
fare modal (Economy Reward | Business Reward | First Reward). The
weakest link, still unverified end-to-end, is `_select_calendar_date`
(scoping a day-cell click when two months are shown side by side) and
exactly what's clickable to open each flight's fare modal in
`_parse_results` -- both were built from screenshots, not real DOM.

If a run fails or looks wrong, the "debug" artifact (screenshot + HTML
dump, written on any failure or on an unrecognised page state) shows
exactly where. Everything in this module is intentionally isolated from
the rest of the pipeline (config, dates, notify, state) so it can be
fixed in place without touching anything else.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
import re
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

    def _fill_location(self, candidate_ids: list[str], value: str) -> None:
        """Fill an origin/destination field. Confirmed there are actually
        TWO separate inputs for each of origin/destination: the homepage
        widget's (book-a-trip-panel-origin-input / -destination-input) and
        a second one that appears once the full-screen modal opens
        (from-to-screen-origin-input / -destination-input) -- confirmed via
        debug capture that filling origin via the homepage id somehow syncs
        both, but destination does not, leaving the modal's real field
        empty. `candidate_ids` should be ordered by preference; whichever
        is actually visible is used.

        Once focused, a picker panel opens with a search box AND a static
        region-browse list with deterministic ids per airport (confirmed
        e.g. id="destination-PER" for Perth) -- try the direct id first,
        since the search box's suggestions are unreliable for bare IATA
        codes (confirmed: typing "PER" returned "No suggestion found" even
        though "Perth (PER)" was sitting right there in the browse list).
        Only European origins in this tool's config won't have a
        browse-list id (no Europe region tab), so this falls back to
        typing + picking a suggestion."""
        page = self._page
        field = None
        for candidate_id in candidate_ids:
            loc = page.locator(f"#{candidate_id}")
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    field = loc.first
                    break
            except Exception:
                continue
        if field is None:
            field = page.locator(f"#{candidate_ids[0]}")

        field.click(timeout=5000)
        page.wait_for_timeout(500)

        field_kind = "origin" if "origin" in candidate_ids[0] else "destination"
        direct = page.locator(f"#{field_kind}-{value}")
        try:
            if direct.count() > 0:
                direct.first.click(timeout=3000)
                return
        except Exception:
            pass

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

    def _select_calendar_date(self, date: dt.date) -> None:
        """The date-picker shows two months side by side (e.g. May 2027 /
        June 2027), so day numbers repeat -- scope the click to whichever
        column also has the target month's heading. Best-effort: if
        navigation or scoping fails, this raises and the caller treats it
        as a hard failure (there's no way to proceed without a date)."""
        page = self._page
        target_label = date.strftime("%B %Y")  # e.g. "May 2027"

        for _ in range(24):
            if page.get_by_text(target_label, exact=False).count() > 0:
                break
            advanced = False
            for name in ["Next", "Next month", ">"]:
                try:
                    btn = page.get_by_role("button", name=name, exact=False)
                    if btn.count() > 0:
                        btn.first.click(timeout=2000)
                        page.wait_for_timeout(300)
                        advanced = True
                        break
                except Exception:
                    continue
            if not advanced:
                break

        day_str = str(date.day)
        month_heading = page.get_by_text(target_label, exact=False).first
        if month_heading.count() == 0:
            raise RewardSearchError(f"Calendar never showed target month {target_label}")
        # Assume day cells are inside the same immediate container as the
        # month heading (a common "month block" layout) so we don't click
        # the same day number in the adjacent month.
        column = month_heading.locator("xpath=ancestor::*[self::div or self::section][1]")
        day_cell = column.get_by_text(day_str, exact=True)
        if day_cell.count() == 0:
            day_cell = page.get_by_text(day_str, exact=True)
        day_cell.first.click(timeout=5000)

    def _fill_search_form(self, origin: str, destination: str, date: dt.date, cabin: str, adults: int) -> None:
        """Confirmed 3-step modal wizard (from a manual walkthrough of the
        real site): Route (From/To) -> "Select dates" -> calendar (One way
        already default-selected) -> "Add guests" -> guests review (Adult
        count already defaults to 1, matching our config default) ->
        "Let's fly", which lands on the results page. No cabin-class
        control exists anywhere in this flow -- Business vs Economy is
        chosen per-flight on the results page instead (see _parse_results)."""
        page = self._page

        try:
            self._fill_location(
                ["book-a-trip-panel-origin-input", "from-to-screen-origin-input"], origin
            )
            self._fill_location(
                ["from-to-screen-destination-input", "book-a-trip-panel-destination-input"], destination
            )
        except PlaywrightTimeoutError as e:
            raise RewardSearchError(
                f"Could not fill origin/destination for {origin}->{destination}: {e}"
            )
        page.wait_for_timeout(1000)

        for name in ["Select dates", "Select Dates"]:
            try:
                btn = page.get_by_text(name, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=5000)
                    break
            except Exception:
                continue
        page.wait_for_timeout(800)

        # Confirmed default-selected already, but click explicitly in case
        # a future visit or different route defaults to "Return" instead.
        for text in ["One way", "One Way", "Oneway"]:
            try:
                option = page.get_by_text(text, exact=False)
                if option.count() > 0:
                    option.first.click(timeout=3000)
                    break
            except Exception:
                continue

        try:
            self._select_calendar_date(date)
        except RewardSearchError:
            raise
        except Exception as e:
            raise RewardSearchError(f"Could not select calendar date {date}: {e}")
        page.wait_for_timeout(500)

        for name in ["Add guests", "Add Guests"]:
            try:
                btn = page.get_by_text(name, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=5000)
                    break
            except Exception:
                continue
        page.wait_for_timeout(500)

        # Adult count already defaults to config's default of 1; only the
        # single-adult case is handled for now (TODO: click the "+" adult
        # stepper `adults - 1` times to support more).

        for name in ["Let's fly", "Lets fly", "Let's Fly"]:
            try:
                btn = page.get_by_text(name, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=5000)
                    break
            except Exception:
                continue

        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

    def _parse_results(self, origin: str, destination: str, date: dt.date, cabin: str) -> list[FlightResult]:
        """Confirmed results-page layout (from a manual walkthrough): a
        list of flight cards, each tagged "Reward Seats", followed by a
        price box. Clicking a card opens a "Choose a fare" modal with three
        FIXED columns in order -- Economy Reward | Business Reward | First
        Reward -- each either "Unavailable" or a points price with a
        "Select X Reward" button. That fixed order lets us just text-slice
        the page between "Business Reward" and "First Reward" rather than
        depend on guessed DOM structure, which is far more robust here."""
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

        if cabin.lower() != "business":
            # Only Business is implemented for now (that's all this tool is
            # configured to search); other cabins would need their own
            # column-slice ("Economy Reward" .. "Business Reward", etc).
            return results

        badges = page.get_by_text("Reward Seats", exact=False)
        count = min(badges.count(), 20)  # sanity cap
        for i in range(count):
            badge = badges.nth(i)
            try:
                # Click forward to the nearest points price in this card;
                # the click bubbles up to whatever element actually owns
                # the click handler, so we don't need to know the exact
                # clickable container.
                price_area = badge.locator("xpath=following::*[contains(text(),'Pts')][1]")
                price_area.click(timeout=5000)
                page.wait_for_timeout(800)
            except Exception:
                continue

            if page.get_by_text("Choose a fare", exact=False).count() == 0:
                # Didn't open a fare modal -- skip this card rather than
                # misreading a stale page state.
                continue

            modal_text = page.inner_text("body")
            try:
                start = modal_text.index("Business Reward")
                end = modal_text.index("First Reward", start)
                business_section = modal_text[start:end]
            except ValueError:
                business_section = ""

            available = (
                "unavailable" not in business_section.lower()
                and "select business reward" in business_section.lower()
            )
            if available:
                points = None
                for token in business_section.replace(",", "").split():
                    if token.isdigit() and len(token) >= 4:
                        points = int(token)
                        break
                flight_match = re.search(r"\b[A-Z]{2}\s?\d{2,4}\b", modal_text)
                results.append(
                    FlightResult(
                        origin=origin,
                        destination=destination,
                        date=date.isoformat(),
                        cabin=cabin,
                        flight_number=flight_match.group(0) if flight_match else f"option-{i}",
                        points=points,
                        taxes_fees=None,
                        available=True,
                    )
                )

            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

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
