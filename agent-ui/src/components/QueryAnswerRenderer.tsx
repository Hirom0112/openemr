import type { Citation, QueryAnswerData } from '../types';
import { NEU } from '../styles/tokens';
import { Header, Pill, SectionHeading, Markdown, CitationFooter } from './primitives';

interface QueryAnswerRendererProps {
  data: QueryAnswerData;
  narrative: string;
  citations: Citation[];
  /** Patient name from response.metadata.patient_name. The query_answer data
   *  shape carries no patient identity, so we accept it as a prop and render
   *  a prominent banner above the answer when present. */
  patientName?: string;
}

function PatientBanner({ name }: { name: string }) {
  return (
    <div
      style={{
        fontSize: 16,
        fontWeight: 600,
        color: NEU.text,
        marginTop: 2,
        marginBottom: 8,
      }}
    >
      {name}
    </div>
  );
}

function CitationsList({ citations }: { citations: Citation[] }) {
  return (
    <ol style={{ margin: 0, paddingLeft: 18 }}>
      {citations.map((c, i) => (
        <li key={i} id={`copilot-citation-${i + 1}`} style={{ marginBottom: 2 }}>
          {c.value_summary}
          {c.effective_datetime ? ` · ${c.effective_datetime.slice(0, 10)}` : ''}
        </li>
      ))}
    </ol>
  );
}

export default function QueryAnswerRenderer({ data, narrative, citations, patientName }: QueryAnswerRendererProps) {
  // Prefer the structured `data.answer` from the tool — it is the
  // authoritative response. The LLM narrative typically restates the same
  // information and would otherwise render twice. Fall back to narrative
  // only when no structured answer is present.
  const hasAnswer = typeof data.answer === 'string' && data.answer.trim().length > 0;
  const text = hasAnswer ? data.answer : (narrative || '');
  const count = citations?.length ?? 0;

  const windowPill = data.window_months != null
    ? <Pill color={NEU} label={`${data.window_months}mo`} />
    : undefined;

  if (data.found === false) {
    return (
      <>
        <Header title="Query answer" subtitle={data.searched} pill={windowPill} />
        {patientName && <PatientBanner name={patientName} />}
        <SectionHeading color={NEU}>No results found</SectionHeading>
        <Markdown narrative={text} citations={citations} />
        <CitationFooter count={count}>
          <CitationsList citations={citations} />
        </CitationFooter>
      </>
    );
  }

  return (
    <>
      <Header title="Query answer" subtitle={data.searched} pill={windowPill} />
      {patientName && <PatientBanner name={patientName} />}
      <Markdown narrative={text} citations={citations} />
      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </>
  );
}
