from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ALERT_GOOD = FIXTURES / "alert_rules" / "good"
ALERT_BAD = FIXTURES / "alert_rules" / "bad"
DASH_GOOD = FIXTURES / "dashboards" / "good"
DASH_BAD = FIXTURES / "dashboards" / "bad"
CHART = FIXTURES / "chart"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def check_ids(findings) -> set[str]:
    return {f.check for f in findings}


def describe(findings) -> list[str]:
    return [f.format_text() for f in findings]
