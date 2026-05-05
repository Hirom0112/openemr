"""W2 50-case eval-set definitions (Slices 5.1 + 5.4).

Each case is a frozen dataclass that points at a fixture key (resolved by
``tests/fixtures/eval/_generate_eval_corpus.py:generate_all``) and carries the
expected agent outcome — kind, critic decision, violation/softwarn codes, and
field assertions. Slices 5.2/5.3's rubric runners consume ``CASES`` directly.

All names, MRNs, and DOBs are SYNTHETIC. The whitelist of synthetic identities
lives in ``tests/test_w2_eval_no_real_phi.py``; do not add a new patient
identity here without updating that whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Bucket = Literal[
    "lab_nominal",
    "intake_nominal",
    "unknown_nominal",
    "wrong_type_hint",
    "wrong_patient",
    "blank_noise",
    "mixed_content",
    "low_quality_scan",
    "intra_doc_conflict",
]

ExpectedKind = Literal["lab_report", "intake_form", "unknown"]
ExpectedDecision = Literal["pass", "soft_warn", "hard_block"]


@dataclass(frozen=True)
class W2EvalCase:
    case_id: str
    bucket: Bucket
    fixture_key: str  # key into eval._generate_eval_corpus.generate_all()
    doc_type_hint: str | None
    chart_patient: dict  # FHIR Patient JSON shape
    expected_kind: ExpectedKind
    expected_critic_decision: ExpectedDecision
    expected_violation_codes: tuple[str, ...] = ()
    expected_softwarn_codes: tuple[str, ...] = ()
    expected_field_assertions: tuple[tuple[str, str], ...] = ()
    notes: str = ""


# ---------------------------------------------------------------------------
# Synthetic FHIR Patient builder
# ---------------------------------------------------------------------------


def _patient(
    *,
    pid: str,
    mrn: str,
    given: str,
    family: str,
    dob: str,
    gender: str | None = None,
) -> dict:
    out: dict = {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": mrn},
        ],
        "name": [{"given": [given], "family": family}],
        "birthDate": dob,
    }
    if gender is not None:
        out["gender"] = gender
    return out


# Convenience patient constants (each pinned to a synthetic identity used in
# at least one case). Keep these aligned with the whitelist in the no-PHI test.
PT_MARCUS_WEBB = _patient(
    pid="pt-100847", mrn="100847", given="Marcus", family="Webb", dob="1962-03-14", gender="male"
)
PT_JANE_DOE = _patient(
    pid="pt-100231", mrn="100231", given="Jane", family="Doe", dob="1978-07-22", gender="female"
)
PT_CARLOS_REYES = _patient(
    pid="pt-100412", mrn="100412", given="Carlos", family="Reyes", dob="1955-11-02", gender="male"
)
PT_PRIYA_NATARAJAN = _patient(
    pid="pt-100558", mrn="100558", given="Priya", family="Natarajan", dob="1990-01-19",
    gender="female",
)
PT_LIANG_PARK = _patient(
    pid="pt-100719", mrn="100719", given="Liang", family="Park", dob="1971-09-30", gender="male"
)
PT_ELEANOR_WHITFIELD = _patient(
    pid="pt-100923", mrn="100923", given="Eleanor", family="Whitfield", dob="1942-08-11",
    gender="female",
)
PT_TOMAS_ALBRIGHT = _patient(
    pid="pt-100377", mrn="100377", given="Tomas", family="Albright", dob="1985-02-26",
    gender="male",
)
PT_HANNAH_GOLDBERG = _patient(
    pid="pt-100644", mrn="100644", given="Hannah", family="Goldberg", dob="2001-12-04",
    gender="female",
)
PT_SVEN_HALVORSEN = _patient(
    pid="pt-100805", mrn="100805", given="Sven", family="Halvorsen", dob="1968-05-17",
    gender="male",
)
PT_YARA_OLSSON = _patient(
    pid="pt-100488", mrn="100488", given="Yara", family="Olsson", dob="1980-06-09", gender="female"
)
PT_AMINA_BAKR = _patient(
    pid="pt-100176", mrn="100176", given="Amina", family="Bakr", dob="1995-04-08", gender="female"
)
PT_GREGOR_VOLKOV = _patient(
    pid="pt-100265", mrn="100265", given="Gregor", family="Volkov", dob="1948-10-25", gender="male"
)
PT_NIA_OKAFOR = _patient(
    pid="pt-100033", mrn="100033", given="Nia", family="Okafor", dob="2007-02-13", gender="female"
)
PT_KENJI_TANAKA = _patient(
    pid="pt-100922", mrn="100922", given="Kenji", family="Tanaka", dob="1959-12-30", gender="male"
)
PT_ROSA_MENDEZ = _patient(
    pid="pt-100611", mrn="100611", given="Rosa", family="Mendez", dob="1983-03-21", gender="female"
)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


CASES: list[W2EvalCase] = [
    # =====================================================================
    # Bucket: lab_nominal — 12 cases
    # =====================================================================
    W2EvalCase(
        case_id="lab_nominal_001_osh_lactate",
        bucket="lab_nominal",
        fixture_key="lab_osh_lactate",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='lactate'].value", "4.2"),),
        notes="Demo headline value: critical lactate.",
    ),
    W2EvalCase(
        case_id="lab_nominal_002_osh_lactate_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_osh_lactate",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='lactate'].value", "4.2"),),
        notes="No type hint — classifier alone must resolve.",
    ),
    W2EvalCase(
        case_id="lab_nominal_003_cbc_bmp",
        bucket="lab_nominal",
        fixture_key="lab_clean_2",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(
            ("values[?test_name=='wbc'].value", "6.8"),
            ("values[?test_name=='glucose'].value", "92"),
        ),
    ),
    W2EvalCase(
        case_id="lab_nominal_004_cbc_bmp_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_clean_2",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='hgb'].value", "13.4"),),
    ),
    W2EvalCase(
        case_id="lab_nominal_005_lipid",
        bucket="lab_nominal",
        fixture_key="lab_clean_3",
        doc_type_hint="lab_report",
        chart_patient=PT_CARLOS_REYES,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='ldl'].value", "104"),),
    ),
    W2EvalCase(
        case_id="lab_nominal_006_lipid_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_clean_3",
        doc_type_hint=None,
        chart_patient=PT_CARLOS_REYES,
        expected_kind="lab_report",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="lab_nominal_007_critical_glucose",
        bucket="lab_nominal",
        fixture_key="lab_critical_4",
        doc_type_hint="lab_report",
        chart_patient=PT_PRIYA_NATARAJAN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='glucose'].value", "612"),),
        notes="Critical HH glucose; agent should surface flag, not soft-warn.",
    ),
    W2EvalCase(
        case_id="lab_nominal_008_critical_glucose_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_critical_4",
        doc_type_hint=None,
        chart_patient=PT_PRIYA_NATARAJAN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="lab_nominal_009_osh_lactate_male_only_chart",
        bucket="lab_nominal",
        fixture_key="lab_osh_lactate",
        doc_type_hint="lab_report",
        # chart matches MRN/name/DOB; minor identifier shape variation only.
        chart_patient={
            "resourceType": "Patient",
            "id": "pt-100847",
            "identifier": [{"type": {"coding": [{"code": "MR"}]}, "value": "100847"}],
            "name": [{"given": ["Marcus", "J"], "family": "Webb"}],
            "birthDate": "1962-03-14",
        },
        expected_kind="lab_report",
        expected_critic_decision="pass",
        notes="Chart name carries middle initial — should still pass.",
    ),
    W2EvalCase(
        case_id="lab_nominal_010_cbc_bmp_alt_chart",
        bucket="lab_nominal",
        fixture_key="lab_clean_2",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='cr'].value", "0.9"),),
    ),
    W2EvalCase(
        case_id="lab_nominal_011_lipid_repeat",
        bucket="lab_nominal",
        fixture_key="lab_clean_3",
        doc_type_hint="lab_report",
        chart_patient=PT_CARLOS_REYES,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='triglycerides'].value", "118"),),
    ),
    W2EvalCase(
        case_id="lab_nominal_012_critical_anion_gap",
        bucket="lab_nominal",
        fixture_key="lab_critical_4",
        doc_type_hint="lab_report",
        chart_patient=PT_PRIYA_NATARAJAN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='anion_gap'].value", "22"),),
    ),
    # =====================================================================
    # Bucket: intake_nominal — 10 cases
    # =====================================================================
    W2EvalCase(
        case_id="intake_nominal_001_full_code",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("code_status", "Full Code"),),
    ),
    W2EvalCase(
        case_id="intake_nominal_002_full_code_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_003_dnr",
        bucket="intake_nominal",
        fixture_key="intake_dnr",
        doc_type_hint="intake_form",
        chart_patient=PT_ELEANOR_WHITFIELD,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("code_status", "DNR / DNI"),),
        notes="DNR/DNI must extract verbatim from the document.",
    ),
    W2EvalCase(
        case_id="intake_nominal_004_dnr_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_dnr",
        doc_type_hint=None,
        chart_patient=PT_ELEANOR_WHITFIELD,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_005_nkda",
        bucket="intake_nominal",
        fixture_key="intake_no_allergies",
        doc_type_hint="intake_form",
        chart_patient=PT_TOMAS_ALBRIGHT,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("allergies", "NKDA"),),
    ),
    W2EvalCase(
        case_id="intake_nominal_006_nkda_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_no_allergies",
        doc_type_hint=None,
        chart_patient=PT_TOMAS_ALBRIGHT,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_007_minimal",
        bucket="intake_nominal",
        fixture_key="intake_minimal",
        doc_type_hint="intake_form",
        chart_patient=PT_HANNAH_GOLDBERG,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        notes="Most fields blank — chief concern only.",
    ),
    W2EvalCase(
        case_id="intake_nominal_008_minimal_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_minimal",
        doc_type_hint=None,
        chart_patient=PT_HANNAH_GOLDBERG,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_009_dnr_repeat_match",
        bucket="intake_nominal",
        fixture_key="intake_dnr",
        doc_type_hint="intake_form",
        chart_patient=PT_ELEANOR_WHITFIELD,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("allergies", "Sulfa - hives"),),
    ),
    W2EvalCase(
        case_id="intake_nominal_010_full_code_repeat",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("allergies", "Penicillin - rash"),),
    ),
    # =====================================================================
    # Bucket: unknown_nominal — 6 cases
    # =====================================================================
    W2EvalCase(
        case_id="unknown_nominal_001_consultant_note_no_hint",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
        notes="Cardiology consult — should classify as unknown without hint.",
    ),
    W2EvalCase(
        case_id="unknown_nominal_002_consultant_note_unknown_hint",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="unknown_nominal_003_imaging_no_hint",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        notes="Radiology report — unknown type, classifier should refuse to fit lab/intake.",
    ),
    W2EvalCase(
        case_id="unknown_nominal_004_imaging_unknown_hint",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint="unknown",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="unknown_nominal_005_consultant_note_alt_chart",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="unknown_nominal_006_imaging_repeat",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    # =====================================================================
    # Bucket: wrong_type_hint — 4 cases
    # =====================================================================
    W2EvalCase(
        case_id="wrong_type_hint_001_lab_hinted_intake",
        bucket="wrong_type_hint",
        fixture_key="lab_osh_lactate",
        doc_type_hint="intake_form",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
        notes="Hint says intake; document is a lab. Classifier overrides hint, soft-warn.",
    ),
    W2EvalCase(
        case_id="wrong_type_hint_002_intake_hinted_lab",
        bucket="wrong_type_hint",
        fixture_key="intake_admission",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
    ),
    W2EvalCase(
        case_id="wrong_type_hint_003_consult_hinted_lab",
        bucket="wrong_type_hint",
        fixture_key="consultant_note",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
        notes="Consultant note hinted as lab — fall back to unknown with warning.",
    ),
    W2EvalCase(
        case_id="wrong_type_hint_004_imaging_hinted_intake",
        bucket="wrong_type_hint",
        fixture_key="imaging_report",
        doc_type_hint="intake_form",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
    ),
    # =====================================================================
    # Bucket: wrong_patient — 5 cases
    #
    # All reuse intake_admission.pdf, which carries
    #     name=Marcus Webb, MRN=100847, DOB=1962-03-14
    # We mutate chart_patient to drive each row of W2_ARCHITECTURE §5.6.
    # =====================================================================
    W2EvalCase(
        case_id="wrong_patient_001_mrn_match_dob_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN matches, name matches, DOB differs by year -> soft_warn
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Marcus", family="Webb", dob="1961-03-14",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("dob_mismatch",),
        notes="§5.6 row 2: MRN+name match, DOB mismatch.",
    ),
    W2EvalCase(
        case_id="wrong_patient_002_mrn_match_name_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN matches, DOB matches, name differs entirely -> soft_warn
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Markus", family="Webber", dob="1962-03-14",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("name_mismatch",),
        notes="§5.6 row 3: MRN+DOB match, name mismatch.",
    ),
    W2EvalCase(
        case_id="wrong_patient_003_mrn_match_both_other_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN matches, name AND DOB differ -> soft_warn (possible OCR collision)
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Roberta", family="Klein", dob="1979-09-01",
            gender="female",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("possible_mrn_ocr_collision",),
        notes="§5.6 row 4: MRN match alone — possible OCR collision.",
    ),
    W2EvalCase(
        case_id="wrong_patient_004_mrn_extracted_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # Document MRN is 100847, chart MRN is something else entirely -> hard_block
        chart_patient=_patient(
            pid="pt-200999", mrn="200999", given="Marcus", family="Webb", dob="1962-03-14",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="hard_block",
        expected_violation_codes=("mrn_mismatch",),
        notes="§5.6 row 5: extracted MRN mismatches chart — hard block.",
    ),
    W2EvalCase(
        case_id="wrong_patient_005_absent_mrn_dob_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # Simulate absent-MRN-on-document by mismatching chart DOB while name matches.
        # Per §5.6 row 9: absent MRN + name match + DOB mismatch -> hard_block.
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Marcus", family="Webb", dob="1972-11-30",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="hard_block",
        expected_violation_codes=("dob_critical_mismatch",),
        notes=(
            "§5.6 row 9 analog: when the agent treats the doc MRN as absent or "
            "unreadable, a DOB mismatch is the dominant signal and hard-blocks."
        ),
    ),
    # =====================================================================
    # Bucket: blank_noise — 4 cases
    # =====================================================================
    W2EvalCase(
        case_id="blank_noise_001_blank_pdf",
        bucket="blank_noise",
        fixture_key="blank",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("empty_document",),
    ),
    W2EvalCase(
        case_id="blank_noise_002_encrypted_pdf",
        bucket="blank_noise",
        fixture_key="encrypted",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("unreadable_document",),
        notes="Stub 'ENCRYPTED' page — must refuse cleanly, not hallucinate.",
    ),
    W2EvalCase(
        case_id="blank_noise_003_empty_stream",
        bucket="blank_noise",
        fixture_key="empty_stream",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Single-space PDF — extractor should refuse without claiming content.",
    ),
    W2EvalCase(
        case_id="blank_noise_004_all_noise_scan",
        bucket="blank_noise",
        fixture_key="all_noise_scan",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("unreadable_document",),
    ),
    # =====================================================================
    # Bucket: mixed_content — 4 cases
    # =====================================================================
    W2EvalCase(
        case_id="mixed_content_001_no_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint=None,
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_002_lab_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="lab_report",
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_003_intake_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="intake_form",
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_004_unknown_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="unknown",
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    # =====================================================================
    # Bucket: low_quality_scan — 3 cases
    # =====================================================================
    W2EvalCase(
        case_id="low_quality_scan_001_lab_blurry",
        bucket="low_quality_scan",
        fixture_key="lab_blurry",
        doc_type_hint="lab_report",
        chart_patient=PT_LIANG_PARK,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        expected_field_assertions=(("values[?test_name=='hgb'].value", "12.7"),),
    ),
    W2EvalCase(
        case_id="low_quality_scan_002_lab_blurry_no_hint",
        bucket="low_quality_scan",
        fixture_key="lab_blurry",
        doc_type_hint=None,
        chart_patient=PT_LIANG_PARK,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
    ),
    W2EvalCase(
        case_id="low_quality_scan_003_intake_blurry",
        bucket="low_quality_scan",
        fixture_key="intake_blurry",
        doc_type_hint="intake_form",
        chart_patient=PT_SVEN_HALVORSEN,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
    ),
    # =====================================================================
    # Bucket: intra_doc_conflict — 2 cases
    # =====================================================================
    W2EvalCase(
        case_id="intra_doc_conflict_001_lactate",
        bucket="intra_doc_conflict",
        fixture_key="intra_doc_conflict_lactate",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("intra_doc_conflict",),
        notes="Lactate 4.2 (page 1) vs 2.4 (page 3); conflict detector surfaces both.",
    ),
    W2EvalCase(
        case_id="intra_doc_conflict_002_lactate_no_hint",
        bucket="intra_doc_conflict",
        fixture_key="intra_doc_conflict_lactate",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("intra_doc_conflict",),
    ),
]


# ---------------------------------------------------------------------------
# Bucket count contract
# ---------------------------------------------------------------------------


BUCKET_COUNTS: dict[str, int] = {
    "lab_nominal": 12,
    "intake_nominal": 10,
    "unknown_nominal": 6,
    "wrong_type_hint": 4,
    "wrong_patient": 5,
    "blank_noise": 4,
    "mixed_content": 4,
    "low_quality_scan": 3,
    "intra_doc_conflict": 2,
}


def _validate() -> None:
    counts: dict[str, int] = {}
    for c in CASES:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1
    if counts != BUCKET_COUNTS:
        raise AssertionError(
            f"CASES bucket distribution drifted: got {counts}, want {BUCKET_COUNTS}"
        )
    if sum(BUCKET_COUNTS.values()) != 50:
        raise AssertionError(f"BUCKET_COUNTS must total 50, got {sum(BUCKET_COUNTS.values())}")
    ids = [c.case_id for c in CASES]
    if len(set(ids)) != len(ids):
        raise AssertionError("Duplicate case_id detected")


_validate()
