"""Tests for triage.census.census_cache_key.

Confirms the cache-key shape used to prevent the prefetch-vs-dispatcher
collision that caused Sara Chen's panel count to oscillate between reloads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from triage.census import census_cache_key

pytestmark = pytest.mark.hard_failure


def test_key_shape_includes_provider_and_hash():
    key = census_cache_key("prov-chen", ["pt-002", "pt-001"])
    parts = key.split(":")
    assert parts[0] == "copilot"
    assert parts[1] == "census"
    assert parts[2] == "prov-chen"
    # 8-char sha1 hex digest
    assert len(parts[3]) == 8
    assert all(c in "0123456789abcdef" for c in parts[3])


def test_key_is_order_independent():
    a = census_cache_key("prov-chen", ["pt-001", "pt-002", "pt-003"])
    b = census_cache_key("prov-chen", ["pt-003", "pt-001", "pt-002"])
    assert a == b


def test_provider_segments_are_distinct():
    a = census_cache_key("prov-chen", ["pt-001"])
    b = census_cache_key("prov-other", ["pt-001"])
    assert a != b


def test_empty_patient_list_uses_all_sentinel():
    key = census_cache_key("prov-chen", [])
    assert key == "copilot:census:prov-chen:all"


def test_missing_provider_does_not_collide_with_real_provider():
    a = census_cache_key(None, ["pt-001"])
    b = census_cache_key("prov-chen", ["pt-001"])
    assert a != b
    assert a.startswith("copilot:census:unknown:")


def test_different_patient_sets_produce_different_hashes():
    a = census_cache_key("prov-chen", ["pt-001", "pt-002"])
    b = census_cache_key("prov-chen", ["pt-001", "pt-003"])
    assert a != b
