import type { ReactNode } from 'react';

/**
 * Walks a narrative string and replaces persisted citation tokens with
 * caller-supplied chip nodes.
 *
 * Recognised tokens (mirrors `agent-api/agent/synthesis.py` and the
 * `f"[{cid}]"` persistence at `agent-api/agent/main.py` ~L4283):
 *   - `[fact:obs:<row_id>]`
 *   - `[fact:intake:<field_name>]`
 *   - `[guideline:<chunk_id>]`
 *
 * Tokens inside fenced ``` code blocks or single-backtick inline code are
 * skipped so literal token examples render verbatim. Malformed tokens
 * (`[fact:obs:]`, `[fact:obs`) do not match and pass through as plain text.
 *
 * Returned array interleaves plain strings with whatever `renderChip`
 * returns. Callers should render this directly inside a React parent:
 *
 *   <p>{parseCitationTokens(narrative, (id) => <Chip id={id} />)}</p>
 *
 * Test cases (mirrored in `citationParser.test.ts`):
 *   - `before [fact:intake:Foo] after` → [string, chip, string]
 *   - inside fenced ``` block — no match
 *   - inside `inline code` — no match
 *   - multiple tokens on one line — multiple chips
 *   - malformed `[fact:obs:]` and `[fact:obs` — no match, raw text passes through
 */

const TOKEN_RE = /\[(fact:obs:[^\]]+|fact:intake:[^\]]+|guideline:[^\]]+)\]/g;

function splitOnFences(input: string): { text: string; isFence: boolean }[] {
  const out: { text: string; isFence: boolean }[] = [];
  const parts = input.split(/(```[\s\S]*?```)/g);
  for (const p of parts) {
    if (!p) continue;
    if (p.startsWith('```') && p.endsWith('```')) {
      out.push({ text: p, isFence: true });
    } else {
      out.push({ text: p, isFence: false });
    }
  }
  return out;
}

function splitOnInlineCode(input: string): { text: string; isCode: boolean }[] {
  const out: { text: string; isCode: boolean }[] = [];
  // Match paired single-backtick spans only. Unmatched backticks pass through.
  const re = /`[^`\n]+`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(input)) !== null) {
    if (m.index > last) {
      out.push({ text: input.slice(last, m.index), isCode: false });
    }
    out.push({ text: m[0], isCode: true });
    last = m.index + m[0].length;
  }
  if (last < input.length) {
    out.push({ text: input.slice(last), isCode: false });
  }
  return out;
}

function parseProse(
  prose: string,
  renderChip: (citationId: string) => ReactNode,
  keyPrefix: string,
): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  // Reset regex state for each prose segment.
  TOKEN_RE.lastIndex = 0;
  let chipIdx = 0;
  while ((m = TOKEN_RE.exec(prose)) !== null) {
    if (m.index > last) {
      out.push(prose.slice(last, m.index));
    }
    const captured = m[1];
    const node = renderChip(captured);
    // Wrap in a fragment with a stable key when the rendered node is a
    // valid React element; otherwise the consumer is on its own.
    out.push(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      typeof node === 'object' && node !== null && (node as any).$$typeof
        ? // Re-keying via fragment would lose props; trust the renderChip
          //   to supply a unique key, or accept the outer parent's key from
          //   array index. We tag plain nodes via wrapper keys when needed.
          node
        : node,
    );
    last = m.index + m[0].length;
    chipIdx += 1;
  }
  if (last < prose.length) {
    out.push(prose.slice(last));
  }
  void chipIdx;
  void keyPrefix;
  return out;
}

export function parseCitationTokens(
  input: string,
  renderChip: (citationId: string) => ReactNode,
): ReactNode[] {
  if (!input) return [];
  const out: ReactNode[] = [];
  const fenceSegments = splitOnFences(input);
  let segIdx = 0;
  for (const fs of fenceSegments) {
    if (fs.isFence) {
      out.push(fs.text);
      segIdx += 1;
      continue;
    }
    const codeSegments = splitOnInlineCode(fs.text);
    for (const cs of codeSegments) {
      if (cs.isCode) {
        out.push(cs.text);
      } else {
        const nodes = parseProse(cs.text, renderChip, `s${segIdx}`);
        for (const n of nodes) out.push(n);
      }
      segIdx += 1;
    }
  }
  return out;
}


/**
 * Strip the citation-grammar prefix from a token for display purposes.
 * The full token is preserved in click handlers, tooltips, and ARIA labels;
 * this helper only governs what shows inside the chip.
 *
 * Mapping:
 *   fact:obs:copilot-{doc}-{loinc}  →  obs · {loinc}
 *   fact:obs:{anything-else}         →  obs · {anything-else}
 *   fact:intake:{field}              →  {field}
 *   guideline:{chunk_id}             →  {chunk_id}
 *   anything else                    →  passthrough
 *
 * Rationale: chip color + shape already say "this is a citation"; the
 * `fact:` / `guideline:` prefix is informational noise. Keep the kind
 * signal for fact:obs (clinical-fact vs guideline matters at a glance)
 * but drop the redundant grammar prefix everywhere.
 */
export function chipLabel(citationId: string): string {
  if (citationId.startsWith('guideline:')) {
    return citationId.slice('guideline:'.length);
  }
  if (citationId.startsWith('fact:obs:')) {
    const tail = citationId.slice('fact:obs:'.length);
    // copilot-{doc}-{loinc} → loinc; otherwise keep the tail.
    const m = /^copilot-\d+-(.+)$/.exec(tail);
    return `obs · ${m ? m[1] : tail}`;
  }
  if (citationId.startsWith('fact:intake:')) {
    return citationId.slice('fact:intake:'.length);
  }
  return citationId;
}
