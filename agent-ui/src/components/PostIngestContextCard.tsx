import { Fragment, useState, type ReactElement, type ReactNode } from 'react';
import type { GuidelineSnippet, SynthesisOutput } from '../api';
import { BRAND, SURFACE } from '../styles/tokens';
import { parseCitationTokens } from '../utils/citationParser';
import SynthesisCitationChip from './SynthesisCitationChip';

/**
 * Renders the post-ingest "clinical context" card surfaced after a
 * document is ingested. Shows the LLM-rendered summary, the underlying
 * RAG query, and a collapsible list of supporting guideline snippets.
 *
 * Citation badges use the same `G:<chunk_id>` shape that downstream chat
 * answers reference, so the visual identity matches when the user asks a
 * follow-up question and the answer cites these same chunks.
 */

export interface PostIngestContextCardProps {
  summary: string;
  guidelines: GuidelineSnippet[];
  queryUsed: string;
  /** When provided, the 4-section synthesis is rendered above the summary +
   *  guidelines. Null/undefined falls back to the deterministic-recap view. */
  synthesis?: SynthesisOutput | null;
  /** When provided, citation chips become clickable buttons. The handler
   *  receives the raw token id (e.g. "fact:obs:42", "guideline:abc"). When
   *  absent, chips render as inert spans (legacy behavior). */
  onCitationClick?: (citationId: string) => void;
}

const SNIPPET_PREVIEW_CHARS = 200;

function preview(content: string): string {
  if (content.length <= SNIPPET_PREVIEW_CHARS) return content;
  return `${content.slice(0, SNIPPET_PREVIEW_CHARS).trimEnd()}…`;
}

const CITATION_CHIP_STYLE: React.CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: 10,
  padding: '1px 5px',
  borderRadius: 4,
  background: BRAND.base,
  color: BRAND.onBrand,
  marginLeft: 4,
  whiteSpace: 'nowrap',
};

/**
 * Render a narrative string with embedded citation tokens
 * (`[fact:obs:*]`, `[fact:intake:*]`, `[guideline:*]`) parsed into
 * clickable chips when `onCitationClick` is provided. Falls back to plain
 * text when no handler is wired (defensive — preserves existing reads).
 */
function renderProse(
  text: string,
  onCitationClick?: (citationId: string) => void,
  keyPrefix = 'p',
): ReactNode {
  if (!onCitationClick) return text;
  const parts = parseCitationTokens(text, (id) => (
    <SynthesisCitationChip
      key={`${keyPrefix}-chip-${id}`}
      citationId={id}
      onClick={onCitationClick}
      title={id.startsWith('guideline:') ? id.slice('guideline:'.length) : undefined}
    />
  ));
  return parts.map((node, i) => (
    <Fragment key={`${keyPrefix}-${i}`}>{node}</Fragment>
  ));
}

function renderCitations(
  ids: string[],
  onCitationClick?: (citationId: string) => void,
): ReactNode {
  return ids.map((id) => {
    if (onCitationClick) {
      const title = id.startsWith('guideline:')
        ? id.slice('guideline:'.length)
        : undefined;
      return (
        <SynthesisCitationChip
          key={id}
          citationId={id}
          onClick={onCitationClick}
          title={title}
        />
      );
    }
    return (
      <span key={id} style={CITATION_CHIP_STYLE}>
        {id}
      </span>
    );
  });
}

function SynthesisSection(props: {
  synthesis: SynthesisOutput;
  onCitationClick?: (citationId: string) => void;
}): ReactElement {
  const { synthesis, onCitationClick } = props;
  return (
    <div style={{ marginBottom: 10 }}>
      {synthesis.approved_facts.trim() && (
        <p style={{ margin: '0 0 10px 0', lineHeight: 1.45 }}>
          {renderProse(synthesis.approved_facts, onCitationClick, 'af')}
        </p>
      )}

      {synthesis.clinical_signals.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: SURFACE.muted,
              letterSpacing: '0.02em',
              marginBottom: 4,
            }}
          >
            Clinical signals
          </div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {synthesis.clinical_signals.map((s, i) => (
              <li key={i} style={{ padding: '4px 0', lineHeight: 1.45 }}>
                {renderProse(s.claim, onCitationClick, `cs-${i}`)}
                {renderCitations(s.citation_ids, onCitationClick)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {synthesis.guideline_mappings.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: SURFACE.muted,
              letterSpacing: '0.02em',
              marginBottom: 4,
            }}
          >
            Guideline mappings
          </div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {synthesis.guideline_mappings.map((m, i) => (
              <li key={i} style={{ padding: '4px 0', lineHeight: 1.45 }}>
                {renderProse(m.claim, onCitationClick, `gm-${i}`)}
                {renderCitations([`guideline:${m.chunk_id}`], onCitationClick)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {synthesis.next_steps.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: SURFACE.muted,
              letterSpacing: '0.02em',
              marginBottom: 4,
            }}
          >
            Suggested next steps
          </div>
          <ul style={{ paddingLeft: 18, margin: 0 }}>
            {synthesis.next_steps.map((step, i) => (
              <li key={i} style={{ padding: '2px 0', lineHeight: 1.45 }}>
                {renderProse(step, onCitationClick, `ns-${i}`)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function PostIngestContextCard(
  props: PostIngestContextCardProps,
): ReactElement {
  const { summary, guidelines, queryUsed, synthesis, onCitationClick } = props;
  const [expanded, setExpanded] = useState<boolean>(true);
  const hasGuidelines = guidelines.length > 0;
  const hasSynthesis =
    !!synthesis &&
    (synthesis.approved_facts.trim().length > 0 ||
      synthesis.clinical_signals.length > 0 ||
      synthesis.guideline_mappings.length > 0 ||
      synthesis.next_steps.length > 0);

  return (
    <div
      style={{
        border: `1px solid ${SURFACE.border}`,
        borderRadius: 8,
        padding: '12px 14px',
        background: SURFACE.panel,
        fontSize: 13,
        color: SURFACE.fg,
      }}
    >
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: SURFACE.muted,
          letterSpacing: '0.02em',
          marginBottom: 6,
        }}
      >
        Clinical context
      </div>

      {hasSynthesis ? (
        <SynthesisSection
          synthesis={synthesis as SynthesisOutput}
          onCitationClick={onCitationClick}
        />
      ) : (
        <p style={{ margin: '0 0 10px 0', lineHeight: 1.45 }}>{summary}</p>
      )}

      {!hasGuidelines && (
        <div style={{ color: SURFACE.subtle, fontSize: 12, fontStyle: 'italic' }}>
          No matching guidelines found.
        </div>
      )}

      {hasGuidelines && (
        <div>
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            style={{
              background: 'transparent',
              border: 'none',
              padding: '2px 0',
              fontSize: 12,
              fontWeight: 600,
              color: SURFACE.fg,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            <span aria-hidden="true" style={{ marginRight: 6 }}>{expanded ? '▾' : '▸'}</span>
            Relevant guidelines ({guidelines.length})
          </button>
          {expanded && (
            <ul style={{ listStyle: 'none', padding: 0, margin: '6px 0 0 0' }}>
              {guidelines.map((g) => {
                const locParts: string[] = [];
                if (g.section) locParts.push(g.section);
                if (typeof g.page_number === 'number') locParts.push(`p${g.page_number}`);
                const loc = locParts.join(' · ');
                return (
                  <li
                    key={g.chunk_id}
                    style={{
                      borderLeft: `2px solid ${SURFACE.border}`,
                      padding: '6px 0 6px 10px',
                      margin: '6px 0',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
                      <span
                        style={{
                          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                          fontSize: 10,
                          padding: '1px 5px',
                          borderRadius: 4,
                          background: BRAND.base,
                          color: BRAND.onBrand,
                        }}
                      >
                        G:{g.chunk_id}
                      </span>
                      <span style={{ fontWeight: 600 }}>{g.document_title}</span>
                      {loc && (
                        <span style={{ color: SURFACE.muted, fontSize: 12 }}>· {loc}</span>
                      )}
                    </div>
                    <div style={{ color: SURFACE.fg, lineHeight: 1.4 }}>{preview(g.content)}</div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}

      <div
        style={{
          marginTop: 10,
          fontSize: 11,
          color: SURFACE.subtle,
          fontStyle: 'italic',
        }}
      >
        Searched: &ldquo;{queryUsed}&rdquo;
      </div>
    </div>
  );
}
