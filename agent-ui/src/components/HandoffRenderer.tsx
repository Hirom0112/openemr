import type { Citation, HandoffData, HandoffPatient } from '../types';
import { RED, NEU, MUTED, AMB } from '../styles/tokens';
import { SectionHeading, ClaimRow, PatientRow, Markdown, CitationFooter } from './primitives';

interface HandoffRendererProps {
  data: HandoffData;
  narrative: string;
  citations: Citation[];
}

function CitationsList({ citations }: { citations: Citation[] }) {
  return (
    <ol style={{ margin: 0, paddingLeft: 18 }}>
      {citations.map((c, i) => (
        <li key={i} id={`copilot-citation-${i + 1}`} style={{ marginBottom: 2 }}>
          {c.value_summary}
          {c.effective_datetime ? ` — ${c.effective_datetime.slice(0, 10)}` : ''}
        </li>
      ))}
    </ol>
  );
}

function HandoffPatientBlock({ patient }: { patient: HandoffPatient }) {
  if (patient.pending) {
    return (
      <div style={{ marginBottom: 8 }}>
        <PatientRow
          color={NEU}
          title={patient.name}
          subtitle="Generating handoff…"
        />
        <div style={{ paddingLeft: 12, fontSize: 12, color: MUTED, fontStyle: 'italic' }}>
          Working on I-PASS summary for {patient.name}…
        </div>
      </div>
    );
  }

  if (patient.error) {
    return (
      <div style={{ marginBottom: 8 }}>
        <PatientRow
          color={AMB}
          title={patient.name}
          subtitle="Handoff unavailable"
        />
        <div style={{ paddingLeft: 12, fontSize: 12, color: AMB.text }}>
          {patient.error}
        </div>
      </div>
    );
  }

  const hasActive = patient.active_issues && patient.active_issues.length > 0;
  const hasPending = patient.pending_items && patient.pending_items.length > 0;
  const hasEscalate = patient.escalation_triggers && patient.escalation_triggers.length > 0;

  return (
    <div style={{ marginBottom: 8 }}>
      <PatientRow
        color={NEU}
        title={patient.name}
        subtitle={patient.status}
      />
      <div style={{ paddingLeft: 12 }}>
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
      </div>
    </div>
  );
}

export default function HandoffRenderer({ data, narrative, citations }: HandoffRendererProps) {
  const count = citations?.length ?? 0;

  if (!data?.patients?.length) {
    return (
      <>
        <Markdown narrative={narrative} citations={citations} />
        <CitationFooter count={count}>
          <CitationsList citations={citations} />
        </CitationFooter>
      </>
    );
  }

  return (
    <div style={{ fontSize: 13, color: NEU.text, fontFamily: 'inherit' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>Shift handoff</div>
        {data.shift_end_time && (
          <span style={{ fontSize: 11, color: MUTED }}>{data.shift_end_time}</span>
        )}
      </div>

      {data.patients.map((pt) => (
        <HandoffPatientBlock key={pt.patient_id} patient={pt} />
      ))}

      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </div>
  );
}
