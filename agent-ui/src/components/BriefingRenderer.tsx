import type { BriefingSection, BriefingResponseSection, Citation } from '../types';
import { RED, AMB, NEU, MUTED } from '../styles/tokens';
import type { ColorToken } from '../styles/tokens';
import { Header, SectionHeading, ClaimRow, Pill, Markdown, CitationFooter } from './primitives';

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

  // Data freshness indicator. The briefing's `generated_at` is the only
  // wire-format timestamp available — it reflects when the agent assembled
  // the briefing from the (possibly cached) FHIR bundle. Sara needs to
  // distinguish a fresh fetch from a 30-min Redis cache hit.
  const STALE_AMBER_MS = 10 * 60 * 1000;  // >10 min → amber
  const STALE_RED_MS = 30 * 60 * 1000;    // >30 min → red

  const generatedDate = data.generated_at ? new Date(data.generated_at) : null;
  const generatedValid = generatedDate && !Number.isNaN(generatedDate.getTime());
  const ageMs = generatedValid ? Date.now() - generatedDate.getTime() : 0;
  const stalenessColor =
    !generatedValid ? MUTED
      : ageMs > STALE_RED_MS ? RED.text
      : ageMs > STALE_AMBER_MS ? AMB.text
      : MUTED;

  const generatedTimeShort = generatedValid
    ? generatedDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : undefined;
  const generatedTooltip = generatedValid
    ? `Last fetched from chart at ${generatedDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })} on ${generatedDate.toISOString().slice(0, 10)}`
    : undefined;

  // TODO: wire to onBrief(data.name, data.patient_id) — callback isn't plumbed
  // through ResponseRenderer → BriefingRenderer yet. Until then, "Refresh" is
  // shown as muted hint text (not interactive) so it doesn't lie to the user.
  const freshnessMeta = generatedTimeShort ? (
    <span style={{ fontSize: 11, color: stalenessColor }}>
      <span title={generatedTooltip}>Data as of {generatedTimeShort}</span>
      <span style={{ color: MUTED }}>{'  ·  '}</span>
      <span style={{ color: MUTED }}>Refresh</span>
    </span>
  ) : undefined;

  return (
    <div style={{ fontSize: 13, fontFamily: 'inherit' }}>
      {/* Patient header */}
      <Header
        title={data.name}
        meta={freshnessMeta}
      />

      {/* Hard alerts — always shown first */}
      {data.alerts?.length > 0 && (
        <>
          <SectionHeading color={RED}>Alerts</SectionHeading>
          {data.alerts.map((a, i) => (
            <ClaimRow key={i} color={RED}>{a}</ClaimRow>
          ))}
        </>
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
