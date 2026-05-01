import type { Citation, QueryAnswerData } from '../types';
import DisclaimerIcon from './DisclaimerIcon';
import { NEU } from '../styles/tokens';
import { Header, Pill, SectionHeading } from './primitives';

interface QueryAnswerRendererProps {
  data: QueryAnswerData;
  narrative: string;
  citations: Citation[];
}

export default function QueryAnswerRenderer({ data, narrative, citations }: QueryAnswerRendererProps) {
  const text = narrative || data.answer || '';

  const windowPill = data.window_months != null
    ? <Pill color={NEU} label={`${data.window_months}mo`} />
    : undefined;

  if (data.found === false) {
    return (
      <div>
        <Header title="Query answer" subtitle={data.searched} pill={windowPill} />
        <SectionHeading color={NEU}>No results found</SectionHeading>
        <p style={{ margin: 0, fontSize: 13, color: '#374151', lineHeight: 1.6 }}>{text}</p>
        <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid #e5e7eb', display: 'flex', justifyContent: 'flex-end' }}>
          <DisclaimerIcon citations={citations} />
        </div>
      </div>
    );
  }

  return (
    <div>
      <Header title="Query answer" subtitle={data.searched} pill={windowPill} />
      <p style={{ margin: 0, fontSize: 13, color: '#374151', lineHeight: 1.6 }}>{text}</p>
      <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid #e5e7eb', display: 'flex', justifyContent: 'flex-end' }}>
        <DisclaimerIcon citations={citations} />
      </div>
    </div>
  );
}
