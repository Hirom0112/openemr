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
  onBrief?: (patientName: string, patientId?: string, options?: { forceRefresh?: boolean }) => void;
  onMeds?: (patientName: string, patientId?: string) => void;
  onHandoff?: (patientIds: string[], patientNames: Record<string, string>) => void;
  handoffInFlight?: boolean;
  providerName?: string;
  /** Refresh the census (force_refresh=true). Wired to the Refresh button in CensusRenderer. */
  onRefreshCensus?: () => void;
  /** Refresh the medication-safety bubble (force_refresh=true). Wired to
   *  the Refresh button in MedicationSafetyRenderer — re-issues
   *  /medication/safety with force_refresh=true so the bundle cache is
   *  bypassed and a fresh generated_at is stamped. */
  onRefreshMedicationSafety?: (patientId: string) => void;
  /** Patient name surfaced by the dispatcher (response.metadata.patient_name).
   *  Used by free-text renderers (query_answer, medication_safety) to render a
   *  prominent banner so the physician can confirm patient identity at a glance. */
  patientName?: string;
  /** sessionId — passed through to CensusRenderer so it can poll
   *  /agent/prefetch/status and surface ⚡/⏳ pills next to each row. */
  sessionId?: string;
}

export default function ResponseRenderer({ response, onBrief, onMeds, onHandoff, handoffInFlight, providerName, patientName, sessionId, onRefreshCensus, onRefreshMedicationSafety }: ResponseRendererProps) {
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
          onHandoff={onHandoff}
          handoffInFlight={handoffInFlight}
          providerName={providerName}
          onRefresh={onRefreshCensus}
          sessionId={sessionId}
        />
      );
    case 'briefing':
      return (
        <BriefingRenderer
          data={data as BriefingSection}
          narrative={narrative}
          citations={citations}
          onBrief={onBrief}
        />
      );
    case 'query_answer':
      return (
        <QueryAnswerRenderer
          data={data as QueryAnswerData}
          narrative={narrative}
          citations={citations}
          patientName={patientName}
        />
      );
    case 'medication_safety':
      return (
        <MedicationSafetyRenderer
          data={data as MedicationSafetyData}
          narrative={narrative}
          citations={citations}
          patientName={patientName}
          onRefresh={onRefreshMedicationSafety}
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
