import { useState, useEffect, useRef } from 'react';
import type { BriefingSection, BriefingResponseSection, Citation } from '../types';
import { RED, AMB, NEU, MUTED } from '../styles/tokens';
import type { ColorToken } from '../styles/tokens';
import { Header, SectionHeading, ClaimRow, Pill, Markdown, CitationFooter } from './primitives';
import { formatFriendly, formatFriendlyWithSeconds } from '../utils/datetime';

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
  onBrief?: (patientName: string, patientId?: string, options?: { forceRefresh?: boolean }) => void;
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

export default function BriefingRenderer({ data, narrative, citations, onBrief }: BriefingRendererProps) {
  const count = citations?.length ?? 0;

  // Track the generated_at value captured when the user clicked Refresh.
  // When a fresh briefing arrives the parent re-renders this component with
  // a new generated_at — that change clears the pending state.
  const [refreshingFrom, setRefreshingFrom] = useState<string | null>(null);
  const hoveredRef = useRef(false);
  const [hovered, setHovered] = useState(false);
  hoveredRef.current = hovered;

  useEffect(() => {
    if (refreshingFrom !== null && data?.generated_at && data.generated_at !== refreshingFrom) {
      setRefreshingFrom(null);
    }
  }, [data?.generated_at, refreshingFrom]);

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

  const generatedTimeShort = generatedValid ? formatFriendly(generatedDate) : undefined;
  const generatedTooltip = generatedValid
    ? `Last fetched from chart at ${formatFriendlyWithSeconds(generatedDate)}`
    : undefined;

  const isRefreshing = refreshingFrom !== null;
  const canRefresh = !!onBrief && !!data.patient_id && !!data.name && !isRefreshing;
  const handleRefresh = () => {
    if (!canRefresh || !onBrief) return;
    setRefreshingFrom(data.generated_at ?? '');
    onBrief(data.name, data.patient_id, { forceRefresh: true });
  };
  const refreshBtnStyle: React.CSSProperties = {
    background: hovered && canRefresh ? '#f3f4f6' : 'transparent',
    border: 'none',
    padding: '0 4px',
    margin: 0,
    fontFamily: 'inherit',
    fontSize: 11,
    color: canRefresh ? MUTED : '#9ca3af',
    cursor: canRefresh ? 'pointer' : 'not-allowed',
    borderRadius: 4,
    lineHeight: 'inherit',
  };
  const freshnessMeta = generatedTimeShort ? (
    <span style={{ fontSize: 11, color: stalenessColor }}>
      <span title={generatedTooltip}>Data as of {generatedTimeShort}</span>
      <span style={{ color: MUTED }}>{'  ·  '}</span>
      <button
        type="button"
        onClick={handleRefresh}
        disabled={!canRefresh}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        aria-label={`Refresh briefing for ${data.name}`}
        style={refreshBtnStyle}
      >
        {isRefreshing ? 'Refreshing…' : 'Refresh'}
      </button>
    </span>
  ) : undefined;

  return (
    <div style={{ fontSize: 13, fontFamily: 'inherit' }}>
      {/* Patient header */}
      <Header
        title={data.name}
        meta={freshnessMeta}
      />

      {/* Executive summary — 2-3 sentence framing the rounding clinician
          can read at a glance, like the patient_summary line in handoff
          rows. Lives ABOVE Alerts/Sections so it's the first thing the
          eye lands on. Sourced from the LLM's top-level `summary` field
          which api.ts/getBriefing lifts into AgentResponse.narrative. */}
      {narrative && (
        <div style={{ margin: '8px 0 12px' }}>
          <div
            style={{
              background: NEU.bg,
              border: `1px solid ${NEU.border}`,
              borderRadius: 6,
              padding: '10px 12px',
              fontSize: 13,
              lineHeight: 1.5,
              color: NEU.text,
            }}
          >
            <Markdown narrative={narrative} citations={citations} />
          </div>
          <div
            style={{
              fontSize: 11,
              color: MUTED,
              marginTop: 4,
              fontStyle: 'italic',
              paddingLeft: 2,
            }}
            aria-label="AI-generated summary disclaimer"
          >
            AI-generated summary — verify against chart before clinical decisions
          </div>
        </div>
      )}

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

      {/* Narrative is a fallback only — when structured sections or alerts
          are present, the prose duplicates the same facts (and the alerts
          array already surfaces any safety canaries). Suppress to avoid
          double-rendering. */}
      {!(data.sections?.length > 0 || data.alerts?.length > 0) && narrative && (
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
