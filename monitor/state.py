"""Track which flight results we've already alerted on, so repeated runs
don't re-notify for a reward seat that's still sitting there available."""
from __future__ import annotations

import json
from pathlib import Path


def load_seen(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    return set(json.loads(p.read_text()))


def save_seen(path: str | Path, seen: set[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(seen), indent=2))
