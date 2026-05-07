"""ADT^A08 parser → :class:`DemographicUpdateEvent` (Phase 9 Slice 9.4).

Per todo.md Slice 9.4 the ADT path drops:
  * EVN-6 free-text reason (low signal, high PHI risk)
  * PID-15 (primary language), PID-16 (marital status), PID-22 (ethnic group)
  * PD1-4 NPI sync (handled separately in a future provider-sync slice)

The output is **not** routed into :class:`extractors.schemas.LabReport`
or :class:`extractors.schemas.IntakeForm`. It's a sibling type that
Slice 9.2's resolver wires into the demographics-update flow. This
slice only produces the type — wiring is downstream.

Citations: every populated field carries a :class:`Citation` with a
synthetic locator (``PID-3.1``, ``PV1-3``, ``IN1-2``). ``source_type``
is ``"document"`` (HL7 messages round-trip through the documents table
per W2_ARCHITECTURE §4.2.1, citation contract Option 3).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from extractors.schemas import Citation

from .types import DemographicField, DemographicUpdateEvent


def _cite(
    document_reference_id: str,
    locator: str,
    quote: str,
) -> Citation:
    return Citation(
        source_type="document",
        source_id=document_reference_id,
        page_or_section=None,
        field_or_chunk_id=locator,
        quote_or_value=quote,
    )


def _df(
    document_reference_id: str,
    locator: str,
    quote: str,
) -> DemographicField:
    return DemographicField(value=quote, citations=[_cite(document_reference_id, locator, quote)])


def _value_or_none(node) -> Optional[str]:
    """Return ``node.value.strip()`` or ``None`` if unset/empty.

    hl7apy returns either a string ``value`` or a complex object whose
    ``to_er7()`` shows the raw ER-7 form. We always want the leaf string
    when it exists.
    """
    if node is None:
        return None
    val = getattr(node, "value", None)
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _parse_dob(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    # PID-7 is TS — YYYYMMDD or YYYYMMDDHHMMSS.
    digits = raw[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def _first_segment(message, name: str):
    for child in message.children:
        if child.name == name:
            return child
    return None


def parse_adt_a08(
    message,
    *,
    document_reference_id: str,
    patient_id: str,
    control_id: str,
) -> DemographicUpdateEvent:
    """Convert a parsed hl7apy ADT^A08 message into a DemographicUpdateEvent.

    *message* is the already-parsed hl7apy Message. The dispatcher owns
    the byte→message conversion so this function stays pure.
    """
    pid = _first_segment(message, "PID")
    pv1 = _first_segment(message, "PV1")
    nk1 = _first_segment(message, "NK1")
    gt1 = _first_segment(message, "GT1")
    in1 = _first_segment(message, "IN1")

    fields: dict[str, object] = {
        "patient_id": patient_id,
        "document_reference_id": document_reference_id,
        "event_type": "ADT^A08",
        "control_id": control_id,
        "extracted_at": datetime.now(timezone.utc),
    }

    # PID — required to even get here (dispatcher validates), but we re-check
    # gracefully so partial messages produce as much output as possible.
    if pid is not None:
        # PID-3.1 — first MRN repetition's first component
        try:
            mrn_val = _value_or_none(pid.pid_3.pid_3_1)
        except Exception:  # noqa: BLE001 — defensive against grammar drift
            mrn_val = None
        if mrn_val:
            fields["mrn"] = _df(document_reference_id, "PID-3.1", mrn_val)

        try:
            family = _value_or_none(pid.pid_5.xpn_1)
        except Exception:  # noqa: BLE001
            family = None
        if family:
            fields["name_family"] = _df(document_reference_id, "PID-5.1", family)

        try:
            given = _value_or_none(pid.pid_5.xpn_2)
        except Exception:  # noqa: BLE001
            given = None
        if given:
            fields["name_given"] = _df(document_reference_id, "PID-5.2", given)

        try:
            middle = _value_or_none(pid.pid_5.xpn_3)
        except Exception:  # noqa: BLE001
            middle = None
        if middle:
            fields["name_middle"] = _df(document_reference_id, "PID-5.3", middle)

        try:
            dob_raw = _value_or_none(pid.pid_7)
        except Exception:  # noqa: BLE001
            dob_raw = None
        dob = _parse_dob(dob_raw)
        if dob is not None:
            fields["dob"] = dob

        try:
            sex_val = _value_or_none(pid.pid_8)
        except Exception:  # noqa: BLE001
            sex_val = None
        if sex_val:
            fields["sex"] = _df(document_reference_id, "PID-8", sex_val)

        try:
            race_val = _value_or_none(pid.pid_10.ce_2)
        except Exception:  # noqa: BLE001
            race_val = None
        if race_val:
            fields["race"] = _df(document_reference_id, "PID-10.2", race_val)

        # PID-11 (XAD) — line, city, state, postal
        for attr, locator, key in (
            ("xad_1", "PID-11.1", "address_line"),
            ("xad_3", "PID-11.3", "address_city"),
            ("xad_4", "PID-11.4", "address_state"),
            ("xad_5", "PID-11.5", "address_postal"),
        ):
            try:
                val = _value_or_none(getattr(pid.pid_11, attr))
            except Exception:  # noqa: BLE001
                val = None
            if val:
                fields[key] = _df(document_reference_id, locator, val)

        # PID-13 home phone (XTN). Use to_er7() because it's a composite
        # whose .value attribute is unreliable across hl7apy versions.
        try:
            phone_raw = pid.pid_13.to_er7().strip()
        except Exception:  # noqa: BLE001
            phone_raw = ""
        if phone_raw:
            fields["home_phone"] = _df(document_reference_id, "PID-13", phone_raw)

    # PV1 — class (PV1-2), assigned location (PV1-3), attending NPI (PV1-7.1)
    if pv1 is not None:
        try:
            patient_class = _value_or_none(pv1.pv1_2)
        except Exception:  # noqa: BLE001
            patient_class = None
        if patient_class:
            fields["patient_class"] = _df(document_reference_id, "PV1-2", patient_class)

        try:
            location = pv1.pv1_3.to_er7().strip()
        except Exception:  # noqa: BLE001
            location = ""
        if location:
            fields["assigned_location"] = _df(document_reference_id, "PV1-3", location)

        try:
            attending_npi = _value_or_none(pv1.pv1_7.xcn_1)
        except Exception:  # noqa: BLE001
            attending_npi = None
        if attending_npi:
            fields["attending_npi"] = _df(document_reference_id, "PV1-7.1", attending_npi)

    # NK1 — first occurrence only in v1
    if nk1 is not None:
        try:
            nok_name = nk1.nk1_2.to_er7().strip()
        except Exception:  # noqa: BLE001
            nok_name = ""
        if nok_name:
            fields["nok_name"] = _df(document_reference_id, "NK1-2", nok_name)

        try:
            relationship = _value_or_none(nk1.nk1_3)
        except Exception:  # noqa: BLE001
            relationship = None
        if relationship:
            fields["nok_relationship"] = _df(document_reference_id, "NK1-3", relationship)

        try:
            nok_phone = nk1.nk1_5.to_er7().strip()
        except Exception:  # noqa: BLE001
            nok_phone = ""
        if nok_phone:
            fields["nok_phone"] = _df(document_reference_id, "NK1-5", nok_phone)

    # GT1 — first occurrence only in v1
    if gt1 is not None:
        try:
            guarantor_name = gt1.gt1_3.to_er7().strip()
        except Exception:  # noqa: BLE001
            guarantor_name = ""
        if guarantor_name:
            fields["guarantor_name"] = _df(document_reference_id, "GT1-3", guarantor_name)

    # IN1 — first occurrence only in v1
    if in1 is not None:
        try:
            plan_id = _value_or_none(in1.in1_2)
        except Exception:  # noqa: BLE001
            plan_id = None
        if plan_id:
            fields["insurance_plan_id"] = _df(document_reference_id, "IN1-2", plan_id)

        try:
            company = in1.in1_4.to_er7().strip()
        except Exception:  # noqa: BLE001
            company = ""
        if company:
            fields["insurance_company"] = _df(document_reference_id, "IN1-4", company)

        try:
            member_id = _value_or_none(in1.in1_36)
        except Exception:  # noqa: BLE001
            member_id = None
        if member_id:
            fields["insurance_member_id"] = _df(document_reference_id, "IN1-36", member_id)

    return DemographicUpdateEvent(**fields)
