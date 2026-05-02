import type { Citation, MedicationSafetyData } from '../types';
import DisclaimerIcon from './DisclaimerIcon';
import { RED, AMB, NEU, cardStyle } from '../styles/tokens';
import { Header, SectionHeading, ClaimRow, DisclaimerFooter } from './primitives';

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
    <div style={{ fontSize: 13, color: NEU.text, fontFamily: 'inherit' }}>
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
        <div style={{ ...cardStyle(NEU), marginTop: 8 }}>
          <p style={{ margin: 0, fontSize: 13, color: NEU.text, lineHeight: 1.6 }}>{narrative}</p>
        </div>
      )}

      <DisclaimerFooter>
        <DisclaimerIcon citations={citations} />
      </DisclaimerFooter>
    </div>
  );
}
