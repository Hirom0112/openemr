"""Eval suite public surface.

Exposes the shared :class:`EvalConfigError` used by rubrics and the
``run_full_suite`` startup check to fail-fast when the eval cannot run
because of missing or invalid configuration.
"""
from __future__ import annotations


class EvalConfigError(RuntimeError):
    """Raised when an eval cannot run because of missing or invalid configuration (e.g., absent ANTHROPIC_API_KEY for LLM-judge rubrics)."""


__all__ = ["EvalConfigError"]
