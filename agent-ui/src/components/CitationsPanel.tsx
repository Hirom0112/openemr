import { useState } from 'react';
import type { Citation } from '../types';
import { buildCitationUrl } from '../utils/citations';
import { NEU, MUTED, secondaryButtonStyle } from '../styles/tokens';

function openInChart(url: string): void {
  window.parent.postMessage({ type: 'copilot:openChart', url }, window.location.origin);
}

const RESOURCE_LABELS: Record<string, string> = {
  Observation:         'Lab / Observation',
  DiagnosticReport:    'Report',
  MedicationRequest:   'Medication',
  MedicationStatement: 'Medication',
  Condition:           'Diagnosis',
  AllergyIntolerance:  'Allergy',
  Encounter:           'Encounter',
  Flag:                'Flag',
  Patient:             'Patient',
};

const CLAIM_COLORS: Record<string, string> = {
  lab_value:   '#1a6fa8',
  vital:       '#1a8a6f',
  medication:  '#7b4fa8',
  condition:   '#a84f1a',
  allergy:     '#a81a1a',
  code_status: '#4f4fa8',
  isolation:   '#4fa84f',
};

interface CitationsPanelProps {
  citations: Citation[];
  patientOrder?: string[];
}

export default function CitationsPanel({ citations, patientOrder }: CitationsPanelProps) {
  const [open, setOpen] = useState(false);

  if (citations.length === 0) return null;

  const sorted = patientOrder
    ? [...citations].sort((a, b) => {
        const ai = patientOrder.indexOf(a.patient_id);
        const bi = patientOrder.indexOf(b.patient_id);
        return (ai === -1 ? 9999 : ai) - (bi === -1 ? 9999 : bi);
      })
    : citations;

  return (
    <div style={{ marginTop: 10, borderTop: `1px solid ${NEU.border}`, paddingTop: 6 }}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          fontSize: 11,
          color: MUTED,
          padding: '2px 0',
          display: 'flex',
          alignItems: 'center',
          gap: 5,
          fontFamily: 'inherit',
        }}
      >
        <span style={{ fontSize: 9 }}>{open ? '▾' : '▸'}</span>
        <span style={{ fontWeight: 500 }}>
          {citations.length} source{citations.length !== 1 ? 's' : ''}
        </span>
      </button>

      {open && (
        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {sorted.map((c, i) => {
            const url = buildCitationUrl(c);
            const label = RESOURCE_LABELS[c.resource_type] ?? c.resource_type;
            const color = CLAIM_COLORS[c.claim_class] ?? '#6b7280';
            return (
              <div
                key={`${c.resource_id}-${i}`}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 10px',
                  background: NEU.bg,
                  border: `1px solid ${NEU.border}`,
                  borderRadius: 6,
                  fontSize: 12,
                }}
              >
                <span
                  style={{
                    background: color,
                    color: '#fff',
                    borderRadius: 6,
                    padding: '2px 7px',
                    fontSize: 10,
                    fontWeight: 500,
                    flexShrink: 0,
                    whiteSpace: 'nowrap',
                    letterSpacing: 0.2,
                  }}
                >
                  {label}
                </span>
                <span style={{ flex: 1, color: NEU.text, minWidth: 0 }}>
                  <span style={{ display: 'block', lineHeight: 1.4 }}>{c.value_summary}</span>
                  {c.effective_datetime && (
                    <span style={{ display: 'block', color: MUTED, fontSize: 11, marginTop: 1 }}>
                      {c.effective_datetime}
                    </span>
                  )}
                </span>
                {url ? (
                  <button
                    onClick={() => openInChart(url)}
                    style={{
                      flexShrink: 0,
                      fontSize: 11,
                      fontWeight: 500,
                      padding: '3px 9px',
                      ...secondaryButtonStyle(),
                      borderRadius: 4,
                      cursor: 'pointer',
                      whiteSpace: 'nowrap',
                      fontFamily: 'inherit',
                    }}
                  >
                    View in Chart ↗
                  </button>
                ) : (
                  <span
                    style={{
                      flexShrink: 0,
                      fontSize: 11,
                      color: MUTED,
                      fontStyle: 'italic',
                    }}
                  >
                    No link
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
