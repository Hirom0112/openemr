import type { PatientSummary } from '../types';

const PRIORITY_COLORS: Record<string, string> = {
  P1: '#c0392b', P2: '#e74c3c', P3: '#e67e22', P4: '#f39c12',
  P5: '#d4ac0d', P6: '#27ae60', P7: '#1abc9c', P8: '#2980b9',
  P9: '#8e44ad', P10: '#7f8c8d',
};

interface PatientCardProps {
  patient: PatientSummary;
  rank: number;
  selected: boolean;
  onExpand: (patientId: string) => void;
  onSelect: (patientId: string) => void;
}

export default function PatientCard({ patient, rank, selected, onExpand, onSelect }: PatientCardProps) {
  const color = PRIORITY_COLORS[patient.priority] ?? '#7f8c8d';

  return (
    <div
      onClick={() => onSelect(patient.patient_id)}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        padding: '8px 10px',
        marginBottom: 5,
        borderRadius: 4,
        border: '1px solid #e0e0e0',
        borderLeft: `4px solid ${color}`,
        background: selected ? '#f0f4ff' : '#fff',
        cursor: 'pointer',
      }}
    >
      <span
        style={{
          minWidth: 26,
          height: 26,
          borderRadius: '50%',
          background: color,
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
          <strong style={{ fontSize: 13 }}>{patient.name}</strong>
          <span style={{ fontSize: 11, color: '#666', marginLeft: 6, flexShrink: 0 }}>
            {patient.bed} &middot; <span style={{ color }}>{patient.priority}</span>
          </span>
        </div>
        <div style={{ fontSize: 12, color: '#444', marginTop: 2 }}>{patient.one_line}</div>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          window.open(
            `${window.location.origin}/interface/patient_file/summary/demographics_full.php?set_pid=${patient.patient_id}`,
            '_blank'
          );
        }}
        title="View patient chart"
        style={{
          flexShrink: 0,
          fontSize: 11,
          fontWeight: 600,
          padding: '3px 9px',
          background: '#2c3e9e',
          color: '#fff',
          borderRadius: 4,
          border: 'none',
          cursor: 'pointer',
          whiteSpace: 'nowrap',
          letterSpacing: 0.1,
          fontFamily: 'inherit',
        }}
      >
        View in Chart ↗
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); onExpand(patient.patient_id); }}
        title="Show triage rationale"
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: '#999',
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
