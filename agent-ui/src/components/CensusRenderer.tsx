import { useState } from 'react';
import type { CensusData, CensusPatient, Citation } from '../types';
import { resolvePatientPid } from '../utils/citations';
import DisclaimerIcon from './DisclaimerIcon';
import { RED, AMB, NEU, MUTED, primaryButtonStyle, secondaryButtonStyle, cardStyle } from '../styles/tokens';
import type { ColorToken } from '../styles/tokens';
import {
  TierDot,
  MetricStrip,
  PatientRow,
  LivePill,
  AdmitBadge,
  DisclaimerFooter,
  Pill,
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
  const url = `/interface/patient_file/summary/demographics.php?set_pid=${pid}`;
  // Switch the patient inside OpenEMR's frame shell — same flow used by the
  // patient finder and tracker (interface/main/tabs/js/frame_proxies.js).
  // Falls back to a new tab if loaded outside the shell (standalone dev).
  const w = window as unknown as {
    top?: { restoreSession?: () => void; RTop?: { location: string } };
  };
  if (w.top?.RTop) {
    w.top.restoreSession?.();
    w.top.RTop.location = url;
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

// Tier groupings based on rules_engine_config.yaml
const IMMEDIATE_LEVELS = new Set([1, 2]);
const CRITICAL_LAB_LEVEL = 3;
const CRITICAL_VITAL_LEVELS = new Set([4, 5, 6]);
const SEVERE_PAIN_LEVEL = 7;
const ABNORMAL_LAB_LEVELS = new Set([8]);
const STABLE_CHRONIC_LEVEL = 9;
const CODE_STATUS_LEVEL = 10;
const ROUTINE_LEVEL = 11;
const LAB_PREVIEW_COUNT = 4;

interface CensusRendererProps {
  data: CensusData;
  narrative: string;
  citations: Citation[];
  onBrief: (patientName: string, patientId?: string) => void;
  onMeds: (patientName: string, patientId?: string) => void;
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

function CensusPatientRow({ patient, color, onBrief, onMeds }: { patient: CensusPatient; color: ColorToken; onBrief: (name: string, patientId?: string) => void; onMeds: (name: string, patientId?: string) => void }) {
  const trigger = extractTrigger(patient.explanation);
  const briefBtnStyle: React.CSSProperties = {
    flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
    ...primaryButtonStyle(color),
    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  const medsBtnStyle: React.CSSProperties = {
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
            onClick={() => onBrief(patient.name, patient.patient_id)}
            style={briefBtnStyle}
          >
            Brief ↗
          </button>
          <button
            aria-label={`Medications for ${patient.name}`}
            onClick={() => onMeds(patient.name, patient.patient_id)}
            style={medsBtnStyle}
          >
            Meds ↗
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
  const isCritical = level === CRITICAL_LAB_LEVEL;
  const col = isCritical ? RED : AMB;
  const label = isCritical ? 'Critical' : 'Borderline';
  return <Pill color={col} label={label} />;
}

export default function CensusRenderer({ data, citations, onBrief, onMeds, providerName }: CensusRendererProps) {
  const [labExpanded, setLabExpanded] = useState(false);

  const census = data?.census ?? [];
  const now = new Date();

  const immediate = census.filter(p => IMMEDIATE_LEVELS.has(p.triage_level));
  const criticalLab = census.filter(p => p.triage_level === CRITICAL_LAB_LEVEL);
  const criticalVital = census.filter(p => CRITICAL_VITAL_LEVELS.has(p.triage_level));
  const severePain = census.filter(p => p.triage_level === SEVERE_PAIN_LEVEL);
  const abnormalLab = census.filter(p => ABNORMAL_LAB_LEVELS.has(p.triage_level));
  const codeStatus = census.filter(p => p.triage_level === CODE_STATUS_LEVEL);
  const stableChronic = census.filter(p => p.triage_level === STABLE_CHRONIC_LEVEL);
  const routine = census.filter(p => p.triage_level === ROUTINE_LEVEL);
  const KNOWN_TIERS = new Set<number>([
    ...IMMEDIATE_LEVELS,
    CRITICAL_LAB_LEVEL,
    ...CRITICAL_VITAL_LEVELS,
    SEVERE_PAIN_LEVEL,
    ...ABNORMAL_LAB_LEVELS,
    CODE_STATUS_LEVEL,
    STABLE_CHRONIC_LEVEL,
    ROUTINE_LEVEL,
  ]);
  const other = census.filter(p => !KNOWN_TIERS.has(p.triage_level));

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

      {data?.dropped !== undefined && data.dropped > 0 && typeof data.requested === 'number' ? (
        <div
          role="status"
          title={data.dropped_ids && data.dropped_ids.length > 0 ? `Failed: ${data.dropped_ids.join(', ')}` : undefined}
          style={{ ...cardStyle(AMB), padding: '6px 10px', fontSize: 12, color: AMB.text, marginBottom: 10 }}
        >
          Showing {data.total} of {data.requested} patients — {data.dropped} failed to load. Refresh to retry.
        </div>
      ) : null}

      {/* Summary strip — 4 tier metric cards */}
      <MetricStrip
        ariaLabel="Census tier summary"
        items={[
          { label: 'Immediate', count: immediate.length, color: RED },
          { label: 'Critical lab', count: criticalLab.length, color: RED },
          { label: 'Critical vital', count: criticalVital.length, color: AMB },
          { label: 'Severe pain', count: severePain.length, color: AMB },
          { label: 'Code unverified', count: codeStatus.length, color: RED },
          { label: 'Abnormal lab', count: abnormalLab.length, color: AMB },
          { label: 'Stable', count: stableChronic.length, color: NEU },
          ...(routine.length > 0 ? [{ label: 'Routine', count: routine.length, color: NEU }] : []),
          ...(other.length > 0 ? [{ label: 'Other', count: other.length, color: NEU }] : []),
        ]}
      />

      {/* Immediate attention */}
      {immediate.length > 0 && (
        <section aria-labelledby="tier-immediate">
          <CensusSectionHeading label="Immediate attention" color={RED} />
          {immediate.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Critical lab — unacknowledged */}
      {criticalLab.length > 0 && (
        <section aria-labelledby="tier-critical-lab">
          <CensusSectionHeading label="Critical lab — unacknowledged" color={RED} />
          {criticalLab.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Critical vital sign */}
      {criticalVital.length > 0 && (
        <section aria-labelledby="tier-critical">
          <CensusSectionHeading label="Critical vital sign" color={AMB} />
          {criticalVital.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={AMB} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Severe pain */}
      {severePain.length > 0 && (
        <section aria-labelledby="tier-pain">
          <CensusSectionHeading label="Severe pain" color={AMB} />
          {severePain.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={AMB} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Code status not documented */}
      {codeStatus.length > 0 && (
        <section aria-labelledby="tier-code">
          <CensusSectionHeading label="Code status not documented" color={RED} />
          <div style={{ fontSize: 11, color: RED.secondary, marginBottom: 6, paddingLeft: 2 }}>
            Hard safety flag · verify before orders
          </div>
          {codeStatus.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Abnormal lab — monitoring required */}
      {abnormalLab.length > 0 && (
        <section aria-labelledby="tier-lab">
          <CensusSectionHeading
            label="Abnormal lab · monitoring required"
            color={AMB}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{abnormalLab.length} patient{abnormalLab.length !== 1 ? 's' : ''}</span>}
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
                        onClick={() => onBrief(p.name, p.patient_id)}
                        style={{
                          flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
                          ...primaryButtonStyle(AMB),
                          borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                        }}
                      >
                        Brief ↗
                      </button>
                      <button
                        aria-label={`Medications for ${p.name}`}
                        onClick={() => onMeds(p.name, p.patient_id)}
                        style={{
                          flexShrink: 0, fontSize: 12, fontWeight: 500, padding: '4px 10px',
                          ...primaryButtonStyle(AMB),
                          borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
                        }}
                      >
                        Meds ↗
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

      {/* Active condition — stable */}
      {stableChronic.length > 0 && (
        <section aria-labelledby="tier-stable">
          <CensusSectionHeading
            label="Active condition — stable"
            color={NEU}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{stableChronic.length} patient{stableChronic.length !== 1 ? 's' : ''}</span>}
          />
          {stableChronic.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={NEU} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Routine */}
      {routine.length > 0 && (
        <section aria-labelledby="tier-routine">
          <CensusSectionHeading
            label="Routine"
            color={NEU}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{routine.length} patient{routine.length !== 1 ? 's' : ''}</span>}
          />
          {routine.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={NEU} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* Other — surfaces unrecognized tiers so backend drift doesn't silently drop patients */}
      {other.length > 0 && (
        <section aria-labelledby="tier-other">
          <CensusSectionHeading
            label="Other"
            color={NEU}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{other.length} patient{other.length !== 1 ? 's' : ''}</span>}
          />
          {other.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={NEU} onBrief={onBrief} onMeds={onMeds} />)}
        </section>
      )}

      {/* What I can do — informational footer */}
      <CapabilitiesFooter providerName={providerName} patientCount={census.length} />

      {/* Disclaimer */}
      <DisclaimerFooter>
        <DisclaimerIcon citations={citations} />
      </DisclaimerFooter>
    </div>
  );
}

function CapabilitiesFooter({ providerName, patientCount }: { providerName?: string; patientCount: number }) {
  const name = providerName?.trim() ? `${providerName}'s` : 'your';
  const providerId = window.__COPILOT_CONFIG__?.providerId;
  const providerIdParenthetical = providerId !== undefined && providerId !== null && String(providerId) !== ''
    ? ` (Provider ID: ${providerId})`
    : '';
  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 10,
        borderTop: `1px solid ${NEU.border}`,
        fontSize: 12,
        color: NEU.secondary,
        lineHeight: 1.6,
      }}
    >
      <div style={{ marginBottom: 6 }}>
        What I can do: I&rsquo;m scoped to support {name} active session{providerIdParenthetical} and the {patientCount} patient{patientCount === 1 ? '' : 's'} on the current census. I can:
      </div>
      <div>🏥 Run morning triage on the active census</div>
      <div>📋 Brief any patient currently on rounds</div>
      <div>🔍 Answer targeted clinical questions from chart data</div>
      <div>💊 Surface medication safety flags for census patients</div>
      <div>📝 Generate shift handoff notes</div>
    </div>
  );
}
