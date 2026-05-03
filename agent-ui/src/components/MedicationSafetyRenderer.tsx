import type { Citation, MedicationSafetyData } from '../types';
import { RED, AMB, NEU } from '../styles/tokens';
import { Header, SectionHeading, ClaimRow, Markdown, CitationFooter } from './primitives';

interface MedicationSafetyRendererProps {
  data: MedicationSafetyData;
  narrative: string;
  citations: Citation[];
  /** Patient name from response.metadata.patient_name. Renders as a prominent
   *  banner above the medication content so the physician can confirm
   *  identity at a glance (especially after pronoun resolution). */
  patientName?: string;
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

export default function MedicationSafetyRenderer({ data, narrative, citations, patientName }: MedicationSafetyRendererProps) {
  const hasAllergies = data.allergies && data.allergies.length > 0;
  const hasInteractions = data.interactions && data.interactions.length > 0;
  const hasMeds = data.current_medications && data.current_medications.length > 0;
  const count = citations?.length ?? 0;
  // Prefer the explicit patientName prop (from metadata.patient_name); fall
  // back to a name field on data if the tool surfaces one in-line. Don't
  // fabricate — render no banner if neither is present.
  const dataName = (data as unknown as { name?: string; patient_name?: string }).name
    ?? (data as unknown as { name?: string; patient_name?: string }).patient_name;
  const displayName = patientName ?? (typeof dataName === 'string' && dataName ? dataName : undefined);

  return (
    <div style={{ fontSize: 13, color: NEU.text, fontFamily: 'inherit' }}>
      <Header title="Medication safety" subtitle={displayName} />
      {displayName && (
        <div
          style={{
            fontSize: 16,
            fontWeight: 600,
            color: NEU.text,
            marginTop: 2,
            marginBottom: 8,
          }}
        >
          {displayName}
        </div>
      )}

      {hasAllergies && (
        <>
          <SectionHeading color={RED}>Allergies</SectionHeading>
          {data.allergies.map((a, i) => (
            <ClaimRow key={i} color={RED}>{a}</ClaimRow>
          ))}
        </>
      )}

      {hasInteractions && (
        <>
          <SectionHeading color={AMB}>Interactions of concern</SectionHeading>
          {data.interactions.map((x, i) => (
            <ClaimRow key={i} color={AMB}>{x}</ClaimRow>
          ))}
        </>
      )}

      {hasMeds && (
        <>
          <SectionHeading color={NEU}>Current medications</SectionHeading>
          {data.current_medications.map((m, i) => (
            <ClaimRow key={i} color={NEU}>{m}</ClaimRow>
          ))}
        </>
      )}

      {narrative && (
        <div style={{ marginTop: 8 }}>
          <Markdown narrative={narrative} citations={citations} />
        </div>
      )}

      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </div>
  );
}
