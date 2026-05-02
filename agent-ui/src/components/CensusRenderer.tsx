import { useState } from 'react';
import type { CensusData, CensusPatient, Citation } from '../types';
import { resolvePatientPid } from '../utils/citations';
import DisclaimerIcon from './DisclaimerIcon';
import { RED, AMB, NEU, MUTED, primaryButtonStyle, secondaryButtonStyle } from '../styles/tokens';
import type { ColorToken } from '../styles/tokens';
import {
  TierDot,
  MetricStrip,
  PatientRow,
  LivePill,
  AdmitBadge,
  DisclaimerFooter,
} from './primitives';

function openPatientChart(patientId: string, openemrPid?: string): void {
  // openemrPid is the numeric integer PID OpenEMR requires for set_pid.
  // Fall back to resolvePatientPid for legacy pt-NNN synthetic IDs.
  const pid = openemrPid ?? resolvePatientPid(patientId);
  // Guard: demographics_full.php expects the integer patient_data.pid column.
  // Passing a UUID or other non-numeric value loads an empty page, so refuse
  // to open it at all and surface the failure in the console.
  if (!/^\d+$/.test(pid)) {
    console.warn('Cannot open chart: resolved pid is not a positive integer', {
      patient_id: patientId,
      openemr_pid: openemrPid,
      resolved_pid: pid,
    });
    return;
  }
  const url = `/interface/patient_file/summary/demographics_full.php?set_pid=${pid}`;
  window.open(url, '_blank', 'noopener,noreferrer');
}

// Tier groupings based on rules_engine_config.yaml
const IMMEDIATE_LEVELS = new Set([1, 2]);      // Sepsis / Rapid Response, Sepsis Concern
const CRITICAL_VITAL_LEVELS = new Set([3, 4, 5, 6]); // Critical Lab, Critical Vital, AMS, Pain
const CODE_STATUS_LEVEL = 9;                   // Blank Code Status
const ABNORMAL_LAB_LEVELS = new Set([7]);      // Abnormal Lab — Monitoring Required
const LAB_PREVIEW_COUNT = 4;

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

function extractTrigger(explanation: string): string {
  const idx = explanation.toLowerCase().indexOf(' because ');
  if (idx !== -1) {
    const s = explanation.slice(idx + 9).replace(/\.$/, '');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  return explanation.replace(/\.$/, '');
}

function CensusSectionHeading({ label, color, aside }: { label: string; color: ColorToken; aside?: React.ReactNode }) {
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

function CensusPatientRow({ patient, color, onBrief }: { patient: CensusPatient; color: ColorToken; onBrief: (name: string) => void }) {
  const trigger = extractTrigger(patient.explanation);
  const briefBtnStyle: React.CSSProperties = {
    flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
    ...primaryButtonStyle(color),
    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  const chartBtnStyle: React.CSSProperties = {
    flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
    ...secondaryButtonStyle(),
    color: color.text,
    border: `1px solid ${color.border}`,
    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  return (
    <PatientRow
      color={color}
      left={`P${patient.triage_level}`}
      title={
        <>
          {patient.name}
          <span style={{ fontSize: 11, color: color.secondary }}>#{patient.mrn.slice(0, 8)}</span>
        </>
      }
      badges={<AdmitBadge days={patient.days_since_admit} />}
      subtitle={`${patient.triage_label} · ${trigger}`}
      actions={
        <>
          <button
            aria-label={`Brief ${patient.name}`}
            onClick={() => onBrief(patient.name)}
            style={briefBtnStyle}
          >
            Brief ↗
          </button>
          <button
            aria-label={`Open chart for ${patient.name}`}
            onClick={() => openPatientChart(patient.patient_id, patient.openemr_pid)}
            style={chartBtnStyle}
          >
            Chart ↗
          </button>
        </>
      }
    />
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
          <div style={{ fontSize: 11, color: NEU.secondary, marginTop: 1 }}>
            {census.length} patient{census.length !== 1 ? 's' : ''}
            {providerName ? ` · ${providerName}` : ''}
            {' · '}{formatTime(now)}
          </div>
        </div>
        <LivePill />
      </div>

      {/* Summary strip — 4 tier metric cards */}
      <MetricStrip
        ariaLabel="Census tier summary"
        items={[
          { label: 'Immediate', count: immediate.length, color: RED },
          { label: 'Critical vital', count: criticalVital.length, color: AMB },
          { label: 'Code unverified', count: codeStatus.length, color: RED },
          { label: 'Abnormal lab', count: abnormalLab.length, color: AMB },
        ]}
      />

      {/* Immediate attention */}
      {immediate.length > 0 && (
        <section aria-labelledby="tier-immediate">
          <CensusSectionHeading label="Immediate attention" color={RED} />
          {immediate.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} />)}
        </section>
      )}

      {/* Critical vital sign */}
      {criticalVital.length > 0 && (
        <section aria-labelledby="tier-critical">
          <CensusSectionHeading label="Critical vital sign" color={AMB} />
          {criticalVital.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={AMB} onBrief={onBrief} />)}
        </section>
      )}

      {/* Code status not documented */}
      {codeStatus.length > 0 && (
        <section aria-labelledby="tier-code">
          <CensusSectionHeading label="Code status not documented" color={RED} />
          <div style={{ fontSize: 11, color: RED.secondary, marginBottom: 6, paddingLeft: 2 }}>
            Hard safety flag · verify before orders
          </div>
          {codeStatus.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} />)}
        </section>
      )}

      {/* Abnormal lab — monitoring required */}
      {abnormalLab.length > 0 && (
        <section aria-labelledby="tier-lab">
          <CensusSectionHeading
            label="Abnormal lab · monitoring required"
            color={AMB}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{abnormalLab.length} patients</span>}
          />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {labVisible.map(p => {
              const trigger = extractTrigger(p.explanation);
              return (
                <PatientRow
                  key={p.patient_id}
                  color={AMB}
                  left={`P${p.triage_level}`}
                  title={
                    <>
                      {p.name}
                      <span style={{ fontSize: 11, color: AMB.secondary }}>#{p.mrn.slice(0, 8)}</span>
                    </>
                  }
                  badges={<AdmitBadge days={p.days_since_admit} />}
                  subtitle={trigger}
                  actions={
                    <>
                      <LabSeverityBadge level={p.triage_level} />
                      <button
                        aria-label={`Brief ${p.name}`}
                        onClick={() => onBrief(p.name)}
                        style={{
                          flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
                          ...primaryButtonStyle(AMB),
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
                          ...secondaryButtonStyle(),
                          color: AMB.text,
                          border: `1px solid ${AMB.border}`,
                          borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                        }}
                      >
                        Chart ↗
                      </button>
                    </>
                  }
                />
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
      <DisclaimerFooter>
        <DisclaimerIcon citations={citations} />
      </DisclaimerFooter>
    </div>
  );
}
