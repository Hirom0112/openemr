import type { Citation } from '../types';
import CitationLink from './CitationLink';

interface TextRendererProps {
  narrative: string;
  citations: Citation[];
}

export default function TextRenderer({ narrative, citations }: TextRendererProps) {
  return (
    <div style={{ fontSize: 13, color: '#333', lineHeight: 1.5 }}>
      <p style={{ margin: '0 0 6px' }}>{narrative}</p>
      {citations.length > 0 && (
        <div style={{ marginTop: 4 }}>
          {citations.map((c, i) => (
            <CitationLink key={`${c.resource_id}-${i}`} citation={c} />
          ))}
        </div>
      )}
    </div>
  );
}
