import { useState, useEffect } from 'react';
import type { CensusData, CensusPatient, Citation } from '../types';
import { resolvePatientPid } from '../utils/citations';
import { formatFriendly, formatFriendlyWithSeconds } from '../utils/datetime';
import { getPrefetchStatus, type WarmStatus } from '../api';
import DisclaimerIcon from './DisclaimerIcon';
import { RED, AMB, NEU, MUTED, SURFACE, TYPE, primaryButtonStyle, secondaryButtonStyle, cardStyle, censusSectionHeadingStyle } from '../styles/tokens';
import type { ColorToken } from '../styles/tokens';
import {
  TierChip,
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
  onHandoff?: (patientIds: string[], patientNames: Record<string, string>) => void;
  handoffInFlight?: boolean;
  providerName?: string;
  /**
   * Called when the user clicks the Refresh button next to the census
   * timestamp. Should re-issue the census request with force_refresh=true so
   * the backend bypasses its Redis cache.
   */
  onRefresh?: () => void;
  /**
   * sessionId — used to poll /agent/prefetch/status for the per-row
   * ⚡/⏳ pills. Optional; when absent the pills simply never appear.
   */
  sessionId?: string;
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
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 20, marginBottom: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span
          aria-hidden="true"
          style={{
            width: 10,
            height: 10,
            borderRadius: '50%',
            background: color.border,
            flexShrink: 0,
            display: 'inline-block',
            boxShadow: color === NEU ? 'none' : `0 0 0 3px ${color.bg}`,
          }}
        />
        <h2 style={censusSectionHeadingStyle(color)}>{label}</h2>
      </div>
      {aside}
    </div>
  );
}

type WarmStatusValue = 'pending' | 'warming' | 'warmed' | 'failed';

function WarmDot({ status }: { status: WarmStatusValue | undefined }) {
  // No status known yet (poll hasn't returned, or no warm in flight) →
  // render nothing so we don't introduce noise on cold loads.
  if (!status) return null;
  const map: Record<WarmStatusValue, { color: string; label: string }> = {
    pending: { color: '#9CA3AF', label: 'Queued for cache warm — Brief click will fetch on demand' },
    warming: { color: '#F59E0B', label: 'Warming cache now — Brief click in a moment will be instant' },
    warmed:  { color: '#10B981', label: 'Cache warmed — Brief and Meds clicks are instant' },
    failed:  { color: '#EF4444', label: 'Cache warm failed — Brief click will fetch on demand' },
  };
  const { color, label } = map[status];
  return (
    <span
      title={label}
      aria-label={label}
      style={{
        display: 'inline-block',
        width: 7,
        height: 7,
        borderRadius: '50%',
        background: color,
        marginLeft: 6,
        verticalAlign: 'middle',
        // Subtle pulse while still warming so the eye catches the live state.
        animation: status === 'warming' ? 'copilot-warm-pulse 1.4s ease-in-out infinite' : undefined,
      }}
    />
  );
}

function CensusPatientRow({ patient, color, onBrief, onMeds, warmStatus }: { patient: CensusPatient; color: ColorToken; onBrief: (name: string, patientId?: string) => void; onMeds: (name: string, patientId?: string) => void; warmStatus?: WarmStatusValue }) {
  const trigger = extractTrigger(patient.explanation);
  const briefBtnStyle: React.CSSProperties = {
    flexShrink: 0, fontSize: 12, fontWeight: 600, padding: '6px 12px', minHeight: 30,
    ...primaryButtonStyle(color),
    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  const medsBtnStyle: React.CSSProperties = {
    flexShrink: 0, fontSize: 12, fontWeight: 600, padding: '6px 12px', minHeight: 30,
    ...primaryButtonStyle(color),
    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  const chartBtnStyle: React.CSSProperties = {
    flexShrink: 0, fontSize: 12, fontWeight: 600, padding: '6px 12px', minHeight: 30,
    ...secondaryButtonStyle(),
    color: color === NEU ? SURFACE.fg : color.text,
    border: `1px solid ${color.border}`,
    borderRadius: 6, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  return (
    <PatientRow
      color={color}
      left={<TierChip level={patient.triage_level} color={color} />}
      title={
        <>
          {patient.name}
          <span style={{ fontSize: 11, fontWeight: 500, color: color === NEU ? SURFACE.subtle : color.secondary }}>#{patient.mrn.slice(0, 8)}</span>
          <WarmDot status={warmStatus} />
        </>
      }
      badges={<AdmitBadge days={patient.days_since_admit} />}
      subtitle={
        <>
          <span style={{ fontWeight: 600, color: color === NEU ? SURFACE.fg : color.text }}>{patient.triage_label}</span>
          <span style={{ color: color === NEU ? SURFACE.subtle : color.secondary }}> · {trigger}</span>
        </>
      }
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

export default function CensusRenderer({ data, citations, onBrief, onMeds, onHandoff, handoffInFlight, providerName, onRefresh, sessionId }: CensusRendererProps) {
  const [labExpanded, setLabExpanded] = useState(false);

  const census = data?.census ?? [];

  // Freshness indicator. Mirrors BriefingRenderer's pattern but with tighter
  // thresholds because census drives every other surface — a stale census
  // means stale briefings, stale handoffs, stale triage. Tightening to 5/15
  // matches the new census_cache_ttl_seconds (300s).
  const STALE_AMBER_MS = 5 * 60 * 1000;   // >5 min  → amber
  const STALE_RED_MS = 15 * 60 * 1000;    // >15 min → red

  // Pending state on the Refresh button. Cleared when a fresh response
  // arrives — detected by generated_at flipping to a new value.
  const [refreshingFrom, setRefreshingFrom] = useState<string | null>(null);
  const [refreshHovered, setRefreshHovered] = useState(false);
  useEffect(() => {
    if (refreshingFrom !== null && data?.generated_at && data.generated_at !== refreshingFrom) {
      setRefreshingFrom(null);
    }
  }, [data?.generated_at, refreshingFrom]);

  // Per-patient warm status. Polls /agent/prefetch/status every 2s while
  // any patient is still pending/warming, then stops to keep the request
  // pressure flat. Best-effort — failures fall back to "no pill" so the
  // census still renders normally.
  const [warmMap, setWarmMap] = useState<Record<string, WarmStatus>>({});
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const result = await getPrefetchStatus(sessionId);
        if (cancelled) return;
        setWarmMap(result.patients);
        const stillRunning = Object.values(result.patients).some(
          (s) => s === 'pending' || s === 'warming',
        );
        if (stillRunning) {
          timer = window.setTimeout(tick, 2000);
        }
      } catch {
        // swallow — pills are non-critical
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [sessionId]);

  const generatedDate = data?.generated_at ? new Date(data.generated_at) : null;
  const generatedValid = generatedDate && !Number.isNaN(generatedDate.getTime());
  const ageMs = generatedValid ? Date.now() - generatedDate.getTime() : 0;
  const stalenessColor =
    !generatedValid ? MUTED
      : ageMs > STALE_RED_MS ? RED.text
      : ageMs > STALE_AMBER_MS ? AMB.text
      : MUTED;
  const generatedTimeShort = generatedValid ? formatFriendly(generatedDate) : undefined;
  const generatedTooltip = generatedValid
    ? `Census generated ${formatFriendlyWithSeconds(generatedDate)}`
    : undefined;

  const isRefreshing = refreshingFrom !== null;
  const canRefresh = !!onRefresh && !isRefreshing;
  const handleRefresh = () => {
    if (!canRefresh || !onRefresh) return;
    // Capture the current generated_at so the effect above can detect when a
    // new value arrives. Empty string is fine — any real new timestamp differs.
    setRefreshingFrom(data?.generated_at ?? '');
    onRefresh();
  };
  const refreshBtnStyle: React.CSSProperties = {
    background: refreshHovered && canRefresh ? '#f3f4f6' : 'transparent',
    border: 'none',
    padding: '0 4px',
    margin: 0,
    fontFamily: 'inherit',
    fontSize: 11,
    color: canRefresh ? MUTED : '#9ca3af',
    cursor: canRefresh ? 'pointer' : 'not-allowed',
    borderRadius: 4,
    lineHeight: 'inherit',
  };

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
    <div style={{ ...TYPE.body, color: SURFACE.fg, fontFamily: 'inherit' }}>
      <style>{`@keyframes copilot-warm-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }`}</style>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 600, color: SURFACE.fgStrong, letterSpacing: '-0.01em', lineHeight: 1.3 }}>Morning census</div>
          <div style={{ ...TYPE.caption, color: SURFACE.muted, marginTop: 3 }}>
            {census.length} patient{census.length !== 1 ? 's' : ''}
            {providerName ? ` · ${providerName}` : ''}
            {' · '}
            {generatedTimeShort ? (
              <span style={{ color: stalenessColor }}>
                <span title={generatedTooltip}>Census as of {generatedTimeShort}</span>
              </span>
            ) : (
              <span style={{ color: MUTED }}>Census</span>
            )}
            {onRefresh ? (
              <>
                <span style={{ color: MUTED }}>{'  ·  '}</span>
                <button
                  type="button"
                  onClick={handleRefresh}
                  disabled={!canRefresh}
                  onMouseEnter={() => setRefreshHovered(true)}
                  onMouseLeave={() => setRefreshHovered(false)}
                  onFocus={() => setRefreshHovered(true)}
                  onBlur={() => setRefreshHovered(false)}
                  aria-label="Refresh census"
                  style={refreshBtnStyle}
                >
                  {isRefreshing ? 'Refreshing…' : 'Refresh'}
                </button>
              </>
            ) : null}
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
          Showing {data.total} of {data.requested} patients · {data.dropped} failed to load. Refresh to retry.
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
          {immediate.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
        </section>
      )}

      {/* Critical lab — unacknowledged */}
      {criticalLab.length > 0 && (
        <section aria-labelledby="tier-critical-lab">
          <CensusSectionHeading label="Critical lab: unacknowledged" color={RED} />
          {criticalLab.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
        </section>
      )}

      {/* Critical vital sign */}
      {criticalVital.length > 0 && (
        <section aria-labelledby="tier-critical">
          <CensusSectionHeading label="Critical vital sign" color={AMB} />
          {criticalVital.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={AMB} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
        </section>
      )}

      {/* Severe pain */}
      {severePain.length > 0 && (
        <section aria-labelledby="tier-pain">
          <CensusSectionHeading label="Severe pain" color={AMB} />
          {severePain.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={AMB} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
        </section>
      )}

      {/* Code status not documented */}
      {codeStatus.length > 0 && (
        <section aria-labelledby="tier-code">
          <CensusSectionHeading label="Code status not documented" color={RED} />
          <div style={{ ...TYPE.caption, color: RED.secondary, marginBottom: 8, paddingLeft: 2 }}>
            Hard safety flag · verify before orders
          </div>
          {codeStatus.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={RED} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
        </section>
      )}

      {/* Abnormal lab — monitoring required */}
      {abnormalLab.length > 0 && (
        <section aria-labelledby="tier-lab">
          <CensusSectionHeading
            label="Abnormal lab: monitoring required"
            color={AMB}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{abnormalLab.length} patient{abnormalLab.length !== 1 ? 's' : ''}</span>}
          />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {labVisible.map(p => {
              const trigger = extractTrigger(p.explanation);
              return (
                <PatientRow
                  key={p.patient_id}
                  color={AMB}
                  left={<TierChip level={p.triage_level} color={AMB} />}
                  title={
                    <>
                      {p.name}
                      <span style={{ fontSize: 11, fontWeight: 500, color: AMB.secondary }}>#{p.mrn.slice(0, 8)}</span>
                      <WarmDot status={warmMap[p.patient_id]} />
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
                          flexShrink: 0, fontSize: 12, fontWeight: 600, padding: '6px 12px', minHeight: 30,
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
                          flexShrink: 0, fontSize: 12, fontWeight: 600, padding: '6px 12px', minHeight: 30,
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
                          flexShrink: 0, fontSize: 12, fontWeight: 600, padding: '6px 12px', minHeight: 30,
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
            label="Active condition: stable"
            color={NEU}
            aside={<span style={{ fontSize: 11, color: MUTED }}>{stableChronic.length} patient{stableChronic.length !== 1 ? 's' : ''}</span>}
          />
          {stableChronic.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={NEU} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
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
          {routine.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={NEU} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
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
          {other.map(p => <CensusPatientRow key={p.patient_id} patient={p} color={NEU} onBrief={onBrief} onMeds={onMeds} warmStatus={warmMap[p.patient_id]} />)}
        </section>
      )}

      {/* Generate shift handoff — global action, ONE button, NOT per-row */}
      {onHandoff && census.length > 0 && (
        <div style={{ marginTop: 14, display: 'flex', justifyContent: 'center' }}>
          <button
            type="button"
            aria-label="Generate shift handoff for all census patients"
            disabled={handoffInFlight}
            onClick={() => {
              const ids = census.map(p => p.patient_id);
              const names: Record<string, string> = {};
              for (const p of census) names[p.patient_id] = p.name;
              onHandoff(ids, names);
            }}
            style={{
              fontSize: 12, fontWeight: 500, padding: '6px 14px',
              ...primaryButtonStyle(NEU),
              borderRadius: 6,
              cursor: handoffInFlight ? 'not-allowed' : 'pointer',
              opacity: handoffInFlight ? 0.5 : 1,
              fontFamily: 'inherit', whiteSpace: 'nowrap',
            }}
          >
            {handoffInFlight ? 'Generating handoff…' : 'Generate shift handoff ↗'}
          </button>
        </div>
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
        marginTop: 20,
        paddingTop: 12,
        borderTop: `1px solid ${SURFACE.border}`,
        ...TYPE.body,
        fontSize: 12,
        color: SURFACE.muted,
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
