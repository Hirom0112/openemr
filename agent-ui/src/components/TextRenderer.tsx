import type { Citation } from '../types';
import { Header, Markdown, CitationFooter } from './primitives';

interface TextRendererProps {
  narrative: string;
  citations: Citation[];
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

export default function TextRenderer({ narrative, citations }: TextRendererProps) {
  const showHeader = (narrative?.length ?? 0) > 400;
  const count = citations?.length ?? 0;
  return (
    <>
      {showHeader && <Header title="Answer" />}
      <Markdown narrative={narrative} citations={citations} />
      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </>
  );
}
