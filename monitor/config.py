"""Load and validate config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Route:
    origin: str
    destination: str


@dataclass
class SearchSettings:
    months: list[int]
    day_step: int
    years_ahead: int
    cabin: str
    adults: int
    min_delay_seconds: float
    max_delay_seconds: float


@dataclass
class NotificationSettings:
    only_notify_new: bool = True


@dataclass
class Config:
    booking_url: str
    routes: list[Route]
    search: SearchSettings
    notification: NotificationSettings
    state_file: str
    debug_dir: str


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())

    routes = [Route(**r) for r in raw["routes"]]
    search = SearchSettings(**raw["search"])
    notification = NotificationSettings(**raw.get("notification", {}))

    return Config(
        booking_url=raw["booking_url"],
        routes=routes,
        search=search,
        notification=notification,
        state_file=raw.get("state_file", "data/seen.json"),
        debug_dir=raw.get("debug_dir", "debug"),
    )
