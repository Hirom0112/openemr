import { useState, useEffect, useRef } from 'react';
import type { Citation, MedicationSafetyData } from '../types';
import { RED, AMB, NEU, MUTED } from '../styles/tokens';
import { Header, SectionHeading, ClaimRow, Markdown, CitationFooter } from './primitives';

/**
 * Canary phrases the LLM narrative may carry that the structured
 * allergies/interactions/current_medications arrays do not capture (e.g.
 * "no allergies recorded — verify in chart", "code status not documented").
 * When we suppress the narrative because structured data is present, we
 * still want these safety phrases to surface, so we extract them as
 * top-of-card claim rows. Match is case-insensitive and substring-based.
 */
const CANARY_PATTERNS: ReadonlyArray<RegExp> = [
  /allerg[^.\n]*\b(incomplete|not (?:documented|recorded|verified)|verify in (?:the )?chart|unknown)\b[^.\n]*/i,
  /code status[^.\n]*\b(not (?:documented|recorded|verified)|unknown|verify)\b[^.\n]*/i,
  /\bverify in (?:the )?chart\b[^.\n]*/i,
];

function extractCanaries(narrative: string): string[] {
  if (!narrative) return [];
  const found: string[] = [];
  const seen = new Set<string>();
  for (const pat of CANARY_PATTERNS) {
    const m = narrative.match(pat);
    if (m && m[0]) {
      const phrase = m[0].trim().replace(/^[-*•\s]+/, '');
      const key = phrase.toLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        found.push(phrase);
      }
    }
  }
  return found;
}

interface MedicationSafetyRendererProps {
  data: MedicationSafetyData;
  narrative: string;
  citations: Citation[];
  /** Patient name from response.metadata.patient_name. Renders as a prominent
   *  banner above the medication content so the physician can confirm
   *  identity at a glance (especially after pronoun resolution). */
  patientName?: string;
  /** Force-refresh callback wired to the in-bubble Refresh button.  Mirrors
   *  BriefingRenderer's onBrief({forceRefresh:true}) plumbing — the parent
   *  re-issues /medication/safety with force_refresh=true and pushes the
   *  fresh bundle as a new assistant message. */
  onRefresh?: (patientId: string) => void;
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

export default function MedicationSafetyRenderer({ data, narrative, citations, patientName, onRefresh }: MedicationSafetyRendererProps) {
  const hasAllergies = data.allergies && data.allergies.length > 0;
  const hasInteractions = data.interactions && data.interactions.length > 0;
  const hasMeds = data.current_medications && data.current_medications.length > 0;
  const count = citations?.length ?? 0;
  // Prefer the explicit patientName prop (from metadata.patient_name); fall
  // back to a name field on data if the tool surfaces one in-line. Don't
  // fabricate — render no banner if neither is present.
  const dataName = (data as unknown as { name?: string; patient_name?: string }).name
    ?? (data as unknown as { name?: string; patient_name?: string }).patient_name;
  const displayName = patientName ?? (typeof dataName === 'string' && dataName ? dataName : undefined);

  // Refresh-button state: track the generated_at value captured at click
  // time. When a fresh response arrives the parent re-renders this component
  // with a different generated_at — that change clears the pending state.
  // Mirrors BriefingRenderer's pattern verbatim.
  const [refreshingFrom, setRefreshingFrom] = useState<string | null>(null);
  const hoveredRef = useRef(false);
  const [hovered, setHovered] = useState(false);
  hoveredRef.current = hovered;

  useEffect(() => {
    if (refreshingFrom !== null && data?.generated_at && data.generated_at !== refreshingFrom) {
      setRefreshingFrom(null);
    }
  }, [data?.generated_at, refreshingFrom]);

  // Freshness colour thresholds — same as BriefingRenderer for visual
  // consistency across the chat surface.
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

  const isRefreshing = refreshingFrom !== null;
  const refreshTargetId = data.patient_id;
  const canRefresh = !!onRefresh && !!refreshTargetId && !isRefreshing;
  const handleRefresh = () => {
    if (!canRefresh || !onRefresh || !refreshTargetId) return;
    setRefreshingFrom(data.generated_at ?? '');
    onRefresh(refreshTargetId);
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
  // Header meta slot. Mirrors briefing's fallback: when generated_at is
  // missing we still render a Refresh button (no time) so legacy responses
  // keep the affordance.
  const freshnessMeta = onRefresh ? (
    <span style={{ fontSize: 11, color: stalenessColor }}>
      {generatedTimeShort && (
        <>
          <span title={generatedTooltip}>Data as of {generatedTimeShort}</span>
          <span style={{ color: MUTED }}>{'  ·  '}</span>
        </>
      )}
      <button
        type="button"
        onClick={handleRefresh}
        disabled={!canRefresh}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        aria-label="Refresh medication safety"
        style={refreshBtnStyle}
      >
        {isRefreshing ? 'Refreshing…' : 'Refresh'}
      </button>
    </span>
  ) : undefined;

  return (
    <div style={{ fontSize: 13, color: NEU.text, fontFamily: 'inherit' }}>
      <Header title="Medication safety" subtitle={displayName} meta={freshnessMeta} />
      {displayName && (
        <div
          style={{
            fontSize: 16,
            fontWeight: 600,
            color: NEU.text,
            marginTop: 2,
            marginBottom: 8,
          }}
        >
          {displayName}
        </div>
      )}

      {hasAllergies && (
        <>
          <SectionHeading color={RED}>Allergies</SectionHeading>
          {data.allergies.map((a, i) => (
            <ClaimRow key={i} color={RED}>{a}</ClaimRow>
          ))}
        </>
      )}

      {hasInteractions && (
        <>
          <SectionHeading color={AMB}>Interactions of concern</SectionHeading>
          {data.interactions.map((x, i) => (
            <ClaimRow key={i} color={AMB}>{x}</ClaimRow>
          ))}
        </>
      )}

      {hasMeds && (
        <>
          <SectionHeading color={NEU}>Current medications</SectionHeading>
          {data.current_medications.map((m, i) => (
            <ClaimRow key={i} color={NEU}>{m}</ClaimRow>
          ))}
        </>
      )}

      {/* Render the LLM narrative below the structured data. The clinician
          values the prose analysis (e.g. "Marcus is critically ill with
          sepsis; clinical context for any new medication is yours to weigh")
          even when the structured tables above already cover the bare facts.
          The earlier full-suppression behavior left the response feeling
          empty — the structured data alone doesn't carry the analysis. */}
      {narrative && (
        <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${NEU.border}` }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: NEU.secondary, textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 6 }}>
            Analysis
          </div>
          <Markdown narrative={narrative} citations={citations} />
        </div>
      )}

      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </div>
  );
}
