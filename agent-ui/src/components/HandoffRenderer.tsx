import type { Citation, HandoffData, HandoffPatient } from '../types';
import DisclaimerIcon from './DisclaimerIcon';
import { RED, NEU, MUTED } from '../styles/tokens';
import { SectionHeading, ClaimRow } from './primitives';

interface HandoffRendererProps {
  data: HandoffData;
  narrative: string;
  citations: Citation[];
}

function PatientCard({ patient }: { patient: HandoffPatient }) {
  const hasActive = patient.active_issues && patient.active_issues.length > 0;
  const hasPending = patient.pending_items && patient.pending_items.length > 0;
  const hasEscalate = patient.escalation_triggers && patient.escalation_triggers.length > 0;

  return (
    <ClaimRow color={NEU}>
      <div style={{ fontSize: 13, fontWeight: 500, color: '#111' }}>{patient.name}</div>
      <div style={{ fontSize: 12, color: MUTED, marginTop: 2, marginBottom: hasActive || hasPending || hasEscalate ? 8 : 0 }}>
        {patient.status}
      </div>

      {hasActive && (
        <>
          <SectionHeading color={RED}>Active issues</SectionHeading>
          {patient.active_issues.map((it, i) => (
            <ClaimRow key={i} color={RED}>{it}</ClaimRow>
          ))}
        </>
      )}

      {hasPending && (
        <>
          <SectionHeading color={NEU}>Pending</SectionHeading>
          {patient.pending_items.map((it, i) => (
            <ClaimRow key={i} color={NEU}>{it}</ClaimRow>
          ))}
        </>
      )}

      {hasEscalate && (
        <>
          <SectionHeading color={RED}>Escalate if</SectionHeading>
          {patient.escalation_triggers.map((t, i) => (
            <ClaimRow key={i} color={RED}>{t}</ClaimRow>
          ))}
        </>
      )}
    </ClaimRow>
  );
}

export default function HandoffRenderer({ data, narrative }: HandoffRendererProps) {
  if (!data?.patients?.length) {
    return <p style={{ fontSize: 13, color: '#374151', margin: 0, lineHeight: 1.6 }}>{narrative}</p>;
  }

  return (
    <div style={{ fontSize: 13, color: '#374151', fontFamily: 'inherit' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>Shift handoff</div>
        {data.shift_end_time && (
          <span style={{ fontSize: 11, color: MUTED }}>{data.shift_end_time}</span>
        )}
      </div>

      {data.patients.map((pt) => (
        <PatientCard key={pt.patient_id} patient={pt} />
      ))}

      <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid #e5e7eb', display: 'flex', justifyContent: 'flex-end' }}>
        <DisclaimerIcon />
      </div>
    </div>
  );
}
