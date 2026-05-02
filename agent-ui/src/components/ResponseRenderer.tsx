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
import { cardStyle, RED } from '../styles/tokens';

interface ResponseRendererProps {
  response: AgentResponse;
  onBrief?: (patientName: string, patientId?: string) => void;
  onMeds?: (patientName: string, patientId?: string) => void;
  providerName?: string;
}

export default function ResponseRenderer({ response, onBrief, onMeds, providerName }: ResponseRendererProps) {
  const { type, data, narrative, citations } = response;

  if (type === 'error') {
    return (
      <div style={{ ...cardStyle(RED), fontSize: 13, color: RED.text }}>
        {narrative || 'An error occurred. Please view the chart directly.'}
      </div>
    );
  }

  switch (type) {
    case 'census':
      return (
        <CensusRenderer
          data={data as CensusData}
          narrative={narrative}
          citations={citations}
          onBrief={onBrief ?? (() => {})}
          onMeds={onMeds ?? (() => {})}
          providerName={providerName}
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
    default:
      return <TextRenderer narrative={narrative} citations={citations} />;
  }
}
