"""Pytest configuration and shared fixtures for Clinical Co-Pilot eval suite."""

# Bypass JWT and audit-DB before any test module imports ``config`` or ``main``.
# pydantic-settings reads env once at module import; if a sibling test imports
# main first with the real secret set, every subsequent test inherits it and
# /document/ingest tests fail with 401. Setting these here — at conftest top —
# guarantees the override lands before pytest collects any test file.
import os

os.environ["COPILOT_JWT_SECRET"] = ""
os.environ.setdefault("AUDIT_DB_URL", "")

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

REQUIRED_MARKERS = {"hard_failure", "clinical_accuracy"}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.fixture(autouse=True)
def _reset_main_globals():
    """Reset ``main`` module-level connection globals after every test.

    ``with TestClient(app) as client`` triggers the FastAPI shutdown hook,
    which calls ``await _redis.aclose()`` but leaves the module-level
    ``_redis`` attribute non-None. Subsequent tests then see a closed
    Redis client and exception paths fire on every call. Reset to ``None``
    after each test so the next one starts cold.
    """
    yield
    try:
        import main
    except Exception:
        return
    for attr in ("_redis", "_redis_saver", "_sqlite_saver", "_langfuse"):
        if hasattr(main, attr):
            setattr(main, attr, None)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Fail if any test is missing a required marker (hard_failure or clinical_accuracy).

    Skipped when a -m marker expression is active — the full-suite run is the
    enforcement point.  Marker-gated CI steps run after the full suite passes,
    so unmarked tests are caught before gates are evaluated.
    """
    if getattr(session.config.option, "markexpr", ""):
        return

    unmarked = [
        item.nodeid
        for item in session.items
        if not {m.name for m in item.iter_markers()} & REQUIRED_MARKERS
    ]
    if unmarked:
        lines = "\n".join(f"  {n}" for n in unmarked)
        raise pytest.UsageError(
            f"Tests missing a required marker ({', '.join(sorted(REQUIRED_MARKERS))}):\n{lines}\n"
            "Tag each test with @pytest.mark.hard_failure and/or @pytest.mark.clinical_accuracy."
        )
