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
import CitationsPanel from './CitationsPanel';
import { cardStyle, RED } from '../styles/tokens';

interface ResponseRendererProps {
  response: AgentResponse;
  onBrief?: (patientName: string, patientId?: string) => void;
  providerName?: string;
}

export default function ResponseRenderer({ response, onBrief, providerName }: ResponseRendererProps) {
  const { type, data, narrative, citations } = response;

  if (type === 'error') {
    return (
      <div style={{ ...cardStyle(RED), fontSize: 13, color: RED.text }}>
        {narrative || 'An error occurred. Please view the chart directly.'}
      </div>
    );
  }

  let body: React.ReactNode;
  switch (type) {
    case 'census':
      body = (
        <CensusRenderer
          data={data as CensusData}
          narrative={narrative}
          citations={citations}
          onBrief={onBrief ?? (() => {})}
          providerName={providerName}
        />
      );
      break;
    case 'briefing':
      body = (
        <BriefingRenderer
          data={data as BriefingSection}
          narrative={narrative}
          citations={citations}
        />
      );
      break;
    case 'query_answer':
      body = (
        <QueryAnswerRenderer
          data={data as QueryAnswerData}
          narrative={narrative}
          citations={citations}
        />
      );
      break;
    case 'medication_safety':
      body = (
        <MedicationSafetyRenderer
          data={data as MedicationSafetyData}
          narrative={narrative}
          citations={citations}
        />
      );
      break;
    case 'handoff':
      body = (
        <HandoffRenderer
          data={data as HandoffData}
          narrative={narrative}
          citations={citations}
        />
      );
      break;
    default:
      body = <TextRenderer narrative={narrative} citations={citations} />;
  }

  if (type === 'census') {
    // census renders its own inline source attribution — skip CitationsPanel to avoid double render
    return <>{body}</>;
  }

  const patientOrder = undefined;

  return (
    <>
      {body}
      <CitationsPanel citations={citations} patientOrder={patientOrder} />
    </>
  );
}
