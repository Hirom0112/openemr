import type { Citation } from '../types';

interface TextRendererProps {
  narrative: string;
  citations: Citation[];
}

export default function TextRenderer({ narrative }: TextRendererProps) {
  return (
    <div>
      <p style={{ margin: 0, fontSize: 13, color: '#374151', lineHeight: 1.6 }}>{narrative}</p>
    </div>
  );
}
