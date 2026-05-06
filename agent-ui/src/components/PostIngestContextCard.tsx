import { useState, type ReactElement } from 'react';
import type { GuidelineSnippet } from '../api';
import { BRAND, SURFACE } from '../styles/tokens';

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
}

const SNIPPET_PREVIEW_CHARS = 200;

function preview(content: string): string {
  if (content.length <= SNIPPET_PREVIEW_CHARS) return content;
  return `${content.slice(0, SNIPPET_PREVIEW_CHARS).trimEnd()}…`;
}

export default function PostIngestContextCard(
  props: PostIngestContextCardProps,
): ReactElement {
  const { summary, guidelines, queryUsed } = props;
  const [expanded, setExpanded] = useState<boolean>(true);
  const hasGuidelines = guidelines.length > 0;

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

      <p style={{ margin: '0 0 10px 0', lineHeight: 1.45 }}>{summary}</p>

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
