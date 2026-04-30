import type {
  AgentResponse,
  BriefingSection,
  CensusData,
  HandoffData,
  MedicationSafetyData,
  QueryAnswerData,
} from '../types';
import CensusRenderer from './CensusRenderer';
import BriefingRenderer from './BriefingRenderer';
import QueryAnswerRenderer from './QueryAnswerRenderer';
import MedicationSafetyRenderer from './MedicationSafetyRenderer';
import HandoffRenderer from './HandoffRenderer';
import TextRenderer from './TextRenderer';

interface ResponseRendererProps {
  response: AgentResponse;
  onPatientSelect?: (patientId: string) => void;
}

export default function ResponseRenderer({ response, onPatientSelect }: ResponseRendererProps) {
  const { type, data, narrative, citations } = response;

  switch (type) {
    case 'census':
      return (
        <CensusRenderer
          data={data as CensusData}
          narrative={narrative}
          citations={citations}
          onPatientSelect={onPatientSelect}
        />
      );
    case 'briefing':
      return (
        <BriefingRenderer
          data={data as BriefingSection}
          narrative={narrative}
          citations={citations}
        />
      );
    case 'query_answer':
      return (
        <QueryAnswerRenderer
          data={data as QueryAnswerData}
          narrative={narrative}
          citations={citations}
        />
      );
    case 'medication_safety':
      return (
        <MedicationSafetyRenderer
          data={data as MedicationSafetyData}
          narrative={narrative}
          citations={citations}
        />
      );
    case 'handoff':
      return (
        <HandoffRenderer
          data={data as HandoffData}
          narrative={narrative}
          citations={citations}
        />
      );
    case 'error':
      return (
        <div style={{ color: '#c0392b', fontSize: 13, padding: '6px 8px', background: '#fff5f5', borderRadius: 4 }}>
          {narrative || 'An error occurred. Please view the chart directly.'}
        </div>
      );
    default:
      return <TextRenderer narrative={narrative} citations={citations} />;
  }
}
