/**
 * Phase-3 Documents tab — structured field editors.
 *
 * Each component is a card that renders the input fields appropriate to a
 * single pending-extraction row's resource type, prepopulated from the row
 * payload. Components emit a fully-merged override_payload (NOT a diff)
 * whenever an input changes; the parent caches it per-row and forwards it
 * to ``approveOne(baseUrl, id, override_payload)`` on Approve.
 *
 * Format scope (locked decisions, see Phase 3 brief):
 *   - PDF / PNG / DOCX  → these editors
 *   - HL7 / XLSX / TIFF → continue to launch ApprovalModal (out of scope)
 *
 * v1 cuts:
 *   - No "Source →" deep-link past the page index — the viewer (left rail)
 *     will resolve a citation chip click via onCitationClick. We pass the
 *     row-payload's first derivedFrom reference where present so the
 *     viewer can scroll to the right artifact; otherwise the button is
 *     a no-op affordance and is dimmed.
 *   - Reject reason is collected via window.prompt to match
 *     ApprovalModal.onBulkReject's pattern (line 200) — keyboard-first
 *     and avoids a second per-row input.
 */

import { useEffect, useState, type ReactElement } from 'react';
import type { PendingExtractionRow } from '../api';
import { BRAND, NEU, RED, SURFACE } from '../styles/tokens';

export interface FieldEditorProps {
  row: PendingExtractionRow;
  /** Disable inputs / buttons while a sibling approve/reject is in flight. */
  busy: boolean;
  /** Per-row override cache. ``null`` clears the cache and falls back to the
   * server-side payload at approve time. The override is the FULL payload
   * with the operator's edits applied — not a diff. */
  onValueChange: (rowId: number, override: Record<string, unknown> | null) => void;
  /** Notify the parent that the operator clicked Approve / Reject for this
   * row. Reason is collected via window.prompt before this fires for reject. */
  onApprove: (rowId: number) => void;
  onReject: (rowId: number, reason: string) => void;
  /** Optional viewer hook — fired when the operator clicks "Source →".
   * The id is whatever locator the row payload supplies (e.g. citation
   * field_id or derivedFrom reference); the page is best-effort. */
  onCitationClick?: (citationFieldId: string, page: string | null) => void;
  /** Terminal status from the parent's row state machine. */
  status: 'idle' | 'busy' | 'done' | 'error';
  /** Status detail copy (e.g. error message, "approved", "rejected"). */
  statusMessage?: string | null;
}

// ────────────────────────────────────────────────────────────────────────────
// Shared visual primitives
// ────────────────────────────────────────────────────────────────────────────

const cardStyle: React.CSSProperties = {
  background: SURFACE.bg,
  border: `1px solid ${SURFACE.border}`,
  borderRadius: 8,
  padding: '15px 16px',
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  boxShadow: '0 1px 2px rgba(15, 23, 42, 0.04)',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  flexWrap: 'wrap',
};

const kindLabelStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: BRAND.base,
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  background: BRAND.tint,
  padding: '3px 8px',
  borderRadius: 4,
};

const idChipStyle: React.CSSProperties = {
  fontSize: 12,
  color: SURFACE.muted,
};

const sourceBtnStyle = (enabled: boolean): React.CSSProperties => ({
  fontSize: 12,
  fontFamily: 'inherit',
  fontWeight: 500,
  padding: '5px 10px',
  background: '#fff',
  color: BRAND.base,
  border: `1px solid ${BRAND.base}`,
  borderRadius: 5,
  cursor: enabled ? 'pointer' : 'not-allowed',
  opacity: enabled ? 1 : 0.45,
  marginLeft: 'auto',
});

const fieldRow: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 5,
};

const labelStyle: React.CSSProperties = {
  fontSize: 12,
  color: SURFACE.muted,
  fontWeight: 500,
  letterSpacing: '0.01em',
};

const inputStyle: React.CSSProperties = {
  fontSize: 13,
  padding: '9px 10px',
  border: `1px solid ${NEU.border}`,
  borderRadius: 6,
  fontFamily: 'inherit',
  background: SURFACE.bg,
  color: SURFACE.fg,
  width: '100%',
  boxSizing: 'border-box',
  minHeight: 36,
  lineHeight: 1.3,
};

const footerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  borderTop: `1px solid ${SURFACE.border}`,
  paddingTop: 10,
};

const approveBtnStyle = (disabled: boolean): React.CSSProperties => ({
  fontSize: 13,
  fontFamily: 'inherit',
  fontWeight: 600,
  padding: '7px 16px',
  background: BRAND.base,
  color: BRAND.onBrand,
  border: `1px solid ${BRAND.base}`,
  borderRadius: 6,
  cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.5 : 1,
  minHeight: 32,
  transition: 'filter 120ms ease-out',
});

const rejectBtnStyle = (disabled: boolean): React.CSSProperties => ({
  fontSize: 13,
  fontFamily: 'inherit',
  padding: '7px 14px',
  background: '#fff',
  color: RED.text,
  border: `1px solid ${RED.border}`,
  borderRadius: 6,
  cursor: disabled ? 'not-allowed' : 'pointer',
  opacity: disabled ? 0.5 : 1,
  minHeight: 32,
  transition: 'background-color 120ms ease-out',
});

const statusPillStyle = (status: FieldEditorProps['status']): React.CSSProperties => {
  if (status === 'done') {
    return {
      fontSize: 12,
      color: '#166534',
      fontWeight: 600,
      background: '#dcfce7',
      padding: '3px 8px',
      borderRadius: 12,
      border: '1px solid #bbf7d0',
    };
  }
  if (status === 'error') {
    return {
      fontSize: 12,
      color: RED.text,
      fontWeight: 600,
      background: RED.bg,
      padding: '3px 8px',
      borderRadius: 12,
      border: `1px solid ${RED.border}`,
    };
  }
  if (status === 'busy') return { fontSize: 12, color: SURFACE.muted, fontStyle: 'italic' };
  return { display: 'none' };
};

function _statusGlyph(status: FieldEditorProps['status'], decisionMessage?: string | null): string {
  if (status === 'done') {
    if (decisionMessage === 'rejected') return '✗ rejected';
    return '✓ approved';
  }
  if (status === 'error') return '✗ error';
  return decisionMessage ?? '';
}

// ────────────────────────────────────────────────────────────────────────────
// Helpers — read potentially-missing nested fields from FHIR-shaped payloads
// ────────────────────────────────────────────────────────────────────────────

function _str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

function _num(v: unknown): number | '' {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && !Number.isNaN(Number(v))) return Number(v);
  return '';
}

function _obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function _arr(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

/** Pull a citable source identifier from a row payload. Best-effort across
 * Observation (derivedFrom[0].reference) and IntakeFormField (citations[0]). */
function _firstSourceId(payload: Record<string, unknown>): string | null {
  const derived = _arr(payload.derivedFrom)[0];
  if (derived && typeof derived === 'object') {
    const ref = (derived as Record<string, unknown>).reference;
    if (typeof ref === 'string' && ref.length > 0) return ref;
  }
  const value = _obj(payload.value);
  const citations = _arr(value.citations ?? payload.citations);
  const c0 = citations[0];
  if (c0 && typeof c0 === 'object') {
    const fid = (c0 as Record<string, unknown>).field_id
      ?? (c0 as Record<string, unknown>).field_or_chunk_id;
    if (typeof fid === 'string' && fid.length > 0) return fid;
  }
  return null;
}

// ────────────────────────────────────────────────────────────────────────────
// Card scaffolding shared by every editor
// ────────────────────────────────────────────────────────────────────────────

interface CardScaffoldProps {
  kindLabel: string;
  row: PendingExtractionRow;
  status: FieldEditorProps['status'];
  statusMessage?: string | null;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  onCitationClick?: () => void;
  hasSource: boolean;
  children: React.ReactNode;
}

function CardScaffold(p: CardScaffoldProps): ReactElement {
  const terminal = p.status === 'done';
  const disabled = p.busy || p.status === 'busy' || terminal;
  return (
    <div style={cardStyle}>
      <div style={headerStyle}>
        <span style={kindLabelStyle}>{p.kindLabel}</span>
        <span style={idChipStyle}>#{p.row.id}</span>
        <button
          type="button"
          onClick={p.onCitationClick}
          disabled={!p.hasSource || !p.onCitationClick}
          style={sourceBtnStyle(Boolean(p.hasSource && p.onCitationClick))}
          aria-label="Open source artifact"
          title={p.hasSource ? 'Show source' : 'No source available'}
        >
          Source →
        </button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>{p.children}</div>
      <div style={footerStyle}>
        <span style={statusPillStyle(p.status)}>
          {p.status === 'done' || p.status === 'error'
            ? _statusGlyph(p.status, p.statusMessage)
            : (p.statusMessage ?? p.status)}
        </span>
        {!terminal && (
          <>
            <button
              type="button"
              onClick={p.onReject}
              disabled={disabled}
              style={{ ...rejectBtnStyle(disabled), marginLeft: 'auto' }}
              onMouseEnter={(e) => { if (!disabled) e.currentTarget.style.backgroundColor = RED.bg; }}
              onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = '#fff'; }}
            >
              Reject
            </button>
            <button
              type="button"
              onClick={p.onApprove}
              disabled={disabled}
              style={approveBtnStyle(disabled)}
              onMouseEnter={(e) => { if (!disabled) e.currentTarget.style.filter = 'brightness(0.93)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.filter = 'none'; }}
            >
              Approve
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function _promptReason(): string | null {
  const r = window.prompt('Reject reason:', 'Operator review — rejected');
  if (!r || !r.trim()) return null;
  return r.trim();
}

// ────────────────────────────────────────────────────────────────────────────
// 1. LabValueEditor — Observation
// ────────────────────────────────────────────────────────────────────────────

export function LabValueEditor(p: FieldEditorProps): ReactElement {
  const payload = p.row.payload;
  const codingArr = _arr(_obj(payload.code).coding);
  const coding0 = _obj(codingArr[0]);
  const valueQty = _obj(payload.valueQuantity);

  const [code, setCode] = useState(_str(coding0.code));
  const [display, setDisplay] = useState(_str(coding0.display));
  const [value, setValue] = useState<number | ''>(_num(valueQty.value));
  const [unit, setUnit] = useState(_str(valueQty.unit));

  // Emit override whenever any field changes. The merged override carries the
  // ENTIRE original payload with patches applied so the backend writer never
  // sees a partially-typed FHIR resource.
  useEffect(() => {
    const next: Record<string, unknown> = JSON.parse(JSON.stringify(payload));
    const code0 = { ..._obj(_arr(_obj(next.code).coding)[0]) };
    code0.code = code;
    code0.display = display;
    next.code = { ..._obj(next.code), coding: [code0, ..._arr(_obj(next.code).coding).slice(1)] };
    next.valueQuantity = {
      ...valueQty,
      value: value === '' ? null : value,
      unit,
    };
    p.onValueChange(p.row.id, next);
    // We deliberately exclude p.onValueChange from deps — its identity changes
    // per render in some parents and would re-fire this callback on every
    // keystroke without changing intent. The closure capture is sufficient.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, display, value, unit, p.row.id]);

  const sourceId = _firstSourceId(payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Lab value"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={fieldRow}>
        <label style={labelStyle}>LOINC code</label>
        <input
          type="text"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          disabled={inputDisabled}
          style={inputStyle}
        />
      </div>
      <div style={fieldRow}>
        <label style={labelStyle}>Display</label>
        <input
          type="text"
          value={display}
          onChange={(e) => setDisplay(e.target.value)}
          disabled={inputDisabled}
          style={inputStyle}
        />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        <div style={fieldRow}>
          <label style={labelStyle}>Value</label>
          <input
            type="number"
            value={value}
            onChange={(e) => setValue(e.target.value === '' ? '' : Number(e.target.value))}
            disabled={inputDisabled}
            style={inputStyle}
            step="any"
          />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Unit</label>
          <input
            type="text"
            value={unit}
            onChange={(e) => setUnit(e.target.value)}
            disabled={inputDisabled}
            style={inputStyle}
          />
        </div>
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// IntakeFormField — shared shape: payload = { kind, value: { ...fields, citations? } }
// ────────────────────────────────────────────────────────────────────────────

interface IntakeBase {
  kind?: unknown;
  value?: Record<string, unknown>;
}

function _intakeValue(payload: Record<string, unknown>): Record<string, unknown> {
  return _obj((payload as IntakeBase).value);
}

function _emitIntake(
  p: FieldEditorProps,
  patches: Record<string, unknown>,
): void {
  const original = p.row.payload;
  const oldValue = _intakeValue(original);
  const merged: Record<string, unknown> = {
    ...original,
    value: { ...oldValue, ...patches },
  };
  p.onValueChange(p.row.id, merged);
}

// ────────────────────────────────────────────────────────────────────────────
// 2. AllergyEditor — IntakeFormField (allergy)
// ────────────────────────────────────────────────────────────────────────────

const SEVERITY_OPTIONS = ['mild', 'moderate', 'severe', 'life-threatening', 'unknown'] as const;

export function AllergyEditor(p: FieldEditorProps): ReactElement {
  const v = _intakeValue(p.row.payload);
  const [substance, setSubstance] = useState(_str(v.substance));
  const [reaction, setReaction] = useState(_str(v.reaction));
  const [severity, setSeverity] = useState(_str(v.severity, 'unknown'));

  useEffect(() => {
    _emitIntake(p, { substance, reaction, severity });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [substance, reaction, severity, p.row.id]);

  const sourceId = _firstSourceId(p.row.payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Allergy"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={fieldRow}>
        <label style={labelStyle}>Substance</label>
        <input type="text" value={substance} onChange={(e) => setSubstance(e.target.value)} disabled={inputDisabled} style={inputStyle} />
      </div>
      <div style={fieldRow}>
        <label style={labelStyle}>Reaction</label>
        <input type="text" value={reaction} onChange={(e) => setReaction(e.target.value)} disabled={inputDisabled} style={inputStyle} />
      </div>
      <div style={fieldRow}>
        <label style={labelStyle}>Severity</label>
        <select value={severity} onChange={(e) => setSeverity(e.target.value)} disabled={inputDisabled} style={inputStyle}>
          {SEVERITY_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// 3. MedicationEditor — IntakeFormField (medication)
// ────────────────────────────────────────────────────────────────────────────

export function MedicationEditor(p: FieldEditorProps): ReactElement {
  const v = _intakeValue(p.row.payload);
  const [name, setName] = useState(_str(v.name));
  const [dose, setDose] = useState(_str(v.dose));
  const [route, setRoute] = useState(_str(v.route));
  const [frequency, setFrequency] = useState(_str(v.frequency));

  useEffect(() => {
    _emitIntake(p, { name, dose, route, frequency });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, dose, route, frequency, p.row.id]);

  const sourceId = _firstSourceId(p.row.payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Medication"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <div style={fieldRow}>
          <label style={labelStyle}>Name</label>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Dose</label>
          <input type="text" value={dose} onChange={(e) => setDose(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Route</label>
          <input type="text" value={route} onChange={(e) => setRoute(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Frequency</label>
          <input type="text" value={frequency} onChange={(e) => setFrequency(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// 4. DemographicsEditor — IntakeFormField (demographics)
// ────────────────────────────────────────────────────────────────────────────
// The Demographics shape is documented as
//   { kind: "demographics", value: { name: { value, citations }, dob: {...},
//                                    sex, mrn, address } }
// In practice a number of those fields are themselves wrapper objects with
// a ``value`` member (e.g. name.value, dob.value), while plainer fields
// (sex, mrn) may be bare strings. We render gracefully by reading
// ``getCited()`` which accepts either shape.

const SEX_OPTIONS = ['male', 'female', 'unknown', 'other'] as const;

function _getCited(field: unknown, fallback = ''): string {
  if (typeof field === 'string') return field;
  if (field && typeof field === 'object') {
    const v = (field as Record<string, unknown>).value;
    if (typeof v === 'string') return v;
  }
  return fallback;
}

function _setCited(original: unknown, next: string): unknown {
  if (original && typeof original === 'object' && !Array.isArray(original)) {
    return { ...(original as Record<string, unknown>), value: next };
  }
  return next;
}

export function DemographicsEditor(p: FieldEditorProps): ReactElement {
  const v = _intakeValue(p.row.payload);
  const [name, setName] = useState(_getCited(v.name));
  const [dob, setDob] = useState(_getCited(v.dob));
  const [sex, setSex] = useState(_getCited(v.sex, 'unknown'));
  const [mrn, setMrn] = useState(_getCited(v.mrn));
  const [address, setAddress] = useState(_getCited(v.address));

  useEffect(() => {
    _emitIntake(p, {
      name: _setCited(v.name, name),
      dob: _setCited(v.dob, dob),
      sex: _setCited(v.sex, sex),
      mrn: _setCited(v.mrn, mrn),
      address: _setCited(v.address, address),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, dob, sex, mrn, address, p.row.id]);

  const sourceId = _firstSourceId(p.row.payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Demographics"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr', gap: 10 }}>
        <div style={fieldRow}>
          <label style={labelStyle}>Name</label>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Date of birth</label>
          <input type="date" value={dob} onChange={(e) => setDob(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Sex</label>
          <select value={sex} onChange={(e) => setSex(e.target.value)} disabled={inputDisabled} style={inputStyle}>
            {SEX_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>{opt}</option>
            ))}
          </select>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, 1fr) 3fr', gap: 10 }}>
        <div style={fieldRow}>
          <label style={labelStyle}>MRN</label>
          <input type="text" value={mrn} onChange={(e) => setMrn(e.target.value)} disabled={inputDisabled} style={inputStyle} />
        </div>
        <div style={fieldRow}>
          <label style={labelStyle}>Address</label>
          <textarea
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            disabled={inputDisabled}
            rows={2}
            style={{ ...inputStyle, resize: 'vertical' }}
          />
        </div>
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// 5. FamilyHistoryEditor — IntakeFormField (family_history)
// ────────────────────────────────────────────────────────────────────────────

export function FamilyHistoryEditor(p: FieldEditorProps): ReactElement {
  const v = _intakeValue(p.row.payload);
  const [relationship, setRelationship] = useState(_str(v.relationship));
  const [condition, setCondition] = useState(_str(v.condition));

  useEffect(() => {
    _emitIntake(p, { relationship, condition });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relationship, condition, p.row.id]);

  const sourceId = _firstSourceId(p.row.payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Family history"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={fieldRow}>
        <label style={labelStyle}>Relationship</label>
        <input type="text" value={relationship} onChange={(e) => setRelationship(e.target.value)} disabled={inputDisabled} style={inputStyle} />
      </div>
      <div style={fieldRow}>
        <label style={labelStyle}>Condition</label>
        <input type="text" value={condition} onChange={(e) => setCondition(e.target.value)} disabled={inputDisabled} style={inputStyle} />
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// 6. ChiefConcernEditor — IntakeFormField (chief_concern)
// ────────────────────────────────────────────────────────────────────────────

export function ChiefConcernEditor(p: FieldEditorProps): ReactElement {
  const v = _intakeValue(p.row.payload);
  const [text, setText] = useState(_str(v.value));

  useEffect(() => {
    _emitIntake(p, { value: text });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, p.row.id]);

  const sourceId = _firstSourceId(p.row.payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Chief concern"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={fieldRow}>
        <label style={labelStyle}>Concern</label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={inputDisabled}
          rows={3}
          style={{ ...inputStyle, resize: 'vertical' }}
        />
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// 7. CodeStatusEditor — IntakeFormField (code_status)
// ────────────────────────────────────────────────────────────────────────────

const CODE_STATUS_OPTIONS = ['full', 'dnr', 'dni', 'dnar', 'comfort_only'] as const;

export function CodeStatusEditor(p: FieldEditorProps): ReactElement {
  const v = _intakeValue(p.row.payload);
  const [status, setStatusValue] = useState(_str(v.status, 'full'));

  useEffect(() => {
    _emitIntake(p, { status });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, p.row.id]);

  const sourceId = _firstSourceId(p.row.payload);
  const inputDisabled = p.busy || p.status === 'busy' || p.status === 'done';

  return (
    <CardScaffold
      kindLabel="Code status"
      row={p.row}
      status={p.status}
      statusMessage={p.statusMessage}
      busy={p.busy}
      hasSource={Boolean(sourceId)}
      onCitationClick={sourceId && p.onCitationClick ? () => p.onCitationClick?.(sourceId, null) : undefined}
      onApprove={() => p.onApprove(p.row.id)}
      onReject={() => {
        const reason = _promptReason();
        if (reason) p.onReject(p.row.id, reason);
      }}
    >
      <div style={fieldRow}>
        <label style={labelStyle}>Status</label>
        <select value={status} onChange={(e) => setStatusValue(e.target.value)} disabled={inputDisabled} style={inputStyle}>
          {CODE_STATUS_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
      </div>
    </CardScaffold>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Resolver — pick the right editor for a row
// ────────────────────────────────────────────────────────────────────────────

/**
 * Choose an editor component based on the row's target_resource_type and (for
 * IntakeFormField) the field-kind suffix in target_resource_id. Returns null
 * for unknown shapes — the caller renders a "no editor for this resource"
 * placeholder rather than crashing.
 */
export function resolveEditor(row: PendingExtractionRow):
  | ((p: FieldEditorProps) => ReactElement)
  | null {
  if (row.target_resource_type === 'Observation') return LabValueEditor;
  if (row.target_resource_type !== 'IntakeFormField') return null;
  // Prefer payload.kind (authoritative); fall back to target_resource_id parsing.
  const payload = row.payload as { kind?: unknown };
  let kind = typeof payload.kind === 'string' ? payload.kind : '';
  if (!kind) {
    // target_resource_id format: copilot-{document_id}-intake-{kind}-{idx}
    const m = /-intake-([\w-]+?)-\d+$/.exec(row.target_resource_id ?? '');
    if (m) kind = m[1];
  }
  switch (kind) {
    case 'allergy': return AllergyEditor;
    case 'medication': return MedicationEditor;
    case 'demographics': return DemographicsEditor;
    case 'family_history': return FamilyHistoryEditor;
    case 'chief_concern': return ChiefConcernEditor;
    case 'code_status': return CodeStatusEditor;
    default: return null;
  }
}
