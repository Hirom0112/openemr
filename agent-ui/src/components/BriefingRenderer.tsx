import type { BriefingSection, BriefingResponseSection, Citation } from '../types';
import { RED, AMB, NEU, MUTED, cardStyle } from '../styles/tokens';
import type { ColorToken } from '../styles/tokens';
import { SectionHeading, ClaimRow, Pill, Markdown, CitationFooter } from './primitives';

const SECTION_META: Record<string, { label: string; color: ColorToken }> = {
  diagnosis:   { label: 'Active problems',    color: NEU },
  vitals:      { label: 'Recent vitals',      color: NEU },
  labs:        { label: 'Labs',               color: AMB },
  medications: { label: 'Medications',        color: AMB },
  allergies:   { label: 'Allergies',          color: RED },
  alerts:      { label: 'Alerts',             color: RED },
};

interface BriefingRendererProps {
  data: BriefingSection;
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

function renderSection(sec: BriefingResponseSection) {
  const meta = SECTION_META[sec.section] ?? { label: sec.section, color: NEU };
  const { label, color } = meta;
  const isVitals = sec.section === 'vitals';

  const items = sec.claims.length > 0
    ? sec.claims.map((c) => c.text)
    : sec.summary
      ? [sec.summary]
      : [];

  if (items.length === 0) return null;

  return (
    <div key={sec.section}>
      <SectionHeading color={color}>{label}</SectionHeading>
      {isVitals ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {items.map((text, i) => <Pill key={i} color={color} label={text} />)}
        </div>
      ) : (
        items.map((text, i) => <ClaimRow key={i} color={color}>{text}</ClaimRow>)
      )}
    </div>
  );
}

export default function BriefingRenderer({ data, narrative, citations }: BriefingRendererProps) {
  const count = citations?.length ?? 0;

  if (!data?.sections) {
    return (
      <>
        <Markdown narrative={narrative} citations={citations} />
        <CitationFooter count={count}>
          <CitationsList citations={citations} />
        </CitationFooter>
      </>
    );
  }

  return (
    <div style={{ fontSize: 13, fontFamily: 'inherit' }}>
      {/* Patient header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>
          {data.name}
        </div>
        {data.generated_at && (
          <div style={{ fontSize: 11, color: MUTED }}>
            {new Date(data.generated_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })}
          </div>
        )}
      </div>

      {/* Hard alerts — always shown first */}
      {data.alerts?.length > 0 && (
        <div style={{ ...cardStyle(RED), marginBottom: 10 }}>
          <div
            style={{
              fontSize: 11,
              fontWeight: 500,
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
              color: RED.text,
              marginBottom: 6,
            }}
          >
            Alerts
          </div>
          {data.alerts.map((a, i) => (
            <div key={i} style={{ fontSize: 13, color: RED.text, lineHeight: 1.5 }}>
              {a}
            </div>
          ))}
        </div>
      )}

      {/* Sections in order returned by backend */}
      {data.sections.map((sec) => renderSection(sec))}

      {/* Optional summary narrative alongside sections */}
      {narrative && (
        <div style={{ marginTop: 10 }}>
          <Markdown narrative={narrative} citations={citations} />
        </div>
      )}

      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </div>
  );
}
