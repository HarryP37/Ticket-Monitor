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

# Confirmed via debug capture: destination search returns "No suggestion
# found" for a bare IATA code ("PER"), while origin search resolves a bare
# code fine ("CDG" -> "Paris (CDG)"). Type city names instead for the
# airports this tool's config actually uses, to sidestep that asymmetry.
AIRPORT_CITY_NAMES = {
    "CDG": "Paris",
    "FCO": "Rome",
    "MXP": "Milan",
    "BCN": "Barcelona",
    "MAD": "Madrid",
    "PER": "Perth",
    "BNE": "Brisbane",
}

# Reverse-engineered from a real URL a user got by manually completing a
# search: https://book.virginaustralia.com/dx/VADX/#/flight-selection?
# ADT=1&class=First&awardBooking=true&pos=au-en&channel=&activeMonth=
# 05-01-2027&journeyType=one-way&date=05-01-2027&origin=CDG&destination=
# BNE&tpid=NA&fareType=FIXED_REWARD&cabinType=NA&businessTravel=false&
# va-flow=flight-search&execution=<uuid>
#
# The trailing "execution" param is almost certainly a single-use,
# server-issued session token -- deliberately omitted here since we can't
# get a valid one without already having submitted a search, and this is
# a bet that the app either treats it as optional or issues a fresh one
# on the fly. "class=First" in that URL is unexplained (possibly a
# results-page filter applied AFTER landing, not part of the original
# search) and also omitted; "cabinType=NA" is kept as-is since it matches
# what we've confirmed the wizard flow itself submits (no cabin choice
# exists until the post-results fare modal).
DIRECT_RESULTS_BASE_URL = "https://book.virginaustralia.com/dx/VADX/#/flight-selection"


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

    def _click_id_or_text(self, element_id: str, texts: list[str]) -> None:
        """Click a wizard nav button. Prefer its confirmed id with a
        forced click (this app's buttons have repeatedly turned out to
        need force=True or dispatch_event rather than a plain click --
        confirmed for the one-way radio and the calendar day tile), and
        fall back to matching any of `texts` if the id isn't found (e.g.
        the site changes its markup)."""
        page = self._page
        target = page.locator(f"#{element_id}")
        if target.count() == 0:
            for text in texts:
                loc = page.get_by_text(text, exact=False)
                if loc.count() > 0:
                    target = loc.first
                    break
            else:
                return
        try:
            target.first.click(timeout=5000, force=True)
        except Exception:
            try:
                target.first.dispatch_event("click")
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

        # Typing a bare IATA code returns "No suggestion found" for
        # destination search (confirmed), so type the city name instead --
        # the resulting suggestion/browse entries are rendered as
        # "CityName (CODE)" text (confirmed from screenshots), so matching
        # on "(CODE)" directly is more robust than guessing the entry's
        # role/CSS class.
        type_text = AIRPORT_CITY_NAMES.get(value, value)
        field.fill("", timeout=5000)
        field.type(type_text, delay=60)
        page.wait_for_timeout(800)

        for locator in (
            page.get_by_text(f"({value})", exact=False).first,
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

    @staticmethod
    def _ordinal(n: int) -> str:
        # Matches the site's own (non-standard) suffix logic exactly, per a
        # debug capture's screen-reader labels: "11st", "12nd", "13rd" --
        # it doesn't apply the usual 11-13 exception that standard English
        # ordinals do, so neither do we.
        return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"

    def _select_calendar_date(self, date: dt.date, always_dump_debug: bool = False) -> None:
        """Confirmed via debug capture: the bare visible day digit sits in
        an aria-hidden <abbr> and is genuinely ambiguous (e.g. "1" also
        matches the guest-count stepper elsewhere on the page) -- an
        earlier version matched on it and silently clicked the wrong
        thing, leaving the date unset with no error. Each day tile also
        carries a unique, unambiguous screen-reader-only label right next
        to it though (confirmed exact format: "Saturday, 1st May 2027"),
        so match on that instead and click its nearest ancestor tile
        (the label span itself may not be "visible" per Playwright's
        actionability check, being screen-reader-only)."""
        page = self._page

        # A "Fare information" disclaimer dialog auto-opens over the
        # calendar (confirmed: #fare-disclaimer-ok-button, "Dismiss") and
        # appears to make the app ignore date-tile clicks entirely while
        # it's open -- three different click techniques all silently
        # failed identically until this was found, which fits a
        # component-level "ignore clicks while a dialog is open" check
        # rather than anything about how the click itself was performed.
        try:
            dismiss = page.locator("#fare-disclaimer-ok-button")
            if dismiss.count() > 0:
                dismiss.first.click(timeout=3000)
                page.wait_for_timeout(300)
        except Exception:
            pass

        # Confirmed via a screenshot from a run where this failed: the "Next"
        # arrow button search never matched anything (it's icon-only, no
        # accessible name matching "Next"/">"), so that loop silently gave
        # up after one failed attempt and left the calendar wherever it
        # happened to default to -- coincidentally the target month in
        # earlier runs, but NOT in that one (showed March/April 2027
        # instead of the requested May 2027), which is why the date could
        # never be set no matter how the day-tile click itself was done.
        #
        # The real navigation control is a month-pill strip (confirmed ids
        # "lowest-fare-month-select-month-N", role="option", aria-label
        # e.g. "May 2027[ selected]. Use arrow keys to navigate between
        # months, Enter key to choose month.") -- same custom-ARIA-listbox
        # pattern as the one-way/return switch. All months are already in
        # the DOM (not lazily loaded as you scroll), so no repeated
        # "next" clicking is needed at all: find the pill for our target
        # month directly and select it.
        target_month_label = date.strftime("%B %Y")  # e.g. "May 2027"
        month_pill = page.get_by_role("option", name=target_month_label, exact=False)
        if month_pill.count() > 0:
            try:
                month_pill.first.click(timeout=3000, force=True)
            except Exception:
                month_pill.first.dispatch_event("click")
            page.wait_for_timeout(500)

        try:
            dismiss = page.locator("#fare-disclaimer-ok-button")
            if dismiss.count() > 0:
                dismiss.first.click(timeout=2000)
                page.wait_for_timeout(300)
        except Exception:
            pass

        day_label = f"{date.strftime('%A')}, {self._ordinal(date.day)} {target_month_label}"
        sr_text = page.get_by_text(day_label, exact=False)
        if sr_text.count() == 0:
            raise RewardSearchError(f"Calendar tile for '{day_label}' not found")
        # Two prior approaches (force-clicking the ancestor "IsSelectable"
        # div, then clicking the sibling <abbr> digit normally) both left
        # the date unset with no error -- root cause still unclear, but
        # likely some combination of animation/transition timing that a
        # coordinate-based click's hit-testing doesn't handle well here.
        # dispatch_event bypasses coordinate/visibility/stability checks
        # entirely and fires a real DOM click event straight at the node;
        # React's delegated listener still catches it via normal bubbling
        # regardless of where exactly in the tile's subtree it originates,
        # which sidesteps all of the above at once.
        tile = sr_text.first.locator("xpath=../..")  # fsDateTileDefault
        try:
            tile.first.dispatch_event("click")
        except Exception:
            sr_text.first.dispatch_event("click")
        page.wait_for_timeout(500)

        # dispatch_event alone hasn't been confirmed to work yet (unclear
        # whether this app's listeners even catch synthetic/non-trusted
        # events) -- also try a real mouse click at the tile's actual
        # screen coordinates as a second attempt, in case one succeeds
        # where the other doesn't.
        try:
            box = tile.first.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.wait_for_timeout(500)
        except Exception:
            pass

        if always_dump_debug:
            self._dump_debug(f"calendar_after_click_{date.isoformat()}")

    def _fill_search_form(
        self, origin: str, destination: str, date: dt.date, cabin: str, adults: int,
        always_dump_debug: bool = False,
    ) -> None:
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

        self._click_id_or_text("fly-to-screen-next-button", ["Select dates", "Select Dates"])
        page.wait_for_timeout(800)

        # NOT default-selected (confirmed via debug capture: "Return" is
        # aria-checked="true" by default, and a plain text-match click on
        # "One way" silently failed to change it -- likely an animated
        # overlay sibling intercepting the click). This is a role="radio"
        # div with a stable id, so target that directly with a forced
        # click to bypass any interception.
        try:
            page.locator("#one-way").click(timeout=3000, force=True)
        except Exception:
            for text in ["One way", "One Way", "Oneway"]:
                try:
                    option = page.get_by_text(text, exact=False)
                    if option.count() > 0:
                        option.first.click(timeout=3000, force=True)
                        break
                except Exception:
                    continue
        page.wait_for_timeout(300)
        try:
            one_way = page.locator("#one-way")
            if one_way.count() > 0 and one_way.first.get_attribute("aria-checked") != "true":
                # The radiogroup's own label says "Use arrow keys to change
                # trip type" -- click may not be wired up at all on these,
                # only keyboard. Focus the group and navigate to it.
                page.locator("#calendar-controls-journey-type-switch [role='radiogroup']").focus()
                page.keyboard.press("Home")
        except Exception:
            pass

        try:
            self._select_calendar_date(date, always_dump_debug=always_dump_debug)
        except RewardSearchError:
            raise
        except Exception as e:
            raise RewardSearchError(f"Could not select calendar date {date}: {e}")
        page.wait_for_timeout(500)

        self._click_id_or_text("date-screen-next-button", ["Add guests", "Add Guests"])
        page.wait_for_timeout(500)

        # Adult count already defaults to config's default of 1; only the
        # single-adult case is handled for now (TODO: click the "+" adult
        # stepper `adults - 1` times to support more).

        self._click_id_or_text("guest-screen-lets-fly-button", ["Let's fly", "Lets fly", "Let's Fly"])

        # This navigates to a SEPARATE booking-engine domain (Sabre-
        # powered, storefront "VADX") which loads much more slowly than
        # the marketing site's own SPA and, confirmed from a debug
        # capture, loads an Incapsula bot-protection resource
        # (/_Incapsula_Resource). That capture showed the page still
        # sitting on its bare loading shell (#initial-progress-indicator,
        # "Loading...") even after a full 30s networkidle wait -- which
        # silently produced a misleadingly-labelled "ok" result with zero
        # data, since nothing here actually checked whether the page had
        # loaded. Poll actively for real results content to appear, with a
        # much longer budget, and raise a clearly diagnostic error if it
        # never resolves.
        #
        # An earlier version of this loop also accepted "Reward Seats"
        # text as a loaded-signal, which backfired: that text already
        # appears dozens of times on the PREVIOUS marketing-site page
        # (confirmed from an earlier capture, promotional content), so the
        # very first poll iteration -- likely still seeing the old page,
        # before cross-domain navigation had even started -- could match
        # it immediately and falsely report "loaded" (confirmed: a run's
        # timestamps showed the whole poll+parse taking only ~52s, far too
        # short to have actually waited out a stuck load, and its final
        # dump showed the bare loading shell regardless). "Choose your
        # flights" only exists on the real results page, so that's the
        # only signal used now.
        loaded = False
        for _ in range(45):  # ~90s at 2s intervals
            if page.get_by_text("Choose your flights", exact=False).count() > 0:
                loaded = True
                break
            page.wait_for_timeout(2000)

        if not loaded:
            raise RewardSearchError(
                "Results page never loaded past its loading spinner after ~90s. "
                "This navigates to a separate Sabre-powered booking domain "
                "(storefront 'VADX') that loads an Incapsula bot-protection "
                "resource -- this may be a slow cross-domain load, or the "
                "automated browser being detected/blocked by Incapsula. "
                "Check the debug screenshot for a captcha/challenge page."
            )

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

    def _try_direct_url(self, origin: str, destination: str, date: dt.date, adults: int) -> bool:
        """Navigate straight to a constructed results URL instead of
        driving the whole wizard -- see DIRECT_RESULTS_BASE_URL for what's
        known/unknown about this. Best-effort: any failure just means
        "didn't work", not a hard error, since the wizard flow is the
        proven fallback."""
        page = self._page
        date_str = date.strftime("%m-%d-%Y")
        params = {
            "ADT": str(adults),
            "awardBooking": "true",
            "pos": "au-en",
            "activeMonth": date_str,
            "journeyType": "one-way",
            "date": date_str,
            "origin": origin,
            "destination": destination,
            "tpid": "NA",
            "fareType": "FIXED_REWARD",
            "cabinType": "NA",
            "businessTravel": "false",
            "va-flow": "flight-search",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{DIRECT_RESULTS_BASE_URL}?{query}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            return False
        for _ in range(15):  # ~30s -- shorter than the full wizard's 90s
            # budget, since this is a cheap bet: if it's going to work it
            # should be fast, and if the domain is Incapsula-blocked it'll
            # be stuck either way, so no point waiting as long twice.
            try:
                if page.get_by_text("Choose your flights", exact=False).count() > 0:
                    return True
            except Exception:
                return False
            page.wait_for_timeout(2000)
        return False

    def search(
        self, origin: str, destination: str, date: dt.date, cabin: str, adults: int,
        always_dump_debug: bool = False,
    ) -> list[FlightResult]:
        try:
            if self._try_direct_url(origin, destination, date, adults):
                results = self._parse_results(origin, destination, date, cabin)
                if always_dump_debug:
                    self._dump_debug(f"ok_direct_{origin}_{destination}_{date.isoformat()}")
                return results

            # Direct URL didn't pan out -- fall back to the full,
            # confirmed-working wizard flow from the homepage.
            self._open_booking_widget()
            self._fill_search_form(origin, destination, date, cabin, adults, always_dump_debug=always_dump_debug)
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
