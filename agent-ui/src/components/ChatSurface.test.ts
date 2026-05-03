import { describe, test, expect } from 'vitest';
import type { AgentResponse, ErrorClass } from '../types';
import { RED, AMB, NEU } from '../styles/tokens';

// regression spec mirroring ChatSurface logic (ChatSurface.tsx)
// Both the error-class dictionary and the auto-collapse algorithm are inlined
// inside the component, so we re-implement them here verbatim and assert on
// the synthetic mirror. Any drift between the mirror and the component is a
// bug — keep them in sync when touching ChatSurface.tsx.

// ── Feature 2: Error states by class (commit 67788efe9) ─────────────────────

type ErrorClassMeta = {
  message: string;
  showRetry: boolean;
  tone: typeof RED | typeof AMB | typeof NEU;
};

const ERROR_CLASS_META: Record<ErrorClass, ErrorClassMeta> = {
  transient:    { message: 'Temporary issue — please try again.',   showRetry: true,  tone: AMB },
  persistent:   { message: 'Configuration issue — please contact IT.', showRetry: false, tone: RED },
  missing_data: { message: 'No record found.',                      showRetry: false, tone: NEU },
  unknown:      { message: 'Something went wrong.',                 showRetry: true,  tone: NEU },
};

function errorClassToMessage(cls: ErrorClass): string {
  return ERROR_CLASS_META[cls].message;
}

function errorClassToShouldShowRetry(cls: ErrorClass): boolean {
  return ERROR_CLASS_META[cls].showRetry;
}

// Mirrors the ErrorCard's rendered message resolution: prefer narrative, fall back to dict.
function resolveErrorMessage(response: AgentResponse): string {
  const meta = response.metadata ?? {};
  const errorClass: ErrorClass = (meta.error_class as ErrorClass) ?? 'unknown';
  const classMeta = ERROR_CLASS_META[errorClass] ?? ERROR_CLASS_META.unknown;
  return response.narrative?.trim() || classMeta.message;
}

// Mirrors the can-retry computation including metadata override.
function canRetry(response: AgentResponse, retryText: string | undefined, loading: boolean): boolean {
  const meta = response.metadata ?? {};
  const errorClass: ErrorClass = (meta.error_class as ErrorClass) ?? 'unknown';
  const classMeta = ERROR_CLASS_META[errorClass] ?? ERROR_CLASS_META.unknown;
  const retrySuggested = meta.retry_suggested ?? classMeta.showRetry;
  return !!(retrySuggested && retryText && !loading);
}

function makeErrorResponse(error_class: ErrorClass, narrative = ''): AgentResponse {
  return {
    type: 'error',
    data: null,
    narrative,
    citations: [],
    metadata: { error_class },
  };
}

describe('ChatSurface error class dictionary', () => {
  test('transient maps to retry-able amber message', () => {
    expect(errorClassToMessage('transient')).toBe('Temporary issue — please try again.');
    expect(errorClassToShouldShowRetry('transient')).toBe(true);
    expect(ERROR_CLASS_META.transient.tone).toBe(AMB);
  });

  test('persistent maps to non-retry red config-issue message', () => {
    expect(errorClassToMessage('persistent')).toBe('Configuration issue — please contact IT.');
    expect(errorClassToShouldShowRetry('persistent')).toBe(false);
    expect(ERROR_CLASS_META.persistent.tone).toBe(RED);
  });

  test('missing_data maps to non-retry neutral "no record" message', () => {
    expect(errorClassToMessage('missing_data')).toBe('No record found.');
    expect(errorClassToShouldShowRetry('missing_data')).toBe(false);
    expect(ERROR_CLASS_META.missing_data.tone).toBe(NEU);
  });

  test('unknown maps to retry-able neutral generic message', () => {
    expect(errorClassToMessage('unknown')).toBe('Something went wrong.');
    expect(errorClassToShouldShowRetry('unknown')).toBe(true);
    expect(ERROR_CLASS_META.unknown.tone).toBe(NEU);
  });

  test('narrative override beats dictionary message when non-empty', () => {
    const r = makeErrorResponse('transient', '  Custom narrative.  ');
    expect(resolveErrorMessage(r)).toBe('Custom narrative.');
  });

  test('blank narrative falls back to dictionary message', () => {
    const r = makeErrorResponse('persistent', '   ');
    expect(resolveErrorMessage(r)).toBe('Configuration issue — please contact IT.');
  });

  test('canRetry honours per-response retry_suggested override', () => {
    const r: AgentResponse = {
      type: 'error', data: null, narrative: '', citations: [],
      metadata: { error_class: 'persistent', retry_suggested: true },
    };
    expect(canRetry(r, 'original text', false)).toBe(true);
  });

  test('canRetry false when loading even if class is retry-able', () => {
    expect(canRetry(makeErrorResponse('transient'), 'orig', true)).toBe(false);
  });

  test('canRetry false when retryText missing', () => {
    expect(canRetry(makeErrorResponse('transient'), undefined, false)).toBe(false);
  });

  test('canRetry per spec: transient & unknown true, persistent & missing_data false', () => {
    const txt = 'orig text';
    expect(canRetry(makeErrorResponse('transient'), txt, false)).toBe(true);
    expect(canRetry(makeErrorResponse('unknown'), txt, false)).toBe(true);
    expect(canRetry(makeErrorResponse('persistent'), txt, false)).toBe(false);
    expect(canRetry(makeErrorResponse('missing_data'), txt, false)).toBe(false);
  });
});

// ── Feature 3: Collapsible AI responses (commit 7a2f1816f) ──────────────────

interface MirrorMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  response?: { type: AgentResponse['type'] };
}

/**
 * Mirror of the auto-collapse useEffect in ChatSurface.tsx.
 * Returns the resulting collapsedIds and the new lastAssistantId ref value.
 */
function applyAutoCollapse(
  messages: MirrorMessage[],
  prevCollapsed: Set<string>,
  prevLastAssistantId: string | null,
): { collapsedIds: Set<string>; lastAssistantId: string | null; mutated: boolean } {
  const assistantMsgs = messages.filter((m) => m.role === 'assistant' && m.response);
  if (assistantMsgs.length === 0) {
    return { collapsedIds: prevCollapsed, lastAssistantId: prevLastAssistantId, mutated: false };
  }
  const latest = assistantMsgs[assistantMsgs.length - 1];
  if (latest.id === prevLastAssistantId) {
    return { collapsedIds: prevCollapsed, lastAssistantId: prevLastAssistantId, mutated: false };
  }
  const newLast = latest.id;
  if (assistantMsgs.length <= 1) {
    return { collapsedIds: prevCollapsed, lastAssistantId: newLast, mutated: false };
  }
  const next = new Set(prevCollapsed);
  for (let i = 0; i < assistantMsgs.length - 1; i++) {
    const m = assistantMsgs[i];
    if (m.response?.type === 'census') continue;
    next.add(m.id);
  }
  next.delete(latest.id);
  return { collapsedIds: next, lastAssistantId: newLast, mutated: true };
}

function toggleCollapsed(prev: Set<string>, id: string): Set<string> {
  const next = new Set(prev);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function makeAssistant(id: string, type: AgentResponse['type'] = 'briefing'): MirrorMessage {
  return { id, role: 'assistant', response: { type } };
}

describe('ChatSurface auto-collapse on new turn', () => {
  test('3 assistant messages → ids 1 and 2 collapsed, id 3 expanded', () => {
    const msgs = [makeAssistant('a1'), makeAssistant('a2'), makeAssistant('a3')];
    const r = applyAutoCollapse(msgs, new Set(), null);
    expect(r.collapsedIds.has('a1')).toBe(true);
    expect(r.collapsedIds.has('a2')).toBe(true);
    expect(r.collapsedIds.has('a3')).toBe(false);
    expect(r.lastAssistantId).toBe('a3');
    expect(r.mutated).toBe(true);
  });

  test('census message is exempt from auto-collapse', () => {
    const msgs = [
      makeAssistant('census-1', 'census'),
      makeAssistant('a2', 'briefing'),
      makeAssistant('a3', 'query_answer'),
    ];
    const r = applyAutoCollapse(msgs, new Set(), null);
    expect(r.collapsedIds.has('census-1')).toBe(false);
    expect(r.collapsedIds.has('a2')).toBe(true);
    expect(r.collapsedIds.has('a3')).toBe(false);
  });

  test('manual toggleCollapsed flips state', () => {
    const a = toggleCollapsed(new Set(), 'm1');
    expect(a.has('m1')).toBe(true);
    const b = toggleCollapsed(a, 'm1');
    expect(b.has('m1')).toBe(false);
  });

  test('idempotent: same latest id yields no mutation', () => {
    const msgs = [makeAssistant('a1'), makeAssistant('a2')];
    const first = applyAutoCollapse(msgs, new Set(), null);
    const second = applyAutoCollapse(msgs, first.collapsedIds, first.lastAssistantId);
    expect(second.mutated).toBe(false);
    expect(second.collapsedIds).toBe(first.collapsedIds);
    expect(second.lastAssistantId).toBe(first.lastAssistantId);
  });

  test('empty assistant list → no error, no mutation', () => {
    const msgs: MirrorMessage[] = [
      { id: 'sys', role: 'system' },
      { id: 'u1', role: 'user' },
    ];
    expect(() => applyAutoCollapse(msgs, new Set(), null)).not.toThrow();
    const r = applyAutoCollapse(msgs, new Set(), null);
    expect(r.mutated).toBe(false);
    expect(r.collapsedIds.size).toBe(0);
    expect(r.lastAssistantId).toBeNull();
  });

  test('single assistant message → ref tracked but no collapse', () => {
    const msgs = [makeAssistant('a1')];
    const r = applyAutoCollapse(msgs, new Set(), null);
    expect(r.collapsedIds.size).toBe(0);
    expect(r.lastAssistantId).toBe('a1');
    expect(r.mutated).toBe(false);
  });

  test('user-toggled-open prior message stays open until the next turn collapses it', () => {
    // After turn 2: a1 collapsed, a2 latest.
    const turn2 = applyAutoCollapse([makeAssistant('a1'), makeAssistant('a2')], new Set(), null);
    expect(turn2.collapsedIds.has('a1')).toBe(true);
    // User manually re-expands a1.
    const userExpanded = toggleCollapsed(turn2.collapsedIds, 'a1');
    expect(userExpanded.has('a1')).toBe(false);
    // A new assistant turn arrives — a1 is collapsed again.
    const turn3 = applyAutoCollapse(
      [makeAssistant('a1'), makeAssistant('a2'), makeAssistant('a3')],
      userExpanded,
      turn2.lastAssistantId,
    );
    expect(turn3.collapsedIds.has('a1')).toBe(true);
    expect(turn3.collapsedIds.has('a2')).toBe(true);
    expect(turn3.collapsedIds.has('a3')).toBe(false);
  });

  test('census interleaved with multiple briefings: only briefings collapse', () => {
    const msgs = [
      makeAssistant('census-1', 'census'),
      makeAssistant('b1', 'briefing'),
      makeAssistant('b2', 'briefing'),
      makeAssistant('b3', 'briefing'),
    ];
    const r = applyAutoCollapse(msgs, new Set(), null);
    expect(r.collapsedIds.has('census-1')).toBe(false);
    expect(r.collapsedIds.has('b1')).toBe(true);
    expect(r.collapsedIds.has('b2')).toBe(true);
    expect(r.collapsedIds.has('b3')).toBe(false);
  });
});
