import { describe, test, expect } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import PostIngestContextCard from './PostIngestContextCard';
import type { GuidelineSnippet } from '../api';

/**
 * Pure-render regression spec for PostIngestContextCard. The agent-ui repo
 * does not ship @testing-library/react or jsdom, so we use react-dom/server's
 * `renderToStaticMarkup` (which only requires the already-installed
 * react-dom dep) to render the card to a static HTML string and assert on
 * the resulting markup. Hooks like useState run normally under SSR — the
 * initial state is rendered, which is what we want for the visible-text
 * assertions below.
 */

function makeGuideline(overrides: Partial<GuidelineSnippet> = {}): GuidelineSnippet {
  return {
    chunk_id: 'chunk_abc',
    source_id: 'src_1',
    document_title: 'KDIGO 2024',
    section: '3.2 Acute kidney injury',
    page_number: 12,
    content: 'Stage 2 AKI is defined as a serum creatinine increase to 2.0-2.9x baseline.',
    relevance_score: 0.91,
    ...overrides,
  };
}

function render(props: Parameters<typeof PostIngestContextCard>[0]): string {
  return renderToStaticMarkup(createElement(PostIngestContextCard, props));
}

describe('PostIngestContextCard', () => {
  test('renders the summary text and the searched query footer', () => {
    const html = render({
      summary: 'Patient meets criteria for stage 2 AKI; consider nephrology consult.',
      guidelines: [makeGuideline()],
      queryUsed: 'AKI staging creatinine 2.4',
    });
    expect(html).toContain('Patient meets criteria for stage 2 AKI');
    expect(html).toContain('Searched');
    expect(html).toContain('AKI staging creatinine 2.4');
  });

  test('renders all guidelines when list is non-empty', () => {
    const guidelines: GuidelineSnippet[] = [
      makeGuideline({ chunk_id: 'c_one', document_title: 'Doc One' }),
      makeGuideline({ chunk_id: 'c_two', document_title: 'Doc Two', section: null, page_number: null }),
      makeGuideline({ chunk_id: 'c_three', document_title: 'Doc Three' }),
    ];
    const html = render({ summary: 'sum', guidelines, queryUsed: 'q' });
    expect(html).toContain('Relevant guidelines (3)');
    expect(html).toContain('G:c_one');
    expect(html).toContain('G:c_two');
    expect(html).toContain('G:c_three');
    expect(html).toContain('Doc One');
    expect(html).toContain('Doc Two');
    expect(html).toContain('Doc Three');
  });

  test('renders fallback message when guidelines list is empty', () => {
    const html = render({
      summary: 'No corpus hits but here is the structured summary.',
      guidelines: [],
      queryUsed: 'something obscure',
    });
    expect(html).toContain('No matching guidelines found.');
    expect(html).toContain('No corpus hits but here is the structured summary.');
    expect(html).not.toContain('Relevant guidelines (');
  });
});
