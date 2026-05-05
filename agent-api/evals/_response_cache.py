"""Eval response cache — content-hash filesystem store.

Keying strategy
---------------
Cache key = SHA-256 of:

    fixture_bytes  (raw PDF / document bytes, or b"" for evidence-retrieval cases)
 || canonical_prompt_hash  (SHA-256 of ``EVAL_CACHE_PROMPT_HASH`` env var, if set,
                            else the string literal "<unset>" — see note below)
 || model_id  (from ``EVAL_MODEL_ID`` env var; defaults to "<unset>")
 || EVAL_CACHE_VERSION  (env var; defaults to "1")

Why not hash the actual prompts?  The prompts are assembled at graph-node
import time, spread across many modules, and may include dynamic snippets.
We can't reliably enumerate them from outside the graph. Instead we expose a
manual-bump mechanism: whenever prompts change, increment ``EVAL_CACHE_VERSION``
(or set ``EVAL_CACHE_PROMPT_HASH`` to a known sentinel). This is documented
in the commit body and the ARCHITECTURE.md prompt-cache section.

Cache mode — ``EVAL_USE_CACHE`` env var
-----------------------------------------
``off``       (default) — cache reads and writes are both disabled.
``read``      — cache is read; misses fall through to the live call.
               Cache is NOT written on miss (useful for auditing).
``write``     — cache is written on every live call; reads are skipped
               (useful for a warm-up run).
``readwrite`` — cache is read first; on miss, the live call result is
               written back.

Storage
-------
``agent-api/.eval_cache/<sha256>.json``

Writes are atomic: we write to a ``.tmp`` file then rename so a partial write
never poisons the cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #

CacheMode = Literal["off", "read", "write", "readwrite"]

_VALID_MODES: frozenset[str] = frozenset({"off", "read", "write", "readwrite"})

# Default cache directory: agent-api/.eval_cache/
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / ".eval_cache"


# --------------------------------------------------------------------------- #
# Mode resolution
# --------------------------------------------------------------------------- #


def resolve_cache_mode(env_value: Optional[str] = None) -> CacheMode:
    """Parse ``EVAL_USE_CACHE`` into a typed mode.

    Unknown values are treated as ``off`` with a warning so a typo never
    silently enables stale reads.
    """
    raw = (env_value if env_value is not None else os.environ.get("EVAL_USE_CACHE", "off")).strip().lower()
    if raw in _VALID_MODES:
        return raw  # type: ignore[return-value]
    logger.warning(
        "eval_cache.unknown_mode",
        extra={"raw": raw, "fallback": "off"},
    )
    return "off"


def cache_reads_enabled(mode: CacheMode) -> bool:
    return mode in ("read", "readwrite")


def cache_writes_enabled(mode: CacheMode) -> bool:
    return mode in ("write", "readwrite")


# --------------------------------------------------------------------------- #
# Key derivation
# --------------------------------------------------------------------------- #


def derive_cache_key(
    fixture_bytes: bytes,
    *,
    model_id: Optional[str] = None,
    cache_version: Optional[str] = None,
    prompt_hash: Optional[str] = None,
) -> str:
    """Return a 64-char hex SHA-256 cache key.

    Parameters are read from env vars when not explicitly supplied, so
    callers in the runner can just call ``derive_cache_key(fixture_bytes)``
    and the environment drives the rest.

    Components (concatenated before hashing):
        fixture_sha  — SHA-256 of raw fixture bytes (or b"")
        prompt_hash  — ``EVAL_CACHE_PROMPT_HASH`` env var or "<unset>"
        model_id     — ``EVAL_MODEL_ID`` env var or "<unset>"
        version      — ``EVAL_CACHE_VERSION`` env var or "1"
    """
    fixture_sha = hashlib.sha256(fixture_bytes).hexdigest()
    effective_prompt_hash = (
        prompt_hash
        if prompt_hash is not None
        else os.environ.get("EVAL_CACHE_PROMPT_HASH", "<unset>")
    )
    effective_model_id = (
        model_id
        if model_id is not None
        else os.environ.get("EVAL_MODEL_ID", "<unset>")
    )
    effective_version = (
        cache_version
        if cache_version is not None
        else os.environ.get("EVAL_CACHE_VERSION", "1")
    )

    components = "|".join([
        fixture_sha,
        effective_prompt_hash,
        effective_model_id,
        effective_version,
    ])
    return hashlib.sha256(components.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #


def _to_serialisable(obj: Any) -> Any:
    """Recursively convert dataclasses and nested objects to JSON-safe dicts."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_serialisable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_serialisable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serialisable(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# Filesystem store
# --------------------------------------------------------------------------- #


class EvalResponseCache:
    """Filesystem-backed eval response cache.

    Thread/coroutine safety: reads are always safe. Writes use an atomic
    tmp→rename dance so a partial write never poisons the cache. Concurrent
    writers for the same key are benign (last rename wins, all payloads are
    identical for the same key).
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def read(self, key: str) -> Optional[dict[str, Any]]:
        """Return the cached payload, or ``None`` on miss / corruption."""
        p = self._path(key)
        if not p.exists():
            logger.debug(
                "eval_cache.miss",
                extra={"key": key[:16] + "…"},
            )
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            logger.debug(
                "eval_cache.hit",
                extra={"key": key[:16] + "…"},
            )
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "eval_cache.read_error",
                extra={"key": key[:16] + "…", "error_type": type(exc).__name__},
            )
            return None

    def write(self, key: str, payload: Any) -> None:
        """Atomically write ``payload`` (JSON-serialisable) to the cache."""
        self._dir.mkdir(parents=True, exist_ok=True)
        serialised = json.dumps(_to_serialisable(payload), default=str, indent=2)
        target = self._path(key)
        try:
            # Write to a sibling tmp file then rename for atomicity.
            fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(serialised)
                os.replace(tmp_path, target)
            except Exception:
                # Clean up the tmp file on failure; don't leave orphans.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.debug(
                "eval_cache.written",
                extra={"key": key[:16] + "…", "bytes": len(serialised)},
            )
        except OSError as exc:
            logger.warning(
                "eval_cache.write_error",
                extra={"key": key[:16] + "…", "error_type": type(exc).__name__},
            )


# Module-level singleton — shared across the runner and the LLM-judge cache.
_DEFAULT_CACHE = EvalResponseCache()


def get_default_cache() -> EvalResponseCache:
    """Return the module-level singleton cache (uses ``_DEFAULT_CACHE_DIR``)."""
    return _DEFAULT_CACHE


__all__ = [
    "CacheMode",
    "EvalResponseCache",
    "cache_reads_enabled",
    "cache_writes_enabled",
    "derive_cache_key",
    "get_default_cache",
    "resolve_cache_mode",
]
