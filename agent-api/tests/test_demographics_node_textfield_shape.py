"""Regression tests for the TextField-shape demographics flatten helper.

The IntakeForm extractor emits ``demographics`` as TextField dicts
(``{"value": str, "citations": [...], "needs_review": bool}``) per field,
so the citation chain survives. The pure-function comparator calls
``.strip()`` on each field — raises ``AttributeError`` on a dict.

Without ``_flatten_doc_demographics``, the eval boundary catches the
exception and silently sets ``extraction=None``, which cascades
``schema_valid``, ``citation_present``, ``citation_resolvable`` to False
for every IntakeForm-producing modality (docx_referral most visibly).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from graph.nodes.demographics import _flatten_doc_demographics  # noqa: E402


@pytest.mark.hard_failure
def test_flatten_textfield_shape_returns_value_strings() -> None:
    doc = {
        "mrn": {"value": "MRN-1", "citations": [], "needs_review": False},
        "name": {"value": "Jane Doe", "citations": [], "needs_review": False},
        "dob": {"value": "1980-01-01", "citations": [], "needs_review": False},
    }
    flat = _flatten_doc_demographics(doc)
    assert flat == {"mrn": "MRN-1", "name": "Jane Doe", "dob": "1980-01-01"}


@pytest.mark.hard_failure
def test_flatten_flat_string_shape_passes_through() -> None:
    doc = {"mrn": "MRN-2", "name": "John Smith", "dob": "1975-06-15"}
    flat = _flatten_doc_demographics(doc)
    assert flat == doc


@pytest.mark.hard_failure
def test_flatten_missing_keys_collapse_to_none() -> None:
    doc = {"mrn": "MRN-3"}
    flat = _flatten_doc_demographics(doc)
    assert flat == {"mrn": "MRN-3", "name": None, "dob": None}


@pytest.mark.hard_failure
def test_flatten_textfield_with_missing_value_collapses_to_none() -> None:
    doc = {"mrn": {"citations": []}, "name": "x", "dob": "y"}
    flat = _flatten_doc_demographics(doc)
    assert flat == {"mrn": None, "name": "x", "dob": "y"}


@pytest.mark.hard_failure
def test_flatten_unknown_shape_collapses_to_none() -> None:
    assert _flatten_doc_demographics(None) == {"mrn": None, "name": None, "dob": None}
    assert _flatten_doc_demographics([1, 2, 3]) == {"mrn": None, "name": None, "dob": None}
    assert _flatten_doc_demographics(42) == {"mrn": None, "name": None, "dob": None}
