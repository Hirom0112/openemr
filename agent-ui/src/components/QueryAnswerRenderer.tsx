import type { Citation, QueryAnswerData } from '../types';
import CitationLink from './CitationLink';

interface QueryAnswerRendererProps {
  data: QueryAnswerData;
  narrative: string;
  citations: Citation[];
}

export default function QueryAnswerRenderer({ data, narrative, citations }: QueryAnswerRendererProps) {
  if (data.found === false) {
    return (
      <div style={{ fontSize: 13, color: '#555' }}>
        <div style={{ color: '#888', fontStyle: 'italic', marginBottom: 4 }}>
          No {data.searched ?? 'result'} found
          {data.window_months ? ` in the last ${data.window_months} months` : ''}.
        </div>
        <p style={{ margin: 0 }}>{narrative}</p>
      </div>
    );
  }

  return (
    <div style={{ fontSize: 13, color: '#333', lineHeight: 1.5 }}>
      <p style={{ margin: '0 0 6px' }}>{narrative}</p>
      {citations.length > 0 && (
        <div style={{ marginTop: 4, display: 'flex', flexWrap: 'wrap', gap: 2 }}>
          {citations.map((c, i) => (
            <CitationLink key={`${c.resource_id}-${i}`} citation={c} />
          ))}
        </div>
      )}
    </div>
  );
}
