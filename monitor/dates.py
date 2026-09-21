"""Generate the list of candidate travel dates to search."""
from __future__ import annotations

import datetime as dt


def generate_dates(months: list[int], day_step: int, years_ahead: int) -> list[dt.date]:
    """Every `day_step`-th day, in the given calendar months, between today
    and `years_ahead` years from today."""
    today = dt.date.today()
    end = today.replace(year=today.year + years_ahead)

    dates = []
    d = today
    while d <= end:
        if d.month in months and d.day == 1:
            month_start = d
            for offset in range(0, 31, day_step):
                candidate = month_start + dt.timedelta(days=offset)
                if candidate.month != month_start.month:
                    break
                if candidate >= today:
                    dates.append(candidate)
        d += dt.timedelta(days=1)

    return sorted(set(dates))
