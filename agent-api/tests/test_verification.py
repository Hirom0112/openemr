"""Eval suite — Verification layer tests (deterministic).

Covers 15 of the 47 required tests.
Hard failure gate: 100% pass required.
"""

from __future__ import annotations

import pytest
from verification.domain_constraints import (
    verify_triage_entry,
    verify_conversation_answer,
    verify_safety_summary,
    _strip_recommendations,
)
from briefing.schema import BriefingResponse, BriefingSection, ClinicalClaim
from briefing.context_builder import BriefingContext, Citation, AllergyEntry
from verification.source_attribution import verify_briefing


# ── Domain constraints — triage ───────────────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestDomainConstraints:
    def test_recommendation_language_stripped(self):
        result = _strip_recommendations("Patient should receive antibiotics.", "pt-1")
        assert "should" not in result.lower()

    def test_consider_language_stripped(self):
        result = _strip_recommendations("Consider ordering a CT scan.", "pt-1")
        assert "consider" not in result.lower()

    def test_safe_text_passes_through(self):
        text = "Patient has a potassium of 6.4 mEq/L."
        assert _strip_recommendations(text, "pt-1") == text

    def test_nkda_blocked_when_allergy_blank(self):
        entry = {"patient_id": "pt-1", "explanation": "No known allergies documented."}
        bundle = {"resources": {"AllergyIntolerance": []}}
        verified = verify_triage_entry(entry, bundle)
        warnings = verified.get("verification_warnings", [])
        assert any("CONSTRAINT_VIOLATION" in w for w in warnings)

    def test_nkda_allowed_when_allergies_present(self):
        entry = {"patient_id": "pt-1", "explanation": "Penicillin allergy documented."}
        bundle = {"resources": {"AllergyIntolerance": [{"resource": {"code": {"coding": [{"display": "Penicillin"}]}}}]}}
        verified = verify_triage_entry(entry, bundle)
        warnings = verified.get("verification_warnings", [])
        constraint_warnings = [w for w in warnings if "CONSTRAINT_VIOLATION" in w]
        assert len(constraint_warnings) == 0

    def test_stale_critical_flag_added(self):
        from datetime import datetime, timedelta, timezone
        stale_dt = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        entry = {"patient_id": "pt-1", "explanation": "K+ 6.4 is critically high."}
        bundle = {
            "resources": {
                "Observation": [{
                    "resource": {
                        "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]},
                        "valueQuantity": {"value": 6.4, "unit": "mEq/L"},
                        "interpretation": [{"coding": [{"code": "HH"}]}],
                        "effectiveDateTime": stale_dt,
                    }
                }],
                "AllergyIntolerance": [],
            }
        }
        verified = verify_triage_entry(entry, bundle)
        warnings = verified.get("verification_warnings", [])
        assert any("STALE_CRITICAL" in w for w in warnings)

    def test_recent_critical_not_flagged_stale(self):
        from datetime import datetime, timedelta, timezone
        recent_dt = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        entry = {"patient_id": "pt-1", "explanation": "K+ 6.4."}
        bundle = {
            "resources": {
                "Observation": [{
                    "resource": {
                        "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]},
                        "valueQuantity": {"value": 6.4},
                        "interpretation": [{"coding": [{"code": "HH"}]}],
                        "effectiveDateTime": recent_dt,
                    }
                }],
                "AllergyIntolerance": [],
            }
        }
        verified = verify_triage_entry(entry, bundle)
        warnings = verified.get("verification_warnings", [])
        assert not any("STALE_CRITICAL" in w for w in warnings)


# ── Source attribution — briefing ─────────────────────────────────────────────

def _make_ctx(patient_id: str = "pt-1", has_blank_allergy: bool = False, has_blank_code: bool = False) -> BriefingContext:
    from datetime import datetime, timezone
    allergy = AllergyEntry(
        substance="Penicillin",
        reaction="Rash",
        severity="mild",
        citation=Citation(resource_type="AllergyIntolerance", code="372687004", display="Penicillin", effective_dt="2024-01-01", value="Rash (mild)"),
    )
    return BriefingContext(
        patient_id=patient_id, name="Test Patient", dob="1970-01-01", mrn="MR-001",
        code_status="" if has_blank_code else "Full Code",
        active_conditions=[], allergies=[] if has_blank_allergy else [allergy],
        active_medications=[], recent_vitals=[], recent_labs=[],
        has_blank_allergy_section=has_blank_allergy,
        has_blank_code_status=has_blank_code,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_briefing(claims: list[ClinicalClaim] | None = None) -> BriefingResponse:
    from datetime import datetime, timezone
    section = BriefingSection(
        section="allergies",
        summary="Patient is allergic to Penicillin.",
        claims=claims or [
            ClinicalClaim(
                text="Patient is allergic to Penicillin.",
                source_resource="AllergyIntolerance",
                source_code="372687004",
                source_value="Rash (mild)",
                source_dt="2024-01-01",
            )
        ],
    )
    return BriefingResponse(
        patient_id="pt-1", name="Test Patient",
        sections=[section], alerts=[],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.mark.clinical_accuracy
class TestSourceAttribution:
    def test_attributed_claim_kept(self):
        ctx = _make_ctx()
        briefing = _make_briefing()
        result = verify_briefing(briefing, ctx, strict=True)
        assert len(result.sections[0].claims) == 1

    def test_unattributed_claim_removed_strict(self):
        ctx = _make_ctx()
        unattributed = ClinicalClaim(
            text="Patient took aspirin.", source_resource="AllergyIntolerance",
            source_code="NOT-A-CODE", source_value="INVENTED", source_dt="",
        )
        briefing = _make_briefing([unattributed])
        result = verify_briefing(briefing, ctx, strict=True)
        assert len(result.sections[0].claims) == 0

    @pytest.mark.hard_failure
    def test_blank_code_status_alert_added(self):
        ctx = _make_ctx(has_blank_code=True)
        briefing = _make_briefing()
        result = verify_briefing(briefing, ctx)
        assert any("BLANK_CODE_STATUS" in a for a in result.alerts)

    @pytest.mark.hard_failure
    def test_nkda_assertion_blocked_blank_allergy(self):
        from datetime import datetime, timezone
        ctx = _make_ctx(has_blank_allergy=True)
        section = BriefingSection(
            section="allergies", summary="No known allergies.", claims=[],
        )
        briefing = BriefingResponse(
            patient_id="pt-1", name="Test", sections=[section], alerts=[],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        result = verify_briefing(briefing, ctx)
        assert any("ATTRIBUTION_VIOLATION" in a for a in result.alerts)

    def test_no_false_alerts_on_complete_data(self):
        ctx = _make_ctx(has_blank_allergy=False, has_blank_code=False)
        briefing = _make_briefing()
        result = verify_briefing(briefing, ctx, strict=True)
        assert result.alerts == []


# ── UC-3 conversation answer verification ─────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestConversationAnswerVerification:
    def test_recommendation_stripped_from_answer(self):
        answer = "The potassium is 6.4. You should order kayexalate immediately."
        result = verify_conversation_answer(answer, "pt-1")
        assert "should" not in result.lower()

    def test_consider_stripped_from_answer(self):
        answer = "Creatinine is 2.1 mg/dL. Consider holding metformin."
        result = verify_conversation_answer(answer, "pt-1")
        assert "consider" not in result.lower()

    def test_clean_answer_passes_through(self):
        answer = "The last creatinine was 1.2 mg/dL on 2024-01-15 (Observation)."
        result = verify_conversation_answer(answer, "pt-1")
        assert result == answer

    def test_prescribe_stripped_from_answer(self):
        answer = "Vancomycin trough is 8. Prescribe a higher dose."
        result = verify_conversation_answer(answer, "pt-1")
        assert "prescribe" not in result.lower()


# ── UC-4 safety summary verification ─────────────────────────────────────────

@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestSafetySummaryVerification:
    def test_safe_assertion_stripped_when_flags_present(self):
        summary = "Heparin is flagged as high-alert. The medication list is safe overall."
        result = verify_safety_summary(summary, "pt-1", has_flags=True)
        assert "is safe" not in result.lower()

    def test_no_safety_concerns_stripped_when_flags_present(self):
        summary = "There are no safety concerns with the current regimen."
        result = verify_safety_summary(summary, "pt-1", has_flags=True)
        assert "no safety concerns" not in result.lower()

    def test_safe_assertion_allowed_when_no_flags(self):
        summary = "No drug interactions detected for the current medication list."
        result = verify_safety_summary(summary, "pt-1", has_flags=False)
        assert result == summary

    def test_recommendation_stripped_from_safety_summary(self):
        summary = "[HIGH] Heparin allergy conflict detected. You should discontinue heparin."
        result = verify_safety_summary(summary, "pt-1", has_flags=True)
        assert "should" not in result.lower()
