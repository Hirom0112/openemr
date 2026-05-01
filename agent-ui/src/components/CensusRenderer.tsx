import { useState } from 'react';
import type { CensusData, CensusPatient, Citation } from '../types';
import { resolvePatientPid } from '../utils/citations';
import DisclaimerIcon from './DisclaimerIcon';

function openPatientChart(patientId: string, openemrPid?: string): void {
  // openemrPid is the numeric integer PID OpenEMR requires for set_pid.
  // Fall back to resolvePatientPid for legacy pt-NNN synthetic IDs.
  const pid = openemrPid ?? resolvePatientPid(patientId);
  const url = `/interface/patient_file/summary/demographics_full.php?set_pid=${pid}`;
  window.open(url, '_blank', 'noopener,noreferrer');
}

// Tier groupings based on rules_engine_config.yaml
const IMMEDIATE_LEVELS = new Set([1, 2]);      // Sepsis / Rapid Response, Sepsis Concern
const CRITICAL_VITAL_LEVELS = new Set([3, 4, 5, 6]); // Critical Lab, Critical Vital, AMS, Pain
const CODE_STATUS_LEVEL = 9;                   // Blank Code Status
const ABNORMAL_LAB_LEVELS = new Set([7]);      // Abnormal Lab — Monitoring Required
const LAB_PREVIEW_COUNT = 4;

// Design tokens — flat surfaces, 3px left border + tinted bg
const RED = { bg: '#FCEBEB', border: '#E24B4A', text: '#7F1D1D', secondary: '#991B1B' };
const AMB = { bg: '#FAEEDA', border: '#EF9F27', text: '#78350F', secondary: '#92400E' };

type ColorToken = typeof RED;

interface CensusRendererProps {
  data: CensusData;
  narrative: string;
  citations: Citation[];
  onBrief: (patientName: string) => void;
  providerName?: string;
}

function formatTime(d: Date): string {
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

function admitBadge(days: number | undefined): { label: string; bg: string; fg: string; border: string } | null {
  if (days === undefined || days === null) return null;
  if (days === 0) return { label: 'Overnight', bg: '#e0f2fe', fg: '#0369a1', border: '#7dd3fc' };
  return { label: `Day ${days + 1}`, bg: '#f3f4f6', fg: '#6b7280', border: '#d1d5db' };
}

function extractTrigger(explanation: string): string {
  const idx = explanation.toLowerCase().indexOf(' because ');
  if (idx !== -1) {
    const s = explanation.slice(idx + 9).replace(/\.$/, '');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  return explanation.replace(/\.$/, '');
}

function TierDot({ color }: { color: ColorToken }) {
  return (
    <span
      aria-hidden="true"
      style={{ width: 8, height: 8, borderRadius: '50%', background: color.border, flexShrink: 0, display: 'inline-block' }}
    />
  );
}

function SectionHeading({ label, color, aside }: { label: string; color: ColorToken; aside?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 16, marginBottom: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <TierDot color={color} />
        <h2 style={{ margin: 0, fontSize: 11, fontWeight: 500, letterSpacing: '0.06em', textTransform: 'uppercase', color: color.text }}>
          {label}
        </h2>
      </div>
      {aside}
    </div>
  );
}

function PatientRow({ patient, color, onBrief }: { patient: CensusPatient; color: ColorToken; onBrief: (name: string) => void }) {
  const trigger = extractTrigger(patient.explanation);
  const badge = admitBadge(patient.days_since_admit);
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      padding: '8px 12px',
      borderRadius: 6,
      background: color.bg,
      borderLeft: `3px solid ${color.border}`,
      marginBottom: 4,
    }}>
      <span style={{ fontSize: 11, fontWeight: 500, color: color.text, flexShrink: 0, minWidth: 22 }}>
        P{patient.triage_level}
      </span>
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, fontWeight: 500, color: color.text }}>{patient.name}</span>
          <span style={{ fontSize: 11, color: color.secondary }}>#{patient.mrn.slice(0, 8)}</span>
          {badge && (
            <span style={{
              fontSize: 10, fontWeight: 500, padding: '1px 6px', borderRadius: 999,
              background: badge.bg, color: badge.fg, border: `1px solid ${badge.border}`,
              flexShrink: 0,
            }}>
              {badge.label}
            </span>
          )}
        </span>
        <span style={{
          display: 'block', fontSize: 12, color: color.secondary, marginTop: 1,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {patient.triage_label} · {trigger}
        </span>
      </span>
      <button
        aria-label={`Brief ${patient.name}`}
        onClick={() => onBrief(patient.name)}
        style={{
          flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
          background: '#fff', color: color.text, border: `1px solid ${color.border}`,
          borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
        }}
      >
        Brief ↗
      </button>
      <button
        aria-label={`Open chart for ${patient.name}`}
        onClick={() => openPatientChart(patient.patient_id, patient.openemr_pid)}
        style={{
          flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
          background: '#e8edf8', color: color.text, border: `1px solid ${color.border}`,
          borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
        }}
      >
        Chart ↗
      </button>
    </div>
  );
}

function LabSeverityBadge({ level }: { level: number }) {
  const isCritical = level === 3;
  const col = isCritical ? RED : AMB;
  const label = isCritical ? 'Critical' : 'Borderline';
  return (
    <span style={{
      background: col.border, color: '#fff', borderRadius: 999,
      padding: '2px 8px', fontSize: 11, fontWeight: 500, whiteSpace: 'nowrap',
    }}>
      {label}
    </span>
  );
}

export default function CensusRenderer({ data, citations, onBrief, providerName }: CensusRendererProps) {
  const [labExpanded, setLabExpanded] = useState(false);

  const census = data?.census ?? [];
  const now = new Date();

  const immediate = census.filter(p => IMMEDIATE_LEVELS.has(p.triage_level));
  const criticalVital = census.filter(p => CRITICAL_VITAL_LEVELS.has(p.triage_level));
  const codeStatus = census.filter(p => p.triage_level === CODE_STATUS_LEVEL);
  const abnormalLab = census.filter(p => ABNORMAL_LAB_LEVELS.has(p.triage_level));

  const labVisible = labExpanded ? abnormalLab : abnormalLab.slice(0, LAB_PREVIEW_COUNT);
  const labHiddenCount = abnormalLab.length - LAB_PREVIEW_COUNT;

  return (
    <div style={{ fontSize: 13, lineHeight: 1.5, fontFamily: 'inherit' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>Morning census</div>
          <div style={{ fontSize: 11, color: '#6b7280', marginTop: 1 }}>
            {census.length} patient{census.length !== 1 ? 's' : ''}
            {providerName ? ` · ${providerName}` : ''}
            {' · '}{formatTime(now)}
          </div>
        </div>
        <span style={{
          fontSize: 11, fontWeight: 500, color: '#166534',
          background: '#dcfce7', border: '1px solid #86efac',
          borderRadius: 999, padding: '3px 10px',
          display: 'flex', alignItems: 'center', gap: 5,
        }}>
          <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: '50%', background: '#16a34a' }} />
          Live
        </span>
      </div>

      {/* Summary strip — 4 tier metric cards */}
      <ul
        role="list"
        aria-label="Census tier summary"
        style={{
          display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)',
          gap: 6, listStyle: 'none', padding: 0, margin: '0 0 4px',
        }}
      >
        {[
          { label: 'Immediate', count: immediate.length, color: RED },
          { label: 'Critical vital', count: criticalVital.length, color: AMB },
          { label: 'Code unverified', count: codeStatus.length, color: RED },
          { label: 'Abnormal lab', count: abnormalLab.length, color: AMB },
        ].map(({ label, count, color }) => (
          <li key={label} style={{
            background: color.bg, border: `1px solid ${color.border}`,
            borderRadius: 8, padding: '8px 10px', textAlign: 'center',
          }}>
            <div style={{ fontSize: 11, fontWeight: 500, color: color.text, marginBottom: 2 }}>{label}</div>
            <div style={{ fontSize: 20, fontWeight: 500, color: color.text }}>{count}</div>
          </li>
        ))}
      </ul>

      {/* Immediate attention */}
      {immediate.length > 0 && (
        <section aria-labelledby="tier-immediate">
          <SectionHeading label="Immediate attention" color={RED} />
          {immediate.map(p => <PatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} />)}
        </section>
      )}

      {/* Critical vital sign */}
      {criticalVital.length > 0 && (
        <section aria-labelledby="tier-critical">
          <SectionHeading label="Critical vital sign" color={AMB} />
          {criticalVital.map(p => <PatientRow key={p.patient_id} patient={p} color={AMB} onBrief={onBrief} />)}
        </section>
      )}

      {/* Code status not documented */}
      {codeStatus.length > 0 && (
        <section aria-labelledby="tier-code">
          <SectionHeading label="Code status not documented" color={RED} />
          <div style={{ fontSize: 11, color: RED.secondary, marginBottom: 6, paddingLeft: 2 }}>
            Hard safety flag · verify before orders
          </div>
          {codeStatus.map(p => <PatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} />)}
        </section>
      )}

      {/* Abnormal lab — monitoring required */}
      {abnormalLab.length > 0 && (
        <section aria-labelledby="tier-lab">
          <SectionHeading
            label="Abnormal lab · monitoring required"
            color={AMB}
            aside={<span style={{ fontSize: 11, color: '#6b7280' }}>{abnormalLab.length} patients</span>}
          />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {labVisible.map(p => {
              const labBadge = admitBadge(p.days_since_admit);
              return (
              <div key={p.patient_id} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '6px 10px', borderRadius: 6,
                background: AMB.bg, borderLeft: `3px solid ${AMB.border}`,
              }}>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 13, fontWeight: 500, color: AMB.text }}>{p.name}</span>
                    <span style={{ fontSize: 11, color: AMB.secondary }}>#{p.mrn.slice(0, 8)}</span>
                    {labBadge && (
                      <span style={{
                        fontSize: 10, fontWeight: 500, padding: '1px 6px', borderRadius: 999,
                        background: labBadge.bg, color: labBadge.fg, border: `1px solid ${labBadge.border}`,
                        flexShrink: 0,
                      }}>
                        {labBadge.label}
                      </span>
                    )}
                  </span>
                  <span style={{ display: 'block', fontSize: 12, color: AMB.secondary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {extractTrigger(p.explanation)}
                  </span>
                </span>
                <LabSeverityBadge level={p.triage_level} />
                <button
                  aria-label={`Brief ${p.name}`}
                  onClick={() => onBrief(p.name)}
                  style={{
                    flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
                    background: '#fff', color: AMB.text, border: `1px solid ${AMB.border}`,
                    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                  }}
                >
                  Brief ↗
                </button>
                <button
                  aria-label={`Open chart for ${p.name}`}
                  onClick={() => openPatientChart(p.patient_id, p.openemr_pid)}
                  style={{
                    flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
                    background: '#e8edf8', color: AMB.text, border: `1px solid ${AMB.border}`,
                    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                  }}
                >
                  Chart ↗
                </button>
              </div>
            );
          })}
          </div>
          {!labExpanded && labHiddenCount > 0 && (
            <button
              onClick={() => setLabExpanded(true)}
              style={{
                marginTop: 6, fontSize: 12, fontWeight: 500,
                background: 'none', border: 'none', color: AMB.text,
                cursor: 'pointer', padding: '4px 0', fontFamily: 'inherit',
              }}
            >
              Show {labHiddenCount} more ↓
            </button>
          )}
        </section>
      )}

      {/* Disclaimer */}
      <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid #e5e7eb', display: 'flex', justifyContent: 'flex-end' }}>
        <DisclaimerIcon citations={citations} />
      </div>
    </div>
  );
}
