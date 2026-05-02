import type { PatientSummary } from '../types';
import { resolvePatientPid } from '../utils/citations';
import { tierColor, primaryButtonStyle, secondaryButtonStyle, NEU, LINK_TINT } from '../styles/tokens';

interface PatientCardProps {
  patient: PatientSummary;
  rank: number;
  selected: boolean;
  onExpand: (patientId: string) => void;
  onSelect: (patientId: string) => void;
}

export default function PatientCard({ patient, rank, selected, onExpand, onSelect }: PatientCardProps) {
  const color = tierColor(patient.priority);

  return (
    <div
      onClick={() => onSelect(patient.patient_id)}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        padding: '8px 10px',
        marginBottom: 5,
        borderRadius: 6,
        border: `1px solid ${NEU.border}`,
        borderLeft: `3px solid ${color.border}`,
        background: selected ? LINK_TINT : color.bg,
        cursor: 'pointer',
      }}
    >
      <span
        style={{
          minWidth: 26,
          height: 26,
          borderRadius: '50%',
          background: color.border,
          color: '#fff',
          fontSize: 11,
          fontWeight: 700,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        {rank}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <strong style={{ fontSize: 13, color: color.text }}>{patient.name}</strong>
          <span style={{ fontSize: 11, color: color.secondary, marginLeft: 6, flexShrink: 0 }}>
            {patient.bed} &middot; <span style={{ color: color.text, fontWeight: 500 }}>{patient.priority}</span>
          </span>
        </div>
        <div style={{ fontSize: 12, color: color.secondary, marginTop: 2 }}>{patient.one_line}</div>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          // Prefer openemr_pid (numeric integer pid required by demographics_full.php).
          // Fall back to resolvePatientPid for legacy pt-NNN synthetic IDs.
          const pid = patient.openemr_pid ?? resolvePatientPid(patient.patient_id);
          if (!/^\d+$/.test(pid)) {
            console.warn('Cannot open chart: no integer pid available', { patient_id: patient.patient_id, openemr_pid: patient.openemr_pid });
            return;
          }
          window.open(
            `${window.location.origin}/interface/patient_file/summary/demographics_full.php?set_pid=${pid}`,
            '_blank'
          );
        }}
        title="View patient chart"
        style={{
          flexShrink: 0,
          fontSize: 11,
          fontWeight: 500,
          padding: '4px 10px',
          ...primaryButtonStyle(color),
          borderRadius: 6,
          cursor: 'pointer',
          whiteSpace: 'nowrap',
          fontFamily: 'inherit',
        }}
      >
        View in Chart ↗
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); onExpand(patient.patient_id); }}
        title="Show triage rationale"
        style={{
          ...secondaryButtonStyle(),
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: color.secondary,
          fontSize: 16,
          padding: '0 2px',
          flexShrink: 0,
        }}
      >
        &#9432;
      </button>
    </div>
  );
}
