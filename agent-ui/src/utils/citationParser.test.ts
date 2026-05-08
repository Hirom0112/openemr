import { describe, test, expect } from 'vitest';
import type { ReactNode } from 'react';
import { parseCitationTokens } from './citationParser';

// Render chips as a sentinel string so we can assert on the interleaved
// output array shape without pulling in React rendering.
const chip = (id: string): ReactNode => `<<CHIP:${id}>>`;

describe('parseCitationTokens', () => {
  test('plain prose: token surrounded by text', () => {
    const out = parseCitationTokens('before [fact:intake:Foo] after', chip);
    expect(out).toEqual(['before ', '<<CHIP:fact:intake:Foo>>', ' after']);
  });

  test('multiple tokens on one line', () => {
    const out = parseCitationTokens(
      'a [fact:obs:42] b [guideline:abc] c [fact:intake:X] d',
      chip,
    );
    expect(out).toEqual([
      'a ',
      '<<CHIP:fact:obs:42>>',
      ' b ',
      '<<CHIP:guideline:abc>>',
      ' c ',
      '<<CHIP:fact:intake:X>>',
      ' d',
    ]);
  });

  test('fenced code block: tokens pass through untouched', () => {
    const input = 'see ```\n[fact:obs:42] inside fence\n``` end';
    const out = parseCitationTokens(input, chip);
    // Fenced segment must appear verbatim, no chip emitted from inside.
    const joined = out
      .map((n) => (typeof n === 'string' ? n : '<CHIP>'))
      .join('');
    expect(joined).toContain('[fact:obs:42] inside fence');
    expect(out.some((n) => n === '<<CHIP:fact:obs:42>>')).toBe(false);
  });

  test('inline code: tokens pass through untouched', () => {
    const out = parseCitationTokens(
      'use `[fact:obs:42]` literally and [fact:obs:99] live',
      chip,
    );
    // The literal token inside backticks survives; the live one becomes a chip.
    const codeSpan = out.find(
      (n) => typeof n === 'string' && n === '`[fact:obs:42]`',
    );
    expect(codeSpan).toBeDefined();
    expect(out).toContain('<<CHIP:fact:obs:99>>');
    expect(out).not.toContain('<<CHIP:fact:obs:42>>');
  });

  test('malformed token: empty payload does not match', () => {
    const out = parseCitationTokens('x [fact:obs:] y', chip);
    expect(out).toEqual(['x [fact:obs:] y']);
  });

  test('malformed token: unclosed bracket does not match', () => {
    const out = parseCitationTokens('x [fact:obs:42 y', chip);
    expect(out).toEqual(['x [fact:obs:42 y']);
  });

  test('empty input returns empty array', () => {
    expect(parseCitationTokens('', chip)).toEqual([]);
  });

  test('input with no tokens returns single string', () => {
    const out = parseCitationTokens('just narrative prose, nothing to see', chip);
    expect(out).toEqual(['just narrative prose, nothing to see']);
  });
});
