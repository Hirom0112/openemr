"""Top-level HL7 v2 dispatcher (Phase 9 Slice 9.4).

Routes raw HL7 v2 bytes to the right slice parser based on MSH-9. v1
supports ADT^A08 and ORU^R01 only; everything else raises
:class:`ParserUnsupportedError`.

The control flow is intentionally narrow:
    bytes → normalize_segment_terminators → hl7apy.parse_message →
    branch on MSH-9 → slice parser.

Failure modes:
  * Missing MSH segment → ParserMalformedError("hl7_missing_msh")
  * Missing PID segment → ParserMalformedError("hl7_missing_pid")
  * Unsupported message type → ParserUnsupportedError(message_type)
  * Underlying hl7apy ParserError / ChildNotValid →
    ParserMalformedError("hl7_parse_failed")

Metrics: ``agent_hl7_parse_total{message_type, outcome}`` and
``agent_hl7_parse_duration_seconds{message_type}``. The dispatcher also
emits a structured ``hl7_parse_completed`` log event with control_id +
duration_ms (no PHI fields).
"""

from __future__ import annotations

import logging
import time
from typing import Union

from .adt import parse_adt_a08
from .exceptions import ParserMalformedError, ParserUnsupportedError
from .normalizer import normalize_segment_terminators
from .oru import parse_oru_r01
from .types import DemographicUpdateEvent

logger = logging.getLogger(__name__)


def _import_metrics():
    """Late-binding import of the Prometheus instruments registered by
    :mod:`parsers.hl7._metrics`. Returns ``(None, None)`` when
    ``prometheus_client`` is not installed (e.g. minimal lint-imports
    sandbox) so the dispatcher still functions without observability.

    The metrics live in ``parsers.hl7._metrics`` rather than
    ``agent.metrics`` to honour the ``parsers-hl7-isolated`` importlinter
    contract (parsers.hl7 must not depend on ``agent``).
    """
    try:
        from ._metrics import (  # noqa: WPS433 — deliberate late import
            agent_hl7_parse_duration_seconds,
            agent_hl7_parse_total,
        )

        return agent_hl7_parse_total, agent_hl7_parse_duration_seconds
    except Exception:  # noqa: BLE001
        return None, None


def _import_lab_report_type():
    from extractors.schemas import LabReport  # noqa: WPS433

    return LabReport


def _first_segment(message, name: str):
    for child in getattr(message, "children", []):
        if child.name == name:
            return child
    return None


def parse_hl7(
    raw: bytes,
    *,
    document_reference_id: str,
    patient_id: str,
):
    """Parse an HL7 v2 byte stream into a LabReport or DemographicUpdateEvent.

    Returns:
        LabReport for ORU^R01, DemographicUpdateEvent for ADT^A08.

    Raises:
        ParserMalformedError: on missing MSH/PID or any underlying parse
            failure (the structural minimum for routing).
        ParserUnsupportedError: on message types not in the v1 set
            (ADT^A08, ORU^R01).
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("parse_hl7 expects bytes")

    counter, histogram = _import_metrics()
    started = time.perf_counter()
    message_type_label = "unknown"

    try:
        normalized = normalize_segment_terminators(bytes(raw))
        try:
            text = normalized.decode("utf-8")
        except UnicodeDecodeError:
            text = normalized.decode("utf-8", errors="replace")

        try:
            from hl7apy.parser import parse_message  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover — surfaced as malformed in CI
            raise ParserMalformedError("hl7_library_unavailable") from exc

        try:
            message = parse_message(text, find_groups=False, validation_level=2)
        except Exception as exc:  # noqa: BLE001 — hl7apy raises a wide tree
            raise ParserMalformedError("hl7_parse_failed") from exc

        msh = _first_segment(message, "MSH")
        if msh is None:
            raise ParserMalformedError("hl7_missing_msh")

        try:
            control_id = (msh.msh_10.value or "").strip() or None
        except Exception:  # noqa: BLE001
            control_id = None

        try:
            msg_code = (msh.msh_9.msg_1.value or "").strip()
            trigger = (msh.msh_9.msg_2.value or "").strip()
        except Exception:  # noqa: BLE001
            msg_code = ""
            trigger = ""
        message_type = f"{msg_code}^{trigger}" if msg_code else ""
        message_type_label = message_type or "unknown"

        # PID required for both supported message types.
        pid = _first_segment(message, "PID")
        if pid is None:
            raise ParserMalformedError("hl7_missing_pid", control_id=control_id)

        result: Union[DemographicUpdateEvent, "LabReport"]  # type: ignore[name-defined]

        if message_type == "ADT^A08":
            result = parse_adt_a08(
                message,
                document_reference_id=document_reference_id,
                patient_id=patient_id,
                control_id=control_id or "",
            )
        elif message_type == "ORU^R01":
            result = parse_oru_r01(
                message,
                document_reference_id=document_reference_id,
                patient_id=patient_id,
            )
        else:
            raise ParserUnsupportedError(message_type or "unknown", control_id=control_id)

        duration_ms = (time.perf_counter() - started) * 1000.0
        if counter is not None:
            counter.labels(message_type=message_type_label, outcome="ok").inc()
        if histogram is not None:
            histogram.labels(message_type=message_type_label).observe(
                (time.perf_counter() - started)
            )
        logger.info(
            "hl7_parse_completed",
            extra={
                "message_type": message_type_label,
                "control_id": control_id or "<unparsed>",
                "duration_ms": round(duration_ms, 2),
                "outcome": "ok",
                "document_reference_id": document_reference_id,
            },
        )
        return result
    except (ParserMalformedError, ParserUnsupportedError) as exc:
        outcome = "malformed" if isinstance(exc, ParserMalformedError) else "unsupported"
        if counter is not None:
            counter.labels(message_type=message_type_label, outcome=outcome).inc()
        if histogram is not None:
            histogram.labels(message_type=message_type_label).observe(
                (time.perf_counter() - started)
            )
        logger.warning(
            "hl7_parse_failed",
            extra={
                "message_type": message_type_label,
                "outcome": outcome,
                "code": getattr(exc, "code", None) or getattr(exc, "message_type", None),
                "document_reference_id": document_reference_id,
            },
        )
        raise
