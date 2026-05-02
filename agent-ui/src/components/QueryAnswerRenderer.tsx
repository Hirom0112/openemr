import type { Citation, QueryAnswerData } from '../types';
import DisclaimerIcon from './DisclaimerIcon';
import { NEU, cardStyle } from '../styles/tokens';
import { Header, Pill, SectionHeading, DisclaimerFooter } from './primitives';

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
        <div style={cardStyle(NEU)}>
          <p style={{ margin: 0, fontSize: 13, color: NEU.text, lineHeight: 1.6 }}>{text}</p>
        </div>
        <DisclaimerFooter>
          <DisclaimerIcon citations={citations} />
        </DisclaimerFooter>
      </div>
    );
  }

  return (
    <div>
      <Header title="Query answer" subtitle={data.searched} pill={windowPill} />
      <div style={cardStyle(NEU)}>
        <p style={{ margin: 0, fontSize: 13, color: NEU.text, lineHeight: 1.6 }}>{text}</p>
      </div>
      <DisclaimerFooter>
        <DisclaimerIcon citations={citations} />
      </DisclaimerFooter>
    </div>
  );
}
