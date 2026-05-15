#!/usr/bin/env python3
"""
Loads synthetic patient data into OpenEMR via REST API + direct MySQL.

OpenEMR's FHIR API is read-only, so we write via:
  - REST API  (/apis/default/api/...)  for patients, encounters, allergies,
                                        conditions, medications
  - Direct MySQL                        for vitals (form_vitals) and labs
                                        (procedure_order/report/result) because
                                        the REST vitals endpoint has an authUserId
                                        bug in this OpenEMR version

Required environment variables:
  BASE_URL        e.g. https://your-railway-app.up.railway.app
  CLIENT_ID       agent-api-loader OAuth2 client ID
  CLIENT_SECRET   agent-api-loader OAuth2 client secret
  OE_USER         OpenEMR admin username  (default: admin)
  OE_PASS         OpenEMR admin password  (default: your-openemr-admin-password)
  MYSQL_HOST      TCP proxy host          (default: shuttle.proxy.rlwy.net)
  MYSQL_PORT      TCP proxy port          (default: 12805)
  MYSQL_USER      MySQL user              (default: root)
  MYSQL_PASS      MySQL password
  MYSQL_DB        database name           (default: openemr)

Usage:
  BASE_URL=https://... CLIENT_ID=... CLIENT_SECRET=... MYSQL_PASS=... python3 load.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    import pymysql
except ImportError:
    print("pymysql not installed. Run: pip3 install pymysql", file=sys.stderr)
    sys.exit(1)

BUNDLES_DIR = pathlib.Path(__file__).parent / "bundles"

SCOPE = (
    "openid api:oemr "
    "user/patient.crus user/encounter.crus user/vital.crus "
    "user/medical_problem.cruds user/medication.cruds user/allergy.cruds "
    "user/Patient.rs user/Encounter.rs user/Observation.rs "
    "user/Condition.rs user/MedicationRequest.rs "
    "user/AllergyIntolerance.rs user/DiagnosticReport.rs"
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_token(base_url: str, client_id: str, client_secret: str,
              oe_user: str, oe_pass: str) -> str:
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": oe_user,
        "password": oe_pass,
        "user_role": "users",
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/oauth2/default/token", data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"No token: {body}")
    return token


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------

def api_post(base_url: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{base_url}/apis/default/api/{path.lstrip('/')}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"error": e.code, "body": body}


def create_patient(base_url: str, token: str, p: dict[str, Any]) -> tuple[int, str]:
    """Returns (pid, puuid)."""
    result = api_post(base_url, token, "patient", {
        "fname": p["fname"],
        "lname": p["lname"],
        "DOB": p["dob"],
        "sex": p["sex"],
        "street": p.get("street", ""),
        "city": p.get("city", "Springfield"),
        "state": p.get("state", "IL"),
        "postal_code": p.get("postal_code", "62701"),
    })
    data = result.get("data", {})
    pid = data.get("pid")
    puuid = data.get("uuid", "")
    if not pid:
        raise RuntimeError(f"Patient creation failed for {p['fname']} {p['lname']}: {result}")
    return int(pid), puuid


def create_encounter(base_url: str, token: str, puuid: str,
                     date: str, reason: str) -> tuple[int, str]:
    """Returns (eid, euuid)."""
    result = api_post(base_url, token, f"patient/{puuid}/encounter", {
        "date": date,
        "onset_date": date,
        "reason": reason,
        "facility_id": 1,
        "pc_catid": 5,
        "class_code": "IMP",
    })
    data = result.get("data", {})
    eid = data.get("eid") or data.get("encounter")
    euuid = data.get("euuid", "")
    if not eid:
        raise RuntimeError(f"Encounter creation failed: {result}")
    return int(eid), euuid


def create_allergy(base_url: str, token: str, puuid: str,
                   title: str, reaction: str = "", allergy_type: str = "allergy") -> None:
    payload: dict[str, Any] = {"title": title, "begdate": "2020-01-01"}
    if reaction:
        payload["reaction"] = reaction
    if allergy_type:
        payload["allergy_type"] = allergy_type
    api_post(base_url, token, f"patient/{puuid}/allergy", payload)


def create_condition(base_url: str, token: str, puuid: str,
                     title: str, icd: str = "") -> None:
    payload: dict[str, Any] = {"title": title, "begdate": "2026-04-27"}
    if icd:
        payload["diagnosis"] = icd
    api_post(base_url, token, f"patient/{puuid}/medical_problem", payload)


def create_medication(base_url: str, token: str, pid: int, title: str, route: str = "Oral") -> None:
    api_post(base_url, token, f"patient/{pid}/medication", {
        "title": title,
        "route": route,
        "unit": "1",
        "form": "Tablet",
    })


# ---------------------------------------------------------------------------
# MySQL helpers (vitals + labs)
# ---------------------------------------------------------------------------

def mysql_conn(cfg: dict[str, str]):
    return pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]),
        user=cfg["user"], password=cfg["password"],
        database=cfg["db"], connect_timeout=10,
        charset="utf8mb4",
    )


# LOINC codes that OpenEMR's FHIR Observation vitals service maps to uuid_mapping
_VITAL_LOINC_CODES = [
    "85353-1",  # Vital signs panel
    "9279-1",   # Respiratory Rate
    "8867-4",   # Heart rate
    "2708-6",   # O2 saturation
    "59408-5",  # Pulse oximetry O2 sat
    "8310-5",   # Body Temperature
    "8327-9",   # Temperature Location
    "8302-2",   # Body Height
    "9843-4",   # Head circumference
    "29463-7",  # Body Weight
    "39156-5",  # BMI
    "85354-9",  # Blood pressure panel
    "8289-1",   # Ped head circumference
    "59576-9",  # Ped BMI
    "77606-2",  # Ped weight-for-height
]


def insert_vitals(conn, pid: int, eid: int, dt: str,
                  bps: int, bpd: int, pulse: int, respiration: int,
                  temperature: float, oxygen_saturation: float,
                  weight: float = 0, height: float = 0) -> None:
    uid = uuid.uuid4().bytes
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO form_vitals
              (uuid, date, pid, user, groupname, authorized, activity,
               bps, bpd, pulse, respiration, temperature, temp_method,
               oxygen_saturation, weight, height)
            VALUES (%s,%s,%s,'admin','Default',1,1,%s,%s,%s,%s,%s,'Oral',%s,%s,%s)
        """, (uid, dt, pid, str(bps), str(bpd), pulse, respiration,
              temperature, oxygen_saturation, weight, height))
        form_vitals_id = cur.lastrowid

        # Register in forms table so OpenEMR's FHIR layer can find these vitals
        cur.execute("""
            INSERT INTO forms
              (date, encounter, form_name, form_id, pid, user, groupname, authorized, deleted, formdir)
            VALUES (%s,%s,'Vitals',%s,%s,'admin','Default',1,0,'vitals')
        """, (dt, eid, form_vitals_id, pid))

        # Register in uuid_registry (required by OpenEMR's UuidRegistry::getRegistryForTable)
        cur.execute("""
            INSERT IGNORE INTO uuid_registry
              (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped)
            VALUES (%s,'form_vitals','id','','',0,0)
        """, (uid,))

        # Create uuid_mapping entries — one per LOINC code — so FHIR Observation queries resolve
        for loinc in _VITAL_LOINC_CODES:
            mapping_uuid = uuid.uuid4().bytes
            resource_path = f"category=vital-signs&code={loinc}"
            cur.execute("""
                INSERT INTO uuid_mapping (uuid, resource, resource_path, `table`, target_uuid)
                VALUES (%s,'Observation',%s,'form_vitals',%s)
            """, (mapping_uuid, resource_path, uid))

    conn.commit()


def insert_lab(conn, pid: int, eid: int, ordered_dt: str, collected_dt: str,
               loinc: str, name: str, value: str, units: str,
               ref_range: str, abnormal: str, status: str = "final") -> None:
    order_uid = uuid.uuid4().bytes
    report_uid = uuid.uuid4().bytes
    result_uid = uuid.uuid4().bytes

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO procedure_order
              (uuid, provider_id, patient_id, encounter_id,
               date_ordered, date_collected, order_status, activity,
               procedure_order_type, order_intent)
            VALUES (%s,1,%s,%s,%s,%s,'complete',1,'laboratory_test','order')
        """, (order_uid, pid, eid, ordered_dt, collected_dt))
        order_id = cur.lastrowid

        # procedure_order_code required so ProcedureService JOIN resolves (procedure_order_seq must match)
        cur.execute("""
            INSERT INTO procedure_order_code
              (procedure_order_id, procedure_order_seq, procedure_code, procedure_name,
               procedure_order_title, do_not_send)
            VALUES (%s,1,%s,%s,%s,0)
        """, (order_id, loinc, name, name))

        cur.execute("""
            INSERT INTO procedure_report
              (uuid, procedure_order_id, procedure_order_seq, date_collected, date_report,
               report_status, review_status)
            VALUES (%s,%s,1,%s,%s,%s,'received')
        """, (report_uid, order_id, collected_dt, collected_dt, status))
        report_id = cur.lastrowid

        cur.execute("""
            INSERT INTO procedure_result
              (uuid, procedure_report_id, result_data_type, result_code,
               result_text, date, units, result, `range`, abnormal, result_status)
            VALUES (%s,%s,'N',%s,%s,%s,%s,%s,%s,%s,%s)
        """, (result_uid, report_id, loinc, name, collected_dt,
              units, value, ref_range, abnormal, status))
    conn.commit()


# ---------------------------------------------------------------------------
# Patient loader functions — one per scenario
# ---------------------------------------------------------------------------

# The static admit dates in the PATIENTS list span 2026-04-26..2026-04-30.
# That window is fine on the day the data was authored, but the Co-Pilot
# census query filters `form_encounter.date >= NOW() - 7 DAY`, so once the
# deploy clock advances past ANCHOR_DATE + 7 days these encounters silently
# fall out of the panel. Map each static date to a rolling date relative to
# today so the panel keeps working without re-seeding.
_ANCHOR_DATE = date(2026, 4, 30)


def rolling_admit_date(static_admit_date: str) -> str:
    """Map a static YYYY-MM-DD admit date to today - (ANCHOR - static) days.

    The most recent static date (ANCHOR) becomes today; earlier static dates
    are spread out as the same number of days earlier than today. Preserves
    the relative ordering and spacing of the original dataset.
    """
    try:
        d = datetime.strptime(static_admit_date, "%Y-%m-%d").date()
    except ValueError:
        return static_admit_date
    offset = (_ANCHOR_DATE - d).days
    if offset < 0:
        offset = 0
    return (date.today() - timedelta(days=offset)).isoformat()


def load_patient(base_url: str, token: str, conn,
                 p_def: dict[str, Any],
                 chen_user_id: int = 0) -> None:
    """Load one patient and all their clinical data."""
    name = f"{p_def['fname']} {p_def['lname']}"
    try:
        pid, puuid = create_patient(base_url, token, p_def)
        print(f"  Patient  pid={pid} puuid={puuid[:8]}...")

        # Encounter — use a rolling date so the encounter never ages out of
        # the 7-day census window.
        admit_date = rolling_admit_date(p_def.get("admit_date", "2026-04-27"))
        reason = p_def.get("admit_reason", "Inpatient admission")
        eid, _ = create_encounter(base_url, token, puuid, admit_date, reason)
        print(f"  Encounter eid={eid}")

        # Assign provider_id in form_encounter so index.php census filter works.
        # "other" patients get provider_id=0 (excluded from Chen's panel).
        # Chen patients get her real user_id so they show on her 15-patient panel.
        target_provider_id = 0 if p_def.get("provider") == "other" else chen_user_id
        # OpenEMR's REST insert and our pymysql connection use separate sessions;
        # MariaDB occasionally raises 1020 ("Record has changed since last read")
        # on the very first UPDATE because the row's generation hasn't propagated
        # to our connection yet. Retry once after a short reconnect.
        for attempt in range(3):
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE form_encounter SET provider_id = %s WHERE encounter = %s",
                        (target_provider_id, eid),
                    )
                conn.commit()
                break
            except pymysql.MySQLError as exc:
                if exc.args and exc.args[0] in (1020, 1213) and attempt < 2:
                    try:
                        conn.rollback()
                    except pymysql.MySQLError:
                        pass
                    time.sleep(0.3)
                    continue
                raise

        # Allergies
        for alg in p_def.get("allergies", []):
            create_allergy(base_url, token, puuid,
                           alg["title"], alg.get("reaction", ""),
                           alg.get("type", "allergy"))

        # Conditions
        for cond in p_def.get("conditions", []):
            create_condition(base_url, token, puuid,
                             cond["title"], cond.get("icd", ""))

        # Medications
        for med in p_def.get("medications", []):
            create_medication(base_url, token, pid, med["title"], med.get("route", "Oral"))

        # Vitals (direct MySQL)
        for v in p_def.get("vitals", []):
            insert_vitals(conn, pid, eid, v["dt"],
                          v.get("bps", 120), v.get("bpd", 80),
                          v.get("pulse", 80), v.get("respiration", 16),
                          v.get("temperature", 37.0),
                          v.get("oxygen_saturation", 98),
                          v.get("weight", 0), v.get("height", 0))

        # Labs (direct MySQL)
        ordered_dt = f"2026-04-29 02:00:00"
        for lab in p_def.get("labs", []):
            insert_lab(conn, pid, eid,
                       ordered_dt, lab["collected_dt"],
                       lab["loinc"], lab["name"],
                       lab["value"], lab["units"],
                       lab.get("range", ""), lab.get("abnormal", "normal"),
                       lab.get("status", "final"))

        print(f"  OK — {len(p_def.get('vitals',[]))} vitals, "
              f"{len(p_def.get('labs',[]))} labs, "
              f"{len(p_def.get('allergies',[]))} allergies, "
              f"{len(p_def.get('conditions',[]))} conditions, "
              f"{len(p_def.get('medications',[]))} meds")

    except Exception as exc:
        print(f"  FAIL: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Patient definitions — 25 patients aligned with synthetic_data/bundles/pt-NNN.json.
# Names, DOBs, sex, providers, conditions, meds, vitals and labs mirror the
# canonical bundle for each pt-NNN so that data planted by load.py classifies
# the same way as the bundle JSON used by the rules-engine tests.
# ---------------------------------------------------------------------------

PATIENTS: list[dict[str, Any]] = [

    # ── pt-001: Marcus Webb — sepsis/pneumonia, qSOFA + critical lactate (P1) ─
    {
        "fname": "Marcus", "lname": "Webb", "dob": "1968-03-14", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Sepsis due to pneumonia",
        "conditions": [{"title": "Sepsis", "icd": "A41.9"},
                       {"title": "Pneumonia", "icd": "J18.9"}],
        "allergies": [],
        "medications": [{"title": "Vancomycin 1g IV"},
                        {"title": "Piperacillin-tazobactam 3.375g IV"},
                        {"title": "Norepinephrine 0.1 mcg/kg/min IV"}],
        "vitals": [
            {"dt": "2026-04-29 05:15:00", "bps": 88, "bpd": 54,
             "pulse": 118, "respiration": 26, "temperature": 38.9,
             "oxygen_saturation": 88, "weight": 82, "height": 175},
        ],
        "labs": [
            {"loinc": "2518-9", "name": "Lactate", "collected_dt": "2026-04-29 04:30:00",
             "value": "4.2", "units": "mmol/L", "range": "0.5-2.2", "abnormal": "critical",
             "status": "final"},
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 03:45:00",
             "value": "18.4", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "9269-2", "name": "Glasgow coma score total", "collected_dt": "2026-04-29 05:15:00",
             "value": "12", "units": "{score}", "range": "13-15", "abnormal": "low",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:15:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-002: Delia Fontaine — AKI on CKD, critical K+ 6.4 (P3) ────────
    {
        "fname": "Delia", "lname": "Fontaine", "dob": "1952-11-07", "sex": "Female",
        "admit_date": "2026-04-30", "admit_reason": "Acute kidney injury on CKD stage 4",
        "conditions": [{"title": "Acute kidney injury", "icd": "N17.9"},
                       {"title": "Chronic kidney disease stage 4", "icd": "N18.4"}],
        "allergies": [],
        "medications": [{"title": "Sodium bicarbonate 8.4% IV"},
                        {"title": "Aspirin 81mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 148, "bpd": 88,
             "pulse": 74, "respiration": 16, "temperature": 37.1,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "6298-4", "name": "Potassium", "collected_dt": "2026-04-29 03:12:00",
             "value": "6.4", "units": "mmol/L", "range": "3.5-5.1", "abnormal": "critical",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:12:00",
             "value": "4.8", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "3094-0", "name": "Urea nitrogen", "collected_dt": "2026-04-29 03:12:00",
             "value": "62", "units": "mg/dL", "range": "7-20", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:30:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-003: Raymond Okafor — COPD exacerbation, SpO2 91% (P4) ────────
    {
        "fname": "Raymond", "lname": "Okafor", "dob": "1945-06-22", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "COPD exacerbation",
        "conditions": [{"title": "COPD exacerbation", "icd": "J44.1"}],
        "allergies": [],
        "medications": [{"title": "Ipratropium bromide inhaler"},
                        {"title": "Methylprednisolone 125mg IV"}],
        "vitals": [
            {"dt": "2026-04-29 05:00:00", "bps": 132, "bpd": 78,
             "pulse": 88, "respiration": 20, "temperature": 37.4,
             "oxygen_saturation": 91},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 04:00:00",
             "value": "11.2", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
        ],
        # No code-status observation — bundle pt-003 omits 81638-3.
    },

    # ── pt-004: Gloria Tran — acute decompensated heart failure (P8) ─────
    {
        "provider": "other",
        "fname": "Gloria", "lname": "Tran", "dob": "1958-09-03", "sex": "Female",
        "admit_date": "2026-04-30", "admit_reason": "Acute decompensated heart failure",
        "conditions": [{"title": "Heart failure", "icd": "I50.9"}],
        "allergies": [{"title": "Sulfonamide", "reaction": "", "type": "allergy"}],
        "medications": [{"title": "Furosemide 80mg IV"},
                        {"title": "Lisinopril 10mg PO"}],
        "vitals": [
            {"dt": "2026-04-29 05:45:00", "bps": 156, "bpd": 94,
             "pulse": 96, "respiration": 18, "temperature": 37.0,
             "oxygen_saturation": 94},
        ],
        "labs": [
            {"loinc": "42637-9", "name": "BNP", "collected_dt": "2026-04-29 03:30:00",
             "value": "1840", "units": "pg/mL", "range": "0-100", "abnormal": "high",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:30:00",
             "value": "1.4", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:45:00",
             "value": "DNR/DNI", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-005: Bernard Kowalski — community-acquired pneumonia (P8) ─────
    {
        "provider": "other",
        "fname": "Bernard", "lname": "Kowalski", "dob": "1973-02-18", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Community-acquired pneumonia",
        "conditions": [{"title": "Pneumonia", "icd": "J18.9"}],
        "allergies": [{"title": "Penicillin", "reaction": "Anaphylaxis", "type": "allergy"}],
        "medications": [{"title": "Amoxicillin 875mg PO q12h"},
                        {"title": "Azithromycin 500mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:00:00", "bps": 122, "bpd": 76,
             "pulse": 90, "respiration": 18, "temperature": 38.2,
             "oxygen_saturation": 95},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 02:30:00",
             "value": "14.6", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-006: Ingrid Nakamura — suspected pulmonary embolism (P8) ──────
    {
        "provider": "other",
        "fname": "Ingrid", "lname": "Nakamura", "dob": "1965-07-29", "sex": "Female",
        "admit_date": "2026-04-29", "admit_reason": "Suspected pulmonary embolism",
        "conditions": [{"title": "Pulmonary embolism", "icd": "I26.99"}],
        "allergies": [],
        "medications": [{"title": "Heparin 5000 units IV bolus"},
                        {"title": "Enoxaparin 1mg/kg SC q12h"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 72,
             "pulse": 102, "respiration": 19, "temperature": 37.2,
             "oxygen_saturation": 93},
        ],
        "labs": [
            {"loinc": "48066-5", "name": "D-dimer", "collected_dt": "2026-04-29 01:00:00",
             "value": "3.2", "units": "mg/L FEU", "range": "0.0-0.5", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-007: Darnell Simmons — hypertensive urgency with AKI (P8) ─────
    {
        "provider": "other",
        "fname": "Darnell", "lname": "Simmons", "dob": "1980-04-11", "sex": "Male",
        "admit_date": "2026-04-30", "admit_reason": "Hypertensive urgency with AKI",
        "conditions": [{"title": "Hypertensive disorder", "icd": "I10"},
                       {"title": "Acute kidney injury", "icd": "N17.9"}],
        "allergies": [],
        "medications": [{"title": "Labetalol 200mg PO q8h"},
                        {"title": "Lisinopril 5mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 192, "bpd": 114,
             "pulse": 82, "respiration": 15, "temperature": 37.0,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 02:00:00",
             "value": "2.9", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "6298-4", "name": "Potassium", "collected_dt": "2026-04-29 02:00:00",
             "value": "5.2", "units": "mmol/L", "range": "3.5-5.1", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:30:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-008: Yvonne Castillo — diabetic nephropathy monitoring (P8) ───
    {
        "fname": "Yvonne", "lname": "Castillo", "dob": "1960-12-30", "sex": "Female",
        "admit_date": "2026-04-30", "admit_reason": "Diabetic nephropathy — monitoring",
        "conditions": [{"title": "Diabetic nephropathy", "icd": "E11.21"}],
        "allergies": [],
        "medications": [{"title": "Metformin 500mg PO BID"},
                        {"title": "Lisinopril 10mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 04:45:00", "bps": 138, "bpd": 82,
             "pulse": 76, "respiration": 14, "temperature": 37.0,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2160-0", "name": "Creatinine (POC)", "collected_dt": "2026-04-29 03:00:00",
             "value": "1.2", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine (Lab)", "collected_dt": "2026-04-29 03:00:00",
             "value": "2.1", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 03:00:00",
             "value": "182", "units": "mg/dL", "range": "70-100", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 04:45:00",
             "value": "DNR/DNI", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-009: Elena Morales — uncomplicated UTI (P8) ───────────────────
    {
        "provider": "other",
        "fname": "Elena", "lname": "Morales", "dob": "1955-08-17", "sex": "Female",
        "admit_date": "2026-05-01", "admit_reason": "Urinary tract infection",
        "conditions": [{"title": "Urinary tract infection", "icd": "N39.0"}],
        "allergies": [],
        "medications": [{"title": "Trimethoprim-sulfamethoxazole 160/800mg PO BID"},
                        {"title": "Aspirin 81mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 132, "bpd": 63,
             "pulse": 70, "respiration": 13, "temperature": 37.5,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 04:00:00",
             "value": "13.2", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "DNR/DNI", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-010: Rajiv Patel — cellulitis left lower extremity (P8) ───────
    {
        "provider": "other",
        "fname": "Rajiv", "lname": "Patel", "dob": "1948-03-24", "sex": "Male",
        "admit_date": "2026-04-30", "admit_reason": "Cellulitis left lower extremity",
        "conditions": [{"title": "Cellulitis left lower extremity", "icd": "L03.116"}],
        "allergies": [],
        "medications": [{"title": "Cephalexin 500mg PO QID"},
                        {"title": "Lisinopril 5mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 111, "bpd": 73,
             "pulse": 61, "respiration": 13, "temperature": 37.2,
             "oxygen_saturation": 95},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 04:00:00",
             "value": "14.8", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-011: Karl Bergstrom — upper GI bleed peptic ulcer (P8) ────────
    {
        "provider": "other",
        "fname": "Karl", "lname": "Bergstrom", "dob": "1971-11-02", "sex": "Male",
        "admit_date": "2026-04-30", "admit_reason": "Upper GI bleed — peptic ulcer disease",
        "conditions": [{"title": "Upper GI bleed — peptic ulcer disease", "icd": "K92.2"}],
        "allergies": [],
        "medications": [{"title": "Pantoprazole 40mg IV q12h"},
                        {"title": "Ondansetron 4mg IV q8h PRN"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 134, "bpd": 80,
             "pulse": 76, "respiration": 15, "temperature": 36.9,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "718-7", "name": "Hemoglobin", "collected_dt": "2026-04-29 02:00:00",
             "value": "8.4", "units": "g/dL", "range": "13.5-17.5", "abnormal": "low",
             "status": "final"},
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 02:00:00",
             "value": "98", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:30:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-012: Miriam Johnson — chronic essential hypertension (P9) ─────
    {
        "fname": "Miriam", "lname": "Johnson", "dob": "1939-05-15", "sex": "Female",
        "admit_date": "2026-04-29", "admit_reason": "Essential hypertension — chronic management",
        "conditions": [{"title": "Essential hypertension — chronic management", "icd": "I10"}],
        "allergies": [],
        "medications": [{"title": "Aspirin 325mg PO daily"},
                        {"title": "Atorvastatin 40mg PO daily"},
                        {"title": "Lisinopril 10mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:00:00", "bps": 134, "bpd": 71,
             "pulse": 66, "respiration": 18, "temperature": 36.9,
             "oxygen_saturation": 95},
        ],
        "labs": [
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:00:00",
             "value": "1.0", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-013: Carlos Reyes — diabetic ketoacidosis (P8) ────────────────
    {
        "provider": "other",
        "fname": "Carlos", "lname": "Reyes", "dob": "1977-09-09", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Diabetic ketoacidosis",
        "conditions": [{"title": "Diabetic ketoacidosis", "icd": "E10.10"}],
        "allergies": [],
        "medications": [{"title": "Insulin glargine 20 units SC daily"},
                        {"title": "Metformin 500mg PO BID"},
                        {"title": "Sodium chloride 0.9% IV 125 mL/hr"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 121, "bpd": 71,
             "pulse": 70, "respiration": 12, "temperature": 37.1,
             "oxygen_saturation": 95},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 05:00:00",
             "value": "420", "units": "mg/dL", "range": "70-100", "abnormal": "critical",
             "status": "final"},
            {"loinc": "6298-4", "name": "Potassium", "collected_dt": "2026-04-29 05:00:00",
             "value": "3.3", "units": "mmol/L", "range": "3.5-5.1", "abnormal": "low",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-014: Abena Osei — acute pancreatitis (P8) ─────────────────────
    {
        "provider": "other",
        "fname": "Abena", "lname": "Osei", "dob": "1962-01-28", "sex": "Female",
        "admit_date": "2026-04-30", "admit_reason": "Acute pancreatitis — alcohol related",
        "conditions": [{"title": "Acute pancreatitis — alcohol related", "icd": "K85.20"}],
        "allergies": [],
        "medications": [{"title": "Pantoprazole 40mg IV daily"},
                        {"title": "Ondansetron 4mg IV q6h PRN"},
                        {"title": "Morphine 2mg IV q4h PRN"}],
        "vitals": [
            {"dt": "2026-04-29 05:45:00", "bps": 111, "bpd": 70,
             "pulse": 84, "respiration": 17, "temperature": 37.0,
             "oxygen_saturation": 96},
        ],
        "labs": [
            {"loinc": "1742-7", "name": "ALT", "collected_dt": "2026-04-29 03:00:00",
             "value": "88", "units": "U/L", "range": "7-56", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:45:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-015: Dorothy Williams — stable systolic heart failure (P8) ────
    {
        "provider": "other",
        "fname": "Dorothy", "lname": "Williams", "dob": "1944-07-04", "sex": "Female",
        "admit_date": "2026-04-29", "admit_reason": "Stable systolic heart failure — diuresis",
        "conditions": [{"title": "Stable systolic heart failure — diuresis", "icd": "I50.20"}],
        "allergies": [],
        "medications": [{"title": "Furosemide 40mg PO daily"},
                        {"title": "Lisinopril 5mg PO daily"},
                        {"title": "Carvedilol 6.25mg PO BID"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 109, "bpd": 68,
             "pulse": 71, "respiration": 12, "temperature": 36.8,
             "oxygen_saturation": 95},
        ],
        "labs": [
            {"loinc": "42637-9", "name": "BNP", "collected_dt": "2026-04-29 01:00:00",
             "value": "560", "units": "pg/mL", "range": "0-100", "abnormal": "high",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-016: Wei Huang — post-op laparoscopic cholecystectomy (P11) ───
    {
        "provider": "other",
        "fname": "Wei", "lname": "Huang", "dob": "1983-10-20", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Post-operative day 1 — laparoscopic cholecystectomy",
        "conditions": [{"title": "Post-operative day 1 — laparoscopic cholecystectomy", "icd": "Z48.815"}],
        "allergies": [],
        "medications": [{"title": "Ketorolac 15mg IV q6h"},
                        {"title": "Ondansetron 4mg IV q8h PRN"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 125, "bpd": 84,
             "pulse": 66, "respiration": 14, "temperature": 37.4,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 04:00:00",
             "value": "10.4", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "DNR/DNI", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-017: Sean Murphy — alcohol withdrawal CIWA protocol (P11) ─────
    {
        "provider": "other",
        "fname": "Sean", "lname": "Murphy", "dob": "1957-03-13", "sex": "Male",
        "admit_date": "2026-04-30", "admit_reason": "Alcohol withdrawal — CIWA protocol",
        "conditions": [{"title": "Alcohol withdrawal — CIWA protocol", "icd": "F10.239"}],
        "allergies": [],
        "medications": [{"title": "Lorazepam 2mg IV q1h PRN CIWA>8"},
                        {"title": "Thiamine 100mg IV daily"},
                        {"title": "Folate 1mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 132, "bpd": 78,
             "pulse": 67, "respiration": 16, "temperature": 36.7,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 02:00:00",
             "value": "112", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 05:30:00",
             "value": "DNR/DNI", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-018: Thomas Greer — atrial fibrillation with RVR (P5) ─────────
    {
        "fname": "Thomas", "lname": "Greer", "dob": "1950-06-01", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Atrial fibrillation with RVR",
        "conditions": [{"title": "Atrial fibrillation", "icd": "I48.91"}],
        "allergies": [],
        "medications": [{"title": "Metoprolol succinate 25mg PO daily"},
                        {"title": "Apixaban 5mg PO BID"}],
        "vitals": [
            {"dt": "2026-04-29 06:30:00", "bps": 128, "bpd": 80,
             "pulse": 136, "respiration": 16, "temperature": 37.1,
             "oxygen_saturation": 96},
        ],
        "labs": [
            {"loinc": "3016-3", "name": "TSH", "collected_dt": "2026-04-29 05:00:00",
             "value": "0.8", "units": "mIU/L", "range": "0.4-4.0", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:30:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-019: Linda Okonkwo — minor fall, blank code status (P10) ──────
    # Bundle pt-019 omits the 81638-3 Observation, so blank_code_status fires.
    {
        "fname": "Linda", "lname": "Okonkwo", "dob": "1966-08-30", "sex": "Female",
        "admit_date": "2026-05-01", "admit_reason": "Observation after minor fall — no fracture identified",
        "conditions": [],
        "allergies": [],
        "medications": [{"title": "Acetaminophen 650mg PO q6h PRN"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 124, "bpd": 76,
             "pulse": 72, "respiration": 15, "temperature": 37.0,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-28 20:00:00",
             "value": "98", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-28 20:00:00",
             "value": "0.9", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-020: Robert Finch — pre-procedure observation, code status documented (P11) ────
    {
        "provider": "other",
        "fname": "Robert", "lname": "Finch", "dob": "1959-03-17", "sex": "Male",
        "admit_date": "2026-05-02", "admit_reason": "Pre-procedure observation — elective colonoscopy prep",
        "conditions": [],
        "allergies": [],
        "medications": [{"title": "Polyethylene glycol 3350 solution PO"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 74,
             "pulse": 68, "respiration": 14, "temperature": 36.8,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 04:00:00",
             "value": "94", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 04:00:00",
             "value": "0.8", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-021: Priya Anand — suspected sepsis, qSOFA=2 no critical lab (P2) ─
    {
        "fname": "Priya", "lname": "Anand", "dob": "1975-04-12", "sex": "Female",
        "admit_date": "2026-05-02", "admit_reason": "Suspected sepsis — source under investigation",
        "conditions": [{"title": "Septicemia", "icd": "A41.9"}],
        "allergies": [],
        "medications": [{"title": "Vancomycin 1g IV"},
                        {"title": "Piperacillin-tazobactam 3.375g IV"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 96, "bpd": 62,
             "pulse": 108, "respiration": 24, "temperature": 38.6,
             "oxygen_saturation": 94},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 04:00:00",
             "value": "14.2", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "2518-9", "name": "Lactate", "collected_dt": "2026-04-29 04:00:00",
             "value": "1.8", "units": "mmol/L", "range": "0.5-2.0", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-022: James Whitfield — acute delirium, GCS 14 (P6) ────────────
    {
        "fname": "James", "lname": "Whitfield", "dob": "1942-10-05", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Acute delirium — hyperactive type",
        "conditions": [{"title": "Delirium", "icd": "F05"}],
        "allergies": [],
        "medications": [{"title": "Haloperidol 0.5mg IV PRN"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 128, "bpd": 74,
             "pulse": 88, "respiration": 17, "temperature": 37.3,
             "oxygen_saturation": 96},
        ],
        "labs": [
            {"loinc": "2823-3", "name": "Potassium", "collected_dt": "2026-04-29 03:00:00",
             "value": "4.0", "units": "mmol/L", "range": "3.5-5.1", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:00:00",
             "value": "1.1", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            {"loinc": "9269-2", "name": "Glasgow coma score total", "collected_dt": "2026-04-29 06:00:00",
             "value": "14", "units": "{score}", "range": "13-15", "abnormal": "low",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-023: Keisha Balogun — sickle cell crisis, pain 9/10 (P7) ──────
    {
        "provider": "other",
        "fname": "Keisha", "lname": "Balogun", "dob": "1988-07-19", "sex": "Female",
        "admit_date": "2026-05-01", "admit_reason": "Sickle cell disease with acute vaso-occlusive crisis",
        "conditions": [{"title": "Sickle cell crisis", "icd": "D57.00"}],
        "allergies": [],
        "medications": [{"title": "Morphine 4mg IV q3h PRN pain"},
                        {"title": "Ketorolac 15mg IV q6h"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 72,
             "pulse": 98, "respiration": 18, "temperature": 37.5,
             "oxygen_saturation": 96},
        ],
        "labs": [
            {"loinc": "718-7", "name": "Hemoglobin", "collected_dt": "2026-04-29 02:00:00",
             "value": "8.2", "units": "g/dL", "range": "12.0-16.0", "abnormal": "low",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 02:00:00",
             "value": "0.8", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            {"loinc": "72514-3", "name": "Pain severity 0-10", "collected_dt": "2026-04-29 06:00:00",
             "value": "9", "units": "{score}", "range": "0-3", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-024: Alejandro Cruz — intra-abdominal sepsis (P1) ─────────────
    {
        "provider": "other",
        "fname": "Alejandro", "lname": "Cruz", "dob": "1971-05-20", "sex": "Male",
        "admit_date": "2026-05-01", "admit_reason": "Intra-abdominal sepsis",
        "conditions": [{"title": "Intra-abdominal sepsis", "icd": "A41.9"},
                       {"title": "Secondary peritonitis", "icd": "K65.1"}],
        "allergies": [],
        "medications": [{"title": "Meropenem 1g IV q8h"},
                        {"title": "Metronidazole 500mg IV q8h"},
                        {"title": "Norepinephrine 0.05 mcg/kg/min"}],
        "vitals": [
            {"dt": "2026-04-30 04:15:00", "bps": 94, "bpd": 58,
             "pulse": 114, "respiration": 26, "temperature": 38.7,
             "oxygen_saturation": 93},
        ],
        "labs": [
            {"loinc": "2518-9", "name": "Lactate", "collected_dt": "2026-04-30 03:00:00",
             "value": "5.1", "units": "mmol/L", "range": "0.5-2.2", "abnormal": "critical",
             "status": "final"},
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-30 03:00:00",
             "value": "24.6", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-30 03:00:00",
             "value": "2.1", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "9269-2", "name": "Glasgow coma score total", "collected_dt": "2026-04-30 04:15:00",
             "value": "13", "units": "{score}", "range": "13-15", "abnormal": "low",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-30 04:15:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-025: Maya Lindgren — back pain 8/10 with chronic HTN (P7) ─────
    {
        "fname": "Maya", "lname": "Lindgren", "dob": "1972-02-09", "sex": "Female",
        "admit_date": "2026-05-01", "admit_reason": "Acute musculoskeletal back pain with chronic hypertension",
        "conditions": [{"title": "Essential hypertension", "icd": "I10"}],
        "allergies": [],
        "medications": [{"title": "Lisinopril 10mg PO daily"},
                        {"title": "Ketorolac 15mg IV q6h PRN pain"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 134, "bpd": 82,
             "pulse": 92, "respiration": 18, "temperature": 37.0,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 04:00:00",
             "value": "96", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 04:00:00",
             "value": "0.9", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            {"loinc": "72514-3", "name": "Pain severity 0-10", "collected_dt": "2026-04-29 06:00:00",
             "value": "8", "units": "{score}", "range": "0-3", "abnormal": "normal",
             "status": "final"},
            {"loinc": "81638-3", "name": "Code status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Fixed seed keeps any future randomized fields deterministic across runs.
    random.seed(42)

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    client_id = os.environ.get("CLIENT_ID", "")
    client_secret = os.environ.get("CLIENT_SECRET", "")
    oe_user = os.environ.get("OE_USER", "admin")
    oe_pass = os.environ.get("OE_PASS", "your-openemr-admin-password")
    mysql_host = os.environ.get("MYSQL_HOST", "shuttle.proxy.rlwy.net")
    mysql_port = int(os.environ.get("MYSQL_PORT", "12805"))
    mysql_user = os.environ.get("MYSQL_USER", "root")
    mysql_pass = os.environ.get("MYSQL_PASS", "")
    mysql_db = os.environ.get("MYSQL_DB", "openemr")

    missing = [k for k, v in {
        "BASE_URL": base_url, "CLIENT_ID": client_id,
        "CLIENT_SECRET": client_secret, "MYSQL_PASS": mysql_pass,
    }.items() if not v]
    if missing:
        print(f"Missing: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"Authenticating to {base_url} ...")
    token = get_token(base_url, client_id, client_secret, oe_user, oe_pass)
    print("Token obtained.")

    print("Connecting to MySQL ...")
    conn = mysql_conn({
        "host": mysql_host, "port": mysql_port,
        "user": mysql_user, "password": mysql_pass, "db": mysql_db,
    })
    print("MySQL connected.\n")

    # Look up the integer user_id for the authenticated user (Sara Chen).
    # This is written into form_encounter.provider_id so index.php's census
    # filter returns her panel and no others.
    # Provider lookup is distinct from the OAuth principal: the seeder may auth
    # as admin (broad API privileges) while still attributing encounters to the
    # clinician whose panel the demo expects (Sara Chen). PROVIDER_USER lets the
    # caller override this; default to "sara" with a fallback to the auth user.
    provider_username = os.environ.get("PROVIDER_USER", "sara")
    chen_user_id = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s LIMIT 1", (provider_username,))
        row = cur.fetchone()
        if not row and provider_username != oe_user:
            cur.execute("SELECT id FROM users WHERE username = %s LIMIT 1", (oe_user,))
            row = cur.fetchone()
        if row:
            chen_user_id = int(row[0])
    print(f"Provider user_id for '{provider_username}': {chen_user_id}\n")

    ok = 0
    total = len(PATIENTS)
    for i, p_def in enumerate(PATIENTS, 1):
        print(f"[{i:02d}/{total}] {p_def['fname']} {p_def['lname']} ...")
        try:
            load_patient(base_url, token, conn, p_def, chen_user_id=chen_user_id)
            ok += 1
        except Exception as exc:
            print(f"  FAIL: {exc}", file=sys.stderr)
        time.sleep(0.3)  # avoid rate-limiting

    # Sara's persistent panel guarantees: pids that must always appear on her
    # morning census, regardless of whether they came from PATIENTS or were
    # created later via the document-ingest workflow. For each pid we ensure
    # (a) the patient's pubpid equals the numeric pid so the agent-api FHIR
    # resolver (Patient?identifier=<pid>) can find them, and (b) at least one
    # active form_encounter row attributes the visit to Sara. Missing pids
    # are skipped silently — they may not exist in this DB yet.
    _ensure_persistent_sara_panel(conn, chen_user_id, [5, 13, 26, 27])

    conn.close()
    print(f"\nDone: {ok}/{total} patients loaded.")
    if ok < total:
        print("Re-run load.py to retry failed patients.", file=sys.stderr)


def _ensure_persistent_sara_panel(conn: Any, sara_user_id: int, pids: list[int]) -> None:
    """Pin specific pids to Sara's panel so they survive a re-seed.

    Idempotent: runs UPDATE/INSERT against current DB state, never DELETEs.
    For each pid:
      - Aligns ``pubpid`` with the numeric pid so the FHIR identifier resolver
        (Patient?identifier=<pid>) finds the row.
      - Ensures at least one active ``form_encounter`` row exists with
        ``provider_id`` set to Sara's user id, so the panel-discovery query
        attributes the visit to her.
    """
    if sara_user_id <= 0 or not pids:
        return
    print(f"\nPinning persistent panel pids to Sara (user_id={sara_user_id}): {pids}")
    with conn.cursor() as cur:
        for pid in pids:
            cur.execute("SELECT pid FROM patient_data WHERE pid = %s", (pid,))
            if cur.fetchone() is None:
                print(f"  pid={pid}: not in patient_data, skipping")
                continue
            # Align pubpid with pid so FHIR identifier search resolves.
            cur.execute(
                "UPDATE patient_data SET pubpid = %s WHERE pid = %s AND pubpid <> %s",
                (str(pid), pid, str(pid)),
            )
            # Ensure at least one form_encounter row attributes a visit to Sara.
            # Refresh the date to NOW() on every seed run so the index.php
            # 30-day panel-discovery filter does not silently age the panel out.
            cur.execute(
                """
                SELECT encounter
                  FROM form_encounter
                 WHERE pid = %s AND provider_id = %s
                 ORDER BY date DESC
                 LIMIT 1
                """,
                (pid, sara_user_id),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO form_encounter
                        (pid, provider_id, date, reason, facility_id, sensitivity)
                    VALUES (%s, %s, NOW(), %s, 3, '')
                    """,
                    (pid, sara_user_id, "Sara persistent panel"),
                )
                print(f"  pid={pid}: pinned (inserted)")
            else:
                cur.execute(
                    """
                    UPDATE form_encounter
                       SET date = NOW(), date_end = NULL
                     WHERE encounter = %s
                    """,
                    (existing[0],),
                )
                print(f"  pid={pid}: pinned (refreshed encounter={existing[0]})")
    conn.commit()


if __name__ == "__main__":
    main()
