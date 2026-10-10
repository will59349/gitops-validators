"""Prometheus-style duration validation (``30s``, ``5m``, ``1h30m``, ``2d``)."""

from __future__ import annotations

import re
from typing import Any

# Same grammar as Prometheus model.ParseDuration: units in descending order,
# each at most once, at least one unit, or the bare string "0".
_DURATION_RE = re.compile(
    r"^(?:(?:[0-9]+)y)?(?:(?:[0-9]+)w)?(?:(?:[0-9]+)d)?(?:(?:[0-9]+)h)?"
    r"(?:(?:[0-9]+)m)?(?:(?:[0-9]+)s)?(?:(?:[0-9]+)ms)?$"
)


def is_valid_duration(value: Any) -> bool:
    """Return True for a Prometheus-style duration string.

    Integers are accepted only when they are 0 (``for: 0``); anything else
    without a unit is ambiguous and rejected.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    if not isinstance(value, str):
        return False
    value = value.strip()
    if value == "0":
        return True
    if not value:
        return False
    return _DURATION_RE.match(value) is not None
