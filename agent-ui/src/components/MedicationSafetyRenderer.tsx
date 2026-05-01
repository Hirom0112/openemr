import type { Citation, MedicationSafetyData } from '../types';
import DisclaimerIcon from './DisclaimerIcon';
import { RED, AMB, NEU } from '../styles/tokens';
import { Header, SectionHeading, ClaimRow } from './primitives';

interface MedicationSafetyRendererProps {
  data: MedicationSafetyData;
  narrative: string;
  citations: Citation[];
}

export default function MedicationSafetyRenderer({ data, narrative, citations }: MedicationSafetyRendererProps) {
  const hasAllergies = data.allergies && data.allergies.length > 0;
  const hasInteractions = data.interactions && data.interactions.length > 0;
  const hasMeds = data.current_medications && data.current_medications.length > 0;

  return (
    <div style={{ fontSize: 13, color: '#374151', fontFamily: 'inherit' }}>
      <Header title="Medication safety" />

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
        <p style={{ margin: '8px 0 0', fontSize: 13, color: '#374151', lineHeight: 1.6 }}>{narrative}</p>
      )}

      <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid #e5e7eb', display: 'flex', justifyContent: 'flex-end' }}>
        <DisclaimerIcon citations={citations} />
      </div>
    </div>
  );
}
