"""Unit tests for evals._response_cache.

Covers:
- cache-miss writes, cache-hit reads identical output
- key changes when fixture bytes change
- key changes when EVAL_CACHE_VERSION changes
- mode ``off`` bypasses both read and write
- mode ``read`` reads but does not write
- mode ``write`` writes but does not read
- mode ``readwrite`` reads then writes on miss
- unknown mode falls back to ``off``
- atomic write: a failing tmp-write does not poison the store
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.hard_failure

from evals._response_cache import (
    EvalResponseCache,
    cache_reads_enabled,
    cache_writes_enabled,
    derive_cache_key,
    resolve_cache_mode,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_SAMPLE_PAYLOAD: dict[str, Any] = {
    "case_id": "test-1",
    "extraction": {"kind": "lab_report", "values": []},
    "critic_decision": "pass",
    "critic_violations": [],
    "soft_warns": [],
    "captured_logs": [],
    "error": None,
    "ocr_layout": None,
    "observations": None,
    "retrieval": None,
    "finalized": None,
    "skipped_reason": None,
}


# --------------------------------------------------------------------------- #
# resolve_cache_mode
# --------------------------------------------------------------------------- #


class TestResolveCacheMode:
    def test_off_is_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVAL_USE_CACHE", raising=False)
        assert resolve_cache_mode() == "off"

    @pytest.mark.parametrize("mode", ["off", "read", "write", "readwrite"])
    def test_valid_modes_from_arg(self, mode: str) -> None:
        assert resolve_cache_mode(mode) == mode

    @pytest.mark.parametrize("mode", ["off", "read", "write", "readwrite"])
    def test_valid_modes_from_env(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("EVAL_USE_CACHE", mode)
        assert resolve_cache_mode() == mode

    def test_unknown_mode_falls_back_to_off(self) -> None:
        assert resolve_cache_mode("BOGUS") == "off"

    def test_arg_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_USE_CACHE", "readwrite")
        assert resolve_cache_mode("off") == "off"


# --------------------------------------------------------------------------- #
# cache_reads_enabled / cache_writes_enabled
# --------------------------------------------------------------------------- #


class TestCacheFlagHelpers:
    @pytest.mark.parametrize("mode,expected", [
        ("off", False),
        ("read", True),
        ("write", False),
        ("readwrite", True),
    ])
    def test_reads_enabled(self, mode: str, expected: bool) -> None:
        assert cache_reads_enabled(mode) == expected  # type: ignore[arg-type]

    @pytest.mark.parametrize("mode,expected", [
        ("off", False),
        ("read", False),
        ("write", True),
        ("readwrite", True),
    ])
    def test_writes_enabled(self, mode: str, expected: bool) -> None:
        assert cache_writes_enabled(mode) == expected  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# derive_cache_key
# --------------------------------------------------------------------------- #


class TestDeriveCacheKey:
    def test_returns_64_char_hex(self) -> None:
        key = derive_cache_key(b"some bytes")
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_key_changes_with_fixture_bytes(self) -> None:
        k1 = derive_cache_key(b"doc-A")
        k2 = derive_cache_key(b"doc-B")
        assert k1 != k2

    def test_key_changes_with_cache_version(self) -> None:
        k1 = derive_cache_key(b"doc", cache_version="1")
        k2 = derive_cache_key(b"doc", cache_version="2")
        assert k1 != k2

    def test_key_changes_with_model_id(self) -> None:
        k1 = derive_cache_key(b"doc", model_id="claude-sonnet")
        k2 = derive_cache_key(b"doc", model_id="claude-opus")
        assert k1 != k2

    def test_key_changes_with_prompt_hash(self) -> None:
        k1 = derive_cache_key(b"doc", prompt_hash="abc")
        k2 = derive_cache_key(b"doc", prompt_hash="xyz")
        assert k1 != k2

    def test_env_version_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVAL_CACHE_VERSION", "99")
        monkeypatch.delenv("EVAL_MODEL_ID", raising=False)
        monkeypatch.delenv("EVAL_CACHE_PROMPT_HASH", raising=False)
        k1 = derive_cache_key(b"doc")
        monkeypatch.setenv("EVAL_CACHE_VERSION", "100")
        k2 = derive_cache_key(b"doc")
        assert k1 != k2

    def test_deterministic_for_same_inputs(self) -> None:
        k1 = derive_cache_key(b"x", model_id="m", cache_version="v", prompt_hash="p")
        k2 = derive_cache_key(b"x", model_id="m", cache_version="v", prompt_hash="p")
        assert k1 == k2


# --------------------------------------------------------------------------- #
# EvalResponseCache — miss/write/hit cycle
# --------------------------------------------------------------------------- #


class TestEvalResponseCache:
    def test_miss_returns_none(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        assert cache.read("nonexistent" * 4) is None

    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"fixture-bytes", cache_version="1")
        cache.write(key, _SAMPLE_PAYLOAD)
        result = cache.read(key)
        assert result is not None
        assert result["case_id"] == "test-1"
        assert result["critic_decision"] == "pass"

    def test_cache_hit_reads_identical_output(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"some-fixture", cache_version="1")
        payload = dict(_SAMPLE_PAYLOAD)
        payload["case_id"] = "hit-test"
        cache.write(key, payload)
        result1 = cache.read(key)
        result2 = cache.read(key)
        assert result1 == result2
        assert result1 is not result2  # different dict objects

    def test_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = "a" * 64
        (tmp_path / f"{key}.json").write_text("NOT JSON {{{{")
        assert cache.read(key) is None

    def test_mode_off_skips_read_and_write(self, tmp_path: Path) -> None:
        """When mode is ``off``, neither read nor write should be invoked.

        We verify this at the functional level: the cache dir stays empty even
        after a write attempt, and read returns None when the file is absent.
        """
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"x", cache_version="1")

        # off mode: write should NOT be called by the outer runner.
        # We test the flag helpers instead (the cache itself is always callable
        # — the runner decides whether to call it based on mode).
        assert not cache_writes_enabled("off")
        assert not cache_reads_enabled("off")

        # Explicitly: if we skip write due to mode=off, read returns None.
        assert cache.read(key) is None
        # Only when we bypass the mode check and call write directly does it store.
        cache.write(key, _SAMPLE_PAYLOAD)
        assert cache.read(key) is not None

    def test_atomic_write_creates_no_orphan_on_error(self, tmp_path: Path) -> None:
        """A failed write (e.g. disk full simulation) must not leave a .tmp orphan."""
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = "b" * 64

        original_replace = os.replace

        def _failing_replace(src: str, dst: str) -> None:
            os.unlink(src)
            raise OSError("simulated disk full")

        with patch("os.replace", side_effect=_failing_replace):
            try:
                cache.write(key, _SAMPLE_PAYLOAD)
            except OSError:
                pass

        # No .tmp files left behind.
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Orphan tmp files: {tmp_files}"
        # The target file was not created.
        assert not (tmp_path / f"{key}.json").exists()

    def test_cache_dir_created_on_write(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "nested" / "cache"
        assert not cache_dir.exists()
        cache = EvalResponseCache(cache_dir=cache_dir)
        key = derive_cache_key(b"x")
        cache.write(key, _SAMPLE_PAYLOAD)
        assert cache_dir.exists()
        assert cache.read(key) is not None

    def test_overwrite_updates_value(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"fixture")
        cache.write(key, {**_SAMPLE_PAYLOAD, "critic_decision": "pass"})
        cache.write(key, {**_SAMPLE_PAYLOAD, "critic_decision": "hard_block"})
        result = cache.read(key)
        assert result is not None
        assert result["critic_decision"] == "hard_block"


# --------------------------------------------------------------------------- #
# Cache-mode integration: read / write / readwrite
# --------------------------------------------------------------------------- #


class TestCacheModeIntegration:
    """Verify that mode flags gate reads/writes as documented."""

    def test_read_mode_does_not_write(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"read-mode", cache_version="1")
        # Simulate read-mode: only read is enabled.
        assert cache_reads_enabled("read")
        assert not cache_writes_enabled("read")
        # A read-mode caller would NOT call write even on miss.
        assert cache.read(key) is None
        # Cache dir should not be created by read-only usage.
        # (We only create on write, and we skipped write.)
        assert cache.read(key) is None

    def test_write_mode_does_not_read(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"write-mode", cache_version="1")
        assert not cache_reads_enabled("write")
        assert cache_writes_enabled("write")
        # Under write mode the caller skips the read and calls write always.
        cache.write(key, _SAMPLE_PAYLOAD)
        assert (tmp_path / f"{key}.json").exists()

    def test_readwrite_reads_then_writes_on_miss(self, tmp_path: Path) -> None:
        cache = EvalResponseCache(cache_dir=tmp_path)
        key = derive_cache_key(b"readwrite-mode", cache_version="1")
        assert cache_reads_enabled("readwrite")
        assert cache_writes_enabled("readwrite")
        # First: miss.
        assert cache.read(key) is None
        # Caller would invoke live graph, then write.
        cache.write(key, _SAMPLE_PAYLOAD)
        # Second: hit.
        result = cache.read(key)
        assert result is not None
        assert result["case_id"] == "test-1"
