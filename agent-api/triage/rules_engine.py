"""Deterministic rules engine for UC-1 triage ranking.

Loads rules_engine_config.yaml and evaluates each rule level against a
TriageCriteria.  Returns the first (highest-priority) matching level.

Rules are never re-read from disk mid-run — the table is parsed once
at module import and cached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from triage.criteria import TriageCriteria

logger = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).parent / "rules" / "rules_engine_config.yaml"


@dataclass(frozen=True)
class TriageResult:
    level: int
    label: str
    description: str
    matched_criteria: dict[str, Any]


def _load_rules() -> list[dict[str, Any]]:
    with open(_RULES_PATH) as f:
        data = yaml.safe_load(f)
    return data["priority_levels"]


_RULES: list[dict[str, Any]] = _load_rules()


def _criteria_as_dict(c: TriageCriteria) -> dict[str, Any]:
    return {
        "qsofa_score": c.qsofa_score,
        "critical_lab": c.critical_lab,
        "rapid_response": c.rapid_response,
        "abnormal_lab": c.abnormal_lab,
        "critical_vital": c.critical_vital,
        "critical_vital_respiratory": c.critical_vital_respiratory,
        "critical_vital_circulatory": c.critical_vital_circulatory,
        "mental_status_alert": c.mental_status_alert,
        "pain_score_high": c.pain_score_high,
        "active_condition": c.active_condition,
        "blank_code_status": c.blank_code_status,
    }


def _evaluate_condition(key: str, rule_value: Any, actual: dict[str, Any]) -> bool:
    """Evaluate a single criteria field against the actual value."""
    actual_val = actual.get(key)
    if isinstance(rule_value, bool):
        return actual_val == rule_value
    if isinstance(rule_value, dict):
        if "gte" in rule_value:
            return isinstance(actual_val, (int, float)) and actual_val >= rule_value["gte"]
        if "lte" in rule_value:
            return isinstance(actual_val, (int, float)) and actual_val <= rule_value["lte"]
        if "eq" in rule_value:
            return actual_val == rule_value["eq"]
    return False


def _matches(rule: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Return True if the criteria block in a rule matches the actual criteria dict."""
    criteria = rule.get("criteria", {})
    if not criteria:
        return True  # level 10 — catch-all

    any_of = criteria.pop("any_of", None)

    # All top-level criteria must be satisfied
    for key, rule_val in criteria.items():
        if not _evaluate_condition(key, rule_val, actual):
            return False

    # any_of: at least one alternative block must match
    if any_of:
        alt_match = False
        for alternative in any_of:
            if all(_evaluate_condition(k, v, actual) for k, v in alternative.items()):
                alt_match = True
                break
        if not alt_match:
            return False

    return True


def rank(criteria: TriageCriteria) -> TriageResult:
    """Return the highest-priority matching rule for the given criteria."""
    actual = _criteria_as_dict(criteria)

    for rule in _RULES:
        rule_copy = dict(rule["criteria"]) if rule.get("criteria") else {}
        any_of = rule_copy.pop("any_of", None)
        rule_with_any = {**rule, "criteria": {**rule_copy, **({"any_of": any_of} if any_of else {})}}

        if _matches(rule_with_any, actual):
            matched = {k: actual[k] for k in rule.get("criteria", {}) if k != "any_of" and k in actual}
            logger.debug(
                "Triage level assigned",
                extra={"level": rule["level"], "label": rule["label"], "criteria": matched},
            )
            return TriageResult(
                level=rule["level"],
                label=rule["label"],
                description=rule["description"],
                matched_criteria=matched,
            )

    # Should never reach here due to level-10 catch-all
    return TriageResult(level=10, label="Routine", description="No elevated criteria", matched_criteria={})
