import type { Citation, QueryAnswerData } from '../types';
import { NEU } from '../styles/tokens';
import { Header, Pill, SectionHeading, Markdown, CitationFooter } from './primitives';

interface QueryAnswerRendererProps {
  data: QueryAnswerData;
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

export default function QueryAnswerRenderer({ data, narrative, citations }: QueryAnswerRendererProps) {
  const text = narrative || data.answer || '';
  const count = citations?.length ?? 0;

  const windowPill = data.window_months != null
    ? <Pill color={NEU} label={`${data.window_months}mo`} />
    : undefined;

  if (data.found === false) {
    return (
      <>
        <Header title="Query answer" subtitle={data.searched} pill={windowPill} />
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
      <Markdown narrative={text} citations={citations} />
      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </>
  );
}
