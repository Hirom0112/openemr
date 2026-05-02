import type { Citation } from '../types';
import { NEU, cardStyle } from '../styles/tokens';

interface TextRendererProps {
  narrative: string;
  citations: Citation[];
}

export default function TextRenderer({ narrative }: TextRendererProps) {
  return (
    <div style={cardStyle(NEU)}>
      <p style={{ margin: 0, fontSize: 13, color: NEU.text, lineHeight: 1.6 }}>{narrative}</p>
    </div>
  );
}
