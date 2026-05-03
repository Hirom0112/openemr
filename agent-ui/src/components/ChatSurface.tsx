import { useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef } from 'react';
import { sendAgentMessage, sendAgentMessageWithMeta, prefetchPatientData, postClientTiming, getBriefing, getMedicationSafety, streamHandoff, refreshCensus } from '../api';
import type { HandoffSummaryPayload } from '../api';
import type { AgentResponse, CensusPatient, ErrorClass, HandoffData, HandoffPatient } from '../types';
import ResponseRenderer from './ResponseRenderer';
import { RED, AMB, NEU, BRAND, SURFACE, cardStyle, secondaryButtonStyle } from '../styles/tokens';
import { resolvePatientPid } from '../utils/citations';
import { formatFriendly } from '../utils/datetime';

function usePrefersReducedMotion(): boolean {
  const [prefers, setPrefers] = useState<boolean>(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const handler = (e: MediaQueryListEvent): void => setPrefers(e.matches);
    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', handler);
      return () => mq.removeEventListener('change', handler);
    }
    mq.addListener(handler);
    return () => mq.removeListener(handler);
  }, []);
  return prefers;
}

function openChartForPatient(patientId: string, openemrPid?: string): void {
  // openemrPid is the numeric integer PID OpenEMR's set_pid requires.
  // Fall back to resolvePatientPid for legacy pt-NNN synthetic IDs.
  const pid = openemrPid ?? resolvePatientPid(patientId);
  if (!/^\d+$/.test(pid)) {
    console.warn('Cannot open chart: resolved pid is not a positive integer', {
      patient_id: patientId,
      openemr_pid: openemrPid,
      resolved_pid: pid,
    });
    return;
  }
  const url = `/interface/patient_file/summary/demographics.php?set_pid=${pid}`;
  const w = window as unknown as {
    top?: { restoreSession?: () => void; RTop?: { location: string } };
  };
  if (w.top?.RTop) {
    w.top.restoreSession?.();
    w.top.RTop.location = url;
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

// Phrases the agent uses when it explicitly tells the physician to confirm
// something in the chart. The chart button is only rendered when one of
// these appears in the response narrative — without this gate, the button
// shows up on every clinical response (including safety refusals and
// follow-ups that don't actually need chart verification), which adds
// noise and can attach the button to the wrong patient.
const CHART_VERIFY_PHRASES = [
  'verify in chart',
  'verify in the chart',
  'view in chart',
  'view in the chart',
  'verify directly in the chart',
  'verify directly in chart',
  'verify before placing orders',
];

function narrativeRequestsChartVerify(narrative: string | undefined): boolean {
  if (!narrative) return false;
  const lowered = narrative.toLowerCase();
  return CHART_VERIFY_PHRASES.some((phrase) => lowered.includes(phrase));
}

function chartPatientIdForResponse(
  response: AgentResponse | undefined,
  patientIdsInContext: string[],
): { patientId: string; openemrPid?: string } | null {
  if (!response) return null;
  if (response.type === 'census' || response.type === 'handoff' || response.type === 'error') {
    return null;
  }
  const data = response.data as { patient_id?: unknown; openemr_pid?: unknown } | null | undefined;
  const openemrPid = data && typeof data.openemr_pid === 'string' && data.openemr_pid
    ? data.openemr_pid : undefined;

  // Structured responses (briefing, medication_safety with full data) always
  // get the chart button — they ARE chart-style summaries the physician is
  // expected to verify against the chart.
  if (response.type === 'briefing' || response.type === 'medication_safety') {
    if (data && typeof data.patient_id === 'string' && data.patient_id) {
      return { patientId: data.patient_id, openemrPid };
    }
  }

  // Free-text responses (text, query_answer): only render the chart button
  // when the narrative explicitly tells the physician to verify in chart.
  // Otherwise the button is noise on conversational answers and refusals.
  if (!narrativeRequestsChartVerify(response.narrative)) {
    return null;
  }

  if (data && typeof data.patient_id === 'string' && data.patient_id) {
    return { patientId: data.patient_id, openemrPid };
  }
  const meta = response.metadata;
  if (meta?.patient_id) {
    return {
      patientId: meta.patient_id,
      openemrPid: meta.openemr_pid ?? openemrPid,
    };
  }
  if (response.citations.length > 0 && response.citations[0].patient_id) {
    return { patientId: response.citations[0].patient_id, openemrPid };
  }
  if (patientIdsInContext.length === 1) {
    return { patientId: patientIdsInContext[0], openemrPid };
  }
  return null;
}

const CENSUS_INIT_MESSAGE = '__census_summary__';

// Progress messages timed against a typical 3-5s tool-chain. The user-visible
// text is intentionally generic — these are perceived-progress hints, not real
// dispatcher events (we have no streaming here). Each entry is shown for
// ~1500ms; the final message stays put until the response actually arrives.
const PROGRESS_MESSAGES: readonly string[] = [
  'Working…',
  'Looking up patient…',
  'Pulling chart data…',
  'Generating response…',
  'Almost there…',
];
const PROGRESS_INTERVAL_MS = 1500;

function ThinkingIndicator() {
  const [index, setIndex] = useState(0);
  const reduceMotion = usePrefersReducedMotion();

  useEffect(() => {
    setIndex(0);
    const id = window.setInterval(() => {
      setIndex((cur) => (cur < PROGRESS_MESSAGES.length - 1 ? cur + 1 : cur));
    }, PROGRESS_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, []);

  return (
    <span
      role="status"
      aria-live="polite"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        height: 16,
        fontSize: 13,
        color: NEU.secondary,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 12,
          height: 12,
          borderRadius: '50%',
          border: `2px solid ${NEU.border}`,
          borderTopColor: NEU.text,
          display: 'inline-block',
          animation: reduceMotion ? 'none' : 'copilot-spin 0.8s linear infinite',
        }}
      />
      <span>{PROGRESS_MESSAGES[index]}</span>
    </span>
  );
}

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content?: string;
  response?: AgentResponse;
  /** Original physician text that produced an error response — used by Retry. */
  retryText?: string;
}

// ── Inline error rendering ────────────────────────────────────────────────────
//
// Distinguish four user-facing error categories so the chat surface can offer
// the right CTA:
//   transient    → retry likely to work (timeout, transient FHIR)
//   persistent   → retry won't help; show support handle, hide retry
//   missing_data → no record; no retry CTA
//   unknown      → generic; offer retry but don't promise recovery
type ErrorClassMeta = {
  message: string;
  showRetry: boolean;
  tone: typeof RED | typeof AMB | typeof NEU;
};

const ERROR_CLASS_META: Record<ErrorClass, ErrorClassMeta> = {
  transient: {
    message: 'Temporary issue. Please try again.',
    showRetry: true,
    tone: AMB,
  },
  persistent: {
    message: 'Configuration issue. Please contact IT.',
    showRetry: false,
    tone: RED,
  },
  missing_data: {
    message: 'No record found.',
    showRetry: false,
    tone: NEU,
  },
  unknown: {
    message: 'Something went wrong.',
    showRetry: true,
    tone: NEU,
  },
};

function ErrorCard({
  response,
  retryText,
  onRetry,
  loading,
}: {
  response: AgentResponse;
  retryText?: string;
  onRetry: (text: string) => void;
  loading: boolean;
}) {
  const meta = response.metadata ?? {};
  const errorClass: ErrorClass = (meta.error_class as ErrorClass) ?? 'unknown';
  const classMeta = ERROR_CLASS_META[errorClass] ?? ERROR_CLASS_META.unknown;
  const retrySuggested = meta.retry_suggested ?? classMeta.showRetry;
  const failureClass = typeof meta.failure_class === 'string' ? meta.failure_class : null;
  const message = response.narrative?.trim() || classMeta.message;
  const canRetry = retrySuggested && !!retryText && !loading;

  return (
    <div
      style={{
        ...cardStyle(classMeta.tone),
        fontSize: 13,
        color: classMeta.tone.text,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
      role="alert"
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <span aria-hidden="true" style={{ fontSize: 14, lineHeight: '18px' }}>!</span>
        <div style={{ flex: 1 }}>{message}</div>
      </div>
      {(canRetry || failureClass) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {canRetry && retryText && (
            <button
              type="button"
              onClick={() => onRetry(retryText)}
              style={{
                padding: '4px 10px',
                background: SURFACE.bg,
                border: `1px solid ${classMeta.tone.border}`,
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 600,
                color: classMeta.tone.text,
                cursor: 'pointer',
                minHeight: 32,
              }}
            >
              Retry
            </button>
          )}
          {failureClass && (
            <span style={{ fontSize: 11, color: classMeta.tone.secondary, marginLeft: 'auto' }}>
              ref: {failureClass}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

interface ChatSurfaceProps {
  sessionId: string;
  patientIds: string[];
  providerName?: string;
}

export default function ChatSurface({ sessionId, patientIds, providerName }: ChatSurfaceProps) {
  const displayName = (providerName && providerName.trim()) || 'Doctor';
  const greeting = `Good day, ${displayName}. Ready for your census.`;

  const [messages, setMessages] = useState<Message[]>([
    { id: 'greeting', role: 'system', content: greeting },
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [collapsedIds, setCollapsedIds] = useState<Set<string>>(new Set());
  const [hoveredHeaderId, setHoveredHeaderId] = useState<string | null>(null);
  const lastAssistantIdRef = useRef<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const censusDispatched = useRef(false);
  const censusContext = useRef<string | undefined>(undefined);
  // Snapshot of the most recent census, used to render handoff placeholders in
  // triage order (P1 first) instead of whichever order SSE chunks land. Updated
  // on every census response; null until the first census arrives.
  const lastCensusRef = useRef<CensusPatient[] | null>(null);
  // Stick-to-bottom state. `isAtBottomRef` mirrors the state for synchronous
  // reads inside the layout effect (avoids stale closure on rapid streaming
  // chunks). `programmaticScrollRef` suppresses the scroll listener while we
  // call scrollIntoView ourselves, preventing a feedback loop.
  const [isAtBottom, setIsAtBottom] = useState(true);
  const isAtBottomRef = useRef(true);
  const programmaticScrollRef = useRef(false);
  const [showNewMessagesPill, setShowNewMessagesPill] = useState(false);
  const reduceMotion = usePrefersReducedMotion();
  const scrollBehaviorRef = useRef<ScrollBehavior>(reduceMotion ? 'auto' : 'smooth');
  scrollBehaviorRef.current = reduceMotion ? 'auto' : 'smooth';
  // When the user explicitly triggers a request (Send, Brief/Meds/Chart,
  // Refresh, Generate Handoff), set this flag so the next `messages` change
  // force-scrolls to bottom regardless of current scroll position. Background
  // updates (handoff SSE chunks, etc.) leave it false and fall through to the
  // existing isAtBottom gate.
  const forceScrollOnNextMessage = useRef(false);

  // When a NEW finalized assistant message arrives, collapse all prior assistant messages.
  // Census responses are exempt — they stay open as the persistent reference frame.
  useEffect(() => {
    const assistantMsgs = messages.filter((m) => m.role === 'assistant' && m.response);
    if (assistantMsgs.length === 0) return;
    const latest = assistantMsgs[assistantMsgs.length - 1];
    if (latest.id === lastAssistantIdRef.current) return;
    lastAssistantIdRef.current = latest.id;
    if (assistantMsgs.length <= 1) return;
    setCollapsedIds((prev) => {
      const next = new Set(prev);
      for (let i = 0; i < assistantMsgs.length - 1; i++) {
        const m = assistantMsgs[i];
        if (m.response?.type === 'census') continue;
        next.add(m.id);
      }
      next.delete(latest.id);
      return next;
    });
  }, [messages]);

  const toggleCollapsed = useCallback((id: string) => {
    setCollapsedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const formatHeaderTime = (id: string): string => {
    const m = id.match(/-(\d+)$/);
    const ts = m ? Number(m[1]) : NaN;
    const d = Number.isFinite(ts) ? new Date(ts) : new Date();
    return formatFriendly(d);
  };

  const labelForResponse = (response: AgentResponse | undefined): string => {
    if (!response) return 'Response';
    switch (response.type) {
      case 'census': return 'Census';
      case 'briefing': return 'Briefing';
      case 'query_answer': return 'Answer';
      case 'medication_safety': return 'Medications';
      case 'handoff': return 'Handoff';
      case 'error': return 'Error';
      case 'text':
      default: return 'Response';
    }
  };

  useEffect(() => {
    if (censusDispatched.current) return;
    censusDispatched.current = true;
    void prefetchPatientData(sessionId, patientIds, { forceRefresh: true });
    void dispatchMessage(CENSUS_INIT_MESSAGE, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Track whether the user is at (or near) the bottom of the scroll container.
  // 80px tolerance covers the case where a thinking indicator pushes content
  // slightly above the true bottom while the user "feels" pinned.
  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const handleScroll = (): void => {
      if (programmaticScrollRef.current) return;
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      const atBottom = distance < 80;
      isAtBottomRef.current = atBottom;
      setIsAtBottom(atBottom);
      if (atBottom) setShowNewMessagesPill(false);
    };
    el.addEventListener('scroll', handleScroll, { passive: true });
    return () => el.removeEventListener('scroll', handleScroll);
  }, []);

  // Initial mount: jump to bottom (chats start at the latest message).
  useLayoutEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    programmaticScrollRef.current = true;
    el.scrollTop = el.scrollHeight;
    requestAnimationFrame(() => { programmaticScrollRef.current = false; });
  }, []);

  // Stick-to-bottom on new content. Two distinct paths:
  //   (1) Force path — the most recent message addition was the result of a
  //       user-initiated action (Send, Brief/Meds/Chart, Refresh, Generate
  //       Handoff). Always scroll to bottom and hide the pill; the user
  //       expects to see their response.
  //   (2) Background path — incremental updates (handoff SSE chunks, etc.).
  //       Only scroll if the user is already at the bottom; otherwise show
  //       the "↓ New messages" pill so we don't yank them down.
  useLayoutEffect(() => {
    if (forceScrollOnNextMessage.current) {
      forceScrollOnNextMessage.current = false;
      programmaticScrollRef.current = true;
      bottomRef.current?.scrollIntoView({ behavior: scrollBehaviorRef.current });
      isAtBottomRef.current = true;
      setIsAtBottom(true);
      setShowNewMessagesPill(false);
      requestAnimationFrame(() => {
        requestAnimationFrame(() => { programmaticScrollRef.current = false; });
      });
    } else if (isAtBottomRef.current) {
      programmaticScrollRef.current = true;
      bottomRef.current?.scrollIntoView({ behavior: scrollBehaviorRef.current });
      // Release the suppression flag after the smooth scroll has had a chance
      // to fire its scroll events. One rAF is enough — the listener early-exits
      // while the flag is true and we re-derive `isAtBottom` on the next real
      // user scroll.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => { programmaticScrollRef.current = false; });
      });
    } else {
      setShowNewMessagesPill(true);
    }
  }, [messages]);

  const scrollToBottom = useCallback((): void => {
    const el = scrollContainerRef.current;
    if (!el) return;
    programmaticScrollRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: scrollBehaviorRef.current });
    isAtBottomRef.current = true;
    setIsAtBottom(true);
    setShowNewMessagesPill(false);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => { programmaticScrollRef.current = false; });
    });
  }, []);

  const dispatchMessage = useCallback(async (text: string, isAutoDispatch = false) => {
    if (!isAutoDispatch) {
      // User-initiated (Send, Retry): force scroll on the user bubble append.
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        { id: `user-${Date.now()}`, role: 'user', content: text },
      ]);
    }

    setLoading(true);
    const submitT0 = !isAutoDispatch ? performance.now() : null;
    try {
      let response: AgentResponse;
      if (!isAutoDispatch && submitT0 !== null) {
        const meta = await sendAgentMessageWithMeta(
          text,
          sessionId,
          censusContext.current,
          (requestId) => {
            postClientTiming({
              action: 'chat_submit_to_first_byte',
              duration_ms: Math.round(performance.now() - submitT0),
              request_id: requestId,
              session_id: sessionId,
            });
          },
        );
        response = meta.response;
        postClientTiming({
          action: 'chat_submit_to_done',
          duration_ms: Math.round(performance.now() - submitT0),
          request_id: meta.requestId,
          session_id: sessionId,
        });
      } else {
        response = await sendAgentMessage(text, sessionId, censusContext.current);
      }

      // After the census loads, cache a name→ID map so the LLM doesn't need
      // to re-fetch it on every subsequent query.
      if (response.type === 'census' && response.data) {
        const data = response.data as { census?: CensusPatient[] };
        if (data.census?.length) {
          const lines = data.census.map((p) => `  - ${p.name}: ${p.patient_id}`).join('\n');
          censusContext.current = `## Census patient name → ID mapping\n${lines}`;
          lastCensusRef.current = data.census;
        }
      }

      if (!isAutoDispatch) forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          response,
          retryText: response.type === 'error' && !isAutoDispatch ? text : undefined,
        },
      ]);
    } catch {
      if (!isAutoDispatch) forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          retryText: !isAutoDispatch ? text : undefined,
          response: {
            type: 'error',
            data: null,
            narrative: 'Agent unavailable. View chart directly.',
            citations: [],
            metadata: {
              error_class: 'transient',
              retry_suggested: true,
              failure_class: 'network',
            },
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  const dispatchBriefDirect = useCallback(async (
    name: string,
    patientId: string,
    options?: { forceRefresh?: boolean },
  ) => {
    const forceRefresh = options?.forceRefresh === true;
    // User-initiated (Brief or Refresh button): force scroll.
    forceScrollOnNextMessage.current = true;
    setMessages((prev) => [
      ...prev,
      {
        id: `user-${Date.now()}`,
        role: 'user',
        content: forceRefresh ? `Refresh ${name}` : `Brief ${name}`,
      },
    ]);

    setLoading(true);
    const submitT0 = performance.now();
    try {
      const meta = await getBriefing(patientId, sessionId, (requestId) => {
        postClientTiming({
          action: 'chat_submit_to_first_byte',
          duration_ms: Math.round(performance.now() - submitT0),
          request_id: requestId,
          session_id: sessionId,
          extra: { action: 'brief_direct_first_byte', patient_id: patientId },
        });
      }, forceRefresh);
      postClientTiming({
        action: 'chat_submit_to_done',
        duration_ms: Math.round(performance.now() - submitT0),
        request_id: meta.requestId,
        session_id: sessionId,
        extra: { action: 'brief_direct_done', patient_id: patientId },
      });
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        { id: `assistant-${Date.now()}`, role: 'assistant', response: meta.response },
      ]);
    } catch {
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          response: {
            type: 'error',
            data: null,
            narrative: 'Agent unavailable. View chart directly.',
            citations: [],
            metadata: {
              error_class: 'transient',
              retry_suggested: false,
              failure_class: 'network',
            },
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  const dispatchMedsDirect = useCallback(async (name: string, patientId: string) => {
    // User-initiated (Meds button): force scroll.
    forceScrollOnNextMessage.current = true;
    setMessages((prev) => [
      ...prev,
      { id: `user-${Date.now()}`, role: 'user', content: `Medications for ${name}` },
    ]);

    setLoading(true);
    const submitT0 = performance.now();
    try {
      const meta = await getMedicationSafety(patientId, sessionId, (requestId) => {
        postClientTiming({
          action: 'chat_submit_to_first_byte',
          duration_ms: Math.round(performance.now() - submitT0),
          request_id: requestId,
          session_id: sessionId,
          extra: { action: 'meds_direct_first_byte', patient_id: patientId },
        });
      });
      postClientTiming({
        action: 'chat_submit_to_done',
        duration_ms: Math.round(performance.now() - submitT0),
        request_id: meta.requestId,
        session_id: sessionId,
        extra: { action: 'meds_direct_done', patient_id: patientId },
      });
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        { id: `assistant-${Date.now()}`, role: 'assistant', response: meta.response },
      ]);
    } catch {
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          response: {
            type: 'error',
            data: null,
            narrative: 'Agent unavailable. View chart directly.',
            citations: [],
            metadata: {
              error_class: 'transient',
              retry_suggested: false,
              failure_class: 'network',
            },
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  // ── Streaming handoff ──────────────────────────────────────────────────────
  // Tracks the in-flight stream so a second click is a no-op and the bubble
  // ID is stable for incremental setMessages updates. Ref over state because
  // the cancel callback and chunk handler need synchronous access to the
  // current bubble id without re-binding setMessages.
  const handoffStreamRef = useRef<{ cancel: () => void; bubbleId: string } | null>(null);
  const [handoffStreaming, setHandoffStreaming] = useState(false);

  // Cancel any in-flight handoff stream when ChatSurface unmounts.
  useEffect(() => {
    return () => {
      handoffStreamRef.current?.cancel();
      handoffStreamRef.current = null;
    };
  }, []);

  /**
   * Map I-PASS HandoffSummary → HandoffPatient (matches the dispatcher transform
   * in agent-api/agent/tools/__init__.py::generate_handoff so the existing
   * HandoffRenderer can consume both paths without divergence).
   */
  const ipassToHandoffPatient = (s: HandoffSummaryPayload): HandoffPatient => ({
    patient_id: s.patient_id,
    name: s.name,
    status: s.illness_severity,
    active_issues: s.patient_summary ? [s.patient_summary] : [],
    pending_items: s.action_list ?? [],
    escalation_triggers: [s.situation_awareness, s.contingency_plan].filter((t): t is string => !!t),
  });

  const dispatchHandoffStreamDirect = useCallback((patientIdsToHandoff: string[], patientNames: Record<string, string>) => {
    if (handoffStreamRef.current) return; // second click while streaming = no-op
    if (patientIdsToHandoff.length === 0) return;

    const userId = `user-${Date.now()}`;
    const bubbleId = `assistant-${Date.now() + 1}`;

    // Determine canonical placeholder order: triage rank ascending (P1 first).
    // SSE chunks land in arrival order, but we want the rendered list stable
    // and clinically sorted. We read the latest census snapshot and sort the
    // requested ids by triage_level; ids not in the census fall back to their
    // original position. If the census is unavailable, we keep the order the
    // caller passed (typically census order from CensusRenderer).
    const censusSnapshot = lastCensusRef.current;
    const orderedPatientIds = (() => {
      if (!censusSnapshot) return patientIdsToHandoff;
      const triageRank = new Map<string, number>();
      for (const p of censusSnapshot) triageRank.set(p.patient_id, p.triage_level);
      // Stable sort: index breaks ties so unknown ids preserve relative order.
      return [...patientIdsToHandoff]
        .map((pid, idx) => ({ pid, idx, level: triageRank.get(pid) ?? Number.POSITIVE_INFINITY }))
        .sort((a, b) => (a.level - b.level) || (a.idx - b.idx))
        .map((e) => e.pid);
    })();

    // Placeholder data — one pending entry per patient, in canonical order.
    const placeholders: HandoffPatient[] = orderedPatientIds.map((pid) => ({
      patient_id: pid,
      name: patientNames[pid] ?? 'Patient',
      status: '',
      active_issues: [],
      pending_items: [],
      escalation_triggers: [],
      pending: true,
    }));
    const initialData: HandoffData = { patients: placeholders };
    const initialResponse: AgentResponse = {
      type: 'handoff',
      data: initialData,
      narrative: '',
      citations: [],
    };

    // User-initiated (Generate Handoff button): force scroll for the placeholder
    // bubble. Subsequent SSE chunk replacements in `replaceEntry` are background
    // updates and intentionally do NOT set this flag.
    forceScrollOnNextMessage.current = true;
    setMessages((prev) => [
      ...prev,
      { id: userId, role: 'user', content: `Generate shift handoff for ${patientIdsToHandoff.length} patients` },
      { id: bubbleId, role: 'assistant', response: initialResponse },
    ]);
    setHandoffStreaming(true);
    setLoading(true);

    const replaceEntry = (patientId: string, next: HandoffPatient): void => {
      setMessages((prev) => prev.map((m) => {
        if (m.id !== bubbleId || !m.response || m.response.type !== 'handoff') return m;
        const cur = m.response.data as HandoffData;
        const patients = cur.patients.map((p) => (p.patient_id === patientId ? next : p));
        return { ...m, response: { ...m.response, data: { ...cur, patients } } };
      }));
    };

    // Chunks render in place as they arrive. The placeholder ARRAY is
    // already triage-sorted (P1 first), so visual order holds even when
    // Linda's chunk lands before Marcus's — Linda's data fills slot 1,
    // Marcus's data fills slot 0 when it lands. The previous in-order
    // buffer attempted strict population order but could strand chunks
    // when an unexpected patient_id arrived; direct replaceEntry is more
    // robust.
    const cancel = streamHandoff(orderedPatientIds, sessionId, {
      onChunk: (patientId, summary) => {
        const next: HandoffPatient = ipassToHandoffPatient(summary);
        if (summary.error) next.error = summary.error;
        replaceEntry(patientId, next);
      },
      onError: (patientId, errorMsg, summary) => {
        const next: HandoffPatient = {
          ...ipassToHandoffPatient(summary),
          error: errorMsg,
        };
        replaceEntry(patientId, next);
      },
      onDone: () => {
        handoffStreamRef.current = null;
        setHandoffStreaming(false);
        setLoading(false);
      },
    });
    handoffStreamRef.current = { cancel, bubbleId };
  }, [sessionId]);

  // Force-refresh the medication-safety bubble. Mirrors dispatchBriefDirect's
  // force_refresh path: re-issues /medication/safety with force_refresh=true
  // so the bundle cache is bypassed and the response carries a fresh
  // generated_at. Wired to the Refresh button in MedicationSafetyRenderer.
  const dispatchMedsForceRefresh = useCallback(async (patientId: string) => {
    forceScrollOnNextMessage.current = true;
    setMessages((prev) => [
      ...prev,
      { id: `user-${Date.now()}`, role: 'user', content: 'Refresh medications' },
    ]);
    setLoading(true);
    const submitT0 = performance.now();
    try {
      const meta = await getMedicationSafety(patientId, sessionId, (requestId) => {
        postClientTiming({
          action: 'chat_submit_to_first_byte',
          duration_ms: Math.round(performance.now() - submitT0),
          request_id: requestId,
          session_id: sessionId,
          extra: { action: 'meds_refresh_first_byte', patient_id: patientId },
        });
      }, true);
      postClientTiming({
        action: 'chat_submit_to_done',
        duration_ms: Math.round(performance.now() - submitT0),
        request_id: meta.requestId,
        session_id: sessionId,
        extra: { action: 'meds_refresh_done', patient_id: patientId },
      });
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        { id: `assistant-${Date.now()}`, role: 'assistant', response: meta.response },
      ]);
    } catch {
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          response: {
            type: 'error',
            data: null,
            narrative: 'Medication safety refresh failed. Try again or view the chart directly.',
            citations: [],
            metadata: {
              error_class: 'transient',
              retry_suggested: true,
              failure_class: 'network',
            },
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  // Force-refresh the census via /triage/census with force_refresh=true.
  // Bypasses the 5-min Redis cache and gives the user a brand-new
  // generated_at. Wired to the Refresh button in CensusRenderer.
  const dispatchCensusForceRefresh = useCallback(async () => {
    forceScrollOnNextMessage.current = true;
    setMessages((prev) => [
      ...prev,
      { id: `user-${Date.now()}`, role: 'user', content: 'Refresh census' },
    ]);
    setLoading(true);
    const submitT0 = performance.now();
    try {
      // Cascading-fresh: when the user clicks Refresh on the census, also
      // fire a force-refresh prefetch so the per-patient bundle / briefing /
      // medication-safety caches turn over too. Without this the census
      // header reads "now" but a subsequent Brief click still returns the
      // briefing keyed against the previous shift's bundle. Fire-and-forget;
      // the census refresh below remains the user-visible operation.
      void prefetchPatientData(sessionId, patientIds, { forceRefresh: true });
      const meta = await refreshCensus(patientIds, sessionId, (requestId) => {
        postClientTiming({
          action: 'chat_submit_to_first_byte',
          duration_ms: Math.round(performance.now() - submitT0),
          request_id: requestId,
          session_id: sessionId,
          extra: { action: 'census_refresh_first_byte' },
        });
      });
      postClientTiming({
        action: 'chat_submit_to_done',
        duration_ms: Math.round(performance.now() - submitT0),
        request_id: meta.requestId,
        session_id: sessionId,
        extra: { action: 'census_refresh_done' },
      });
      // Update the cached census snapshot used by handoff ordering.
      const data = meta.response.data as { census?: CensusPatient[] };
      if (data?.census?.length) {
        const lines = data.census.map((p) => `  - ${p.name}: ${p.patient_id}`).join('\n');
        censusContext.current = `## Census patient name → ID mapping\n${lines}`;
        lastCensusRef.current = data.census;
      }
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        { id: `assistant-${Date.now()}`, role: 'assistant', response: meta.response },
      ]);
    } catch {
      forceScrollOnNextMessage.current = true;
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          response: {
            type: 'error',
            data: null,
            narrative: 'Census refresh failed. Try again or view the chart directly.',
            citations: [],
            metadata: {
              error_class: 'transient',
              retry_suggested: true,
              failure_class: 'network',
            },
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }, [sessionId, patientIds]);

  // Free-text patterns that should route to the streaming handoff path
  // (same as the in-census Generate Handoff button). Without this, typed
  // "handoff" goes through the dispatcher → returns one big JSON blob at
  // the end (~15-25s of stare-at-loading-dots), while the button streams
  // patient-by-patient in 3-4s. Match the keywords mirrors
  // _RESPONSE_TYPE_INTENT_HINTS["handoff"] in agent-api/agent/dispatcher.py.
  const HANDOFF_KEYWORDS = useMemo(
    () => [
      'handoff',
      'hand-off',
      'hand off',
      'sign-out',
      'signout',
      'sign out',
      'end of rounds',
      'end-of-rounds',
      'shift end',
      'generate handoff',
    ],
    [],
  );

  const isHandoffRequest = useCallback((text: string): boolean => {
    const lowered = text.toLowerCase();
    return HANDOFF_KEYWORDS.some((kw) => lowered.includes(kw));
  }, [HANDOFF_KEYWORDS]);

  const handleSend = useCallback(() => {
    const text = inputText.trim();
    if (!text || loading) return;
    setInputText('');

    // Route typed handoff requests to the same SSE path the button uses.
    // Falls through to the dispatcher only when the latest census isn't
    // available (no patient list to stream against — let the LLM handle it).
    if (isHandoffRequest(text) && lastCensusRef.current?.length) {
      const ids: string[] = [];
      const names: Record<string, string> = {};
      for (const p of lastCensusRef.current) {
        if (typeof p.patient_id === 'string' && p.patient_id) {
          ids.push(p.patient_id);
          if (typeof p.name === 'string' && p.name) {
            names[p.patient_id] = p.name;
          }
        }
      }
      if (ids.length > 0) {
        dispatchHandoffStreamDirect(ids, names);
        return;
      }
    }

    void dispatchMessage(text);
  }, [inputText, loading, dispatchMessage, isHandoffRequest, dispatchHandoffStreamDirect]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <>
      {/* Keyframe animation injected once */}
      <style>{`
        @keyframes copilot-spin {
          to { transform: rotate(360deg); }
        }
      `}</style>

      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', minHeight: 0, position: 'relative' }}>
        {/* pinned above input row */}
        {showNewMessagesPill && !isAtBottom && (
          <button
            type="button"
            onClick={scrollToBottom}
            style={{
              position: 'absolute',
              bottom: PILL_BOTTOM_OFFSET,
              left: '50%',
              transform: 'translateX(-50%)',
              zIndex: 10,
              padding: '8px 14px',
              background: BRAND.base,
              color: BRAND.onBrand,
              border: 'none',
              borderRadius: 16,
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
              boxShadow: '0 2px 8px rgba(15, 23, 42, 0.18)',
              fontFamily: 'inherit',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              minHeight: 36,
            }}
          >
            <span aria-hidden="true">↓</span>
            <span>New messages</span>
          </button>
        )}
        {/* Message list */}
        <div
          ref={scrollContainerRef}
          style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '12px 14px' }}
        >
          {messages.map((msg) => {
            if (msg.role === 'system') {
              return (
                <div key={msg.id} style={styles.systemMsg}>
                  <span aria-hidden="true" style={styles.systemGlyph}>◆</span>
                  <span>{msg.content}</span>
                </div>
              );
            }

            if (msg.role === 'user') {
              return (
                <div key={msg.id} style={styles.userRow}>
                  <div style={styles.userBubble}>{msg.content}</div>
                </div>
              );
            }

            // assistant
            // Census is collapsible MANUALLY (chevron + click) but is exempt
            // from the auto-collapse-on-new-message effect so it stays the
            // persistent reference frame unless the user explicitly folds it.
            const collapsed = collapsedIds.has(msg.id);
            const label = labelForResponse(msg.response);
            const time = formatHeaderTime(msg.id);
            const isHovered = hoveredHeaderId === msg.id;
            const patientLabel =
              typeof msg.response?.metadata?.patient_name === 'string' && msg.response.metadata.patient_name
                ? ` · ${msg.response.metadata.patient_name as string}`
                : '';
            const headerStyle: React.CSSProperties = {
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 12,
              color: SURFACE.muted,
              fontWeight: 600,
              letterSpacing: '0.02em',
              padding: '6px 12px',
              minHeight: 32,
              background: isHovered ? SURFACE.hover : SURFACE.panel,
              border: `1px solid ${SURFACE.border}`,
              borderBottom: collapsed ? `1px solid ${SURFACE.border}` : 'none',
              borderRadius: collapsed ? 8 : '8px 8px 0 0',
              cursor: 'pointer',
              width: '100%',
              textAlign: 'left',
              fontFamily: 'inherit',
              transition: 'background 0.12s',
            };
            const headerInner = (
              <>
                <span aria-hidden="true" style={{ color: BRAND.base, fontSize: 11 }}>◆</span>
                <span style={{ color: SURFACE.fg }}>{label}</span>
                <span style={{ color: SURFACE.subtle, fontWeight: 400 }}>·</span>
                <span style={{ fontWeight: 500 }}>{time}</span>
                {patientLabel && (
                  <span style={{ fontWeight: 400, color: SURFACE.muted }}>{patientLabel}</span>
                )}
                <span style={{ marginLeft: 'auto', fontSize: 12, color: SURFACE.muted }} aria-hidden="true">
                  {collapsed ? '▸' : '▾'}
                </span>
              </>
            );
            return (
              <div key={msg.id} style={styles.assistantFrame}>
                <button
                  type="button"
                  aria-expanded={!collapsed}
                  aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${label} message`}
                  onClick={() => toggleCollapsed(msg.id)}
                  onMouseEnter={() => setHoveredHeaderId(msg.id)}
                  onMouseLeave={() => setHoveredHeaderId((cur) => (cur === msg.id ? null : cur))}
                  onFocus={() => setHoveredHeaderId(msg.id)}
                  onBlur={() => setHoveredHeaderId((cur) => (cur === msg.id ? null : cur))}
                  style={headerStyle}
                >
                  {headerInner}
                </button>
                {!collapsed && (
                  <div style={styles.assistantBody}>
                    {msg.response && msg.response.type === 'error' ? (
                      <ErrorCard
                        response={msg.response}
                        retryText={msg.retryText}
                        loading={loading}
                        onRetry={(text) => { void dispatchMessage(text); }}
                      />
                    ) : msg.response ? (
                      <ResponseRenderer
                        response={msg.response}
                        sessionId={sessionId}
                        patientName={
                          typeof msg.response.metadata?.patient_name === 'string'
                            ? (msg.response.metadata.patient_name as string)
                            : undefined
                        }
                        onBrief={(name, patientId, options) => {
                          if (patientId) {
                            void dispatchBriefDirect(name, patientId, options);
                          } else {
                            void dispatchMessage(`Brief ${name}`);
                          }
                        }}
                        onMeds={(name, patientId) => {
                          if (patientId) {
                            void dispatchMedsDirect(name, patientId);
                          } else {
                            void dispatchMessage(`show meds for ${name}`);
                          }
                        }}
                        onHandoff={(ids, names) => dispatchHandoffStreamDirect(ids, names)}
                        handoffInFlight={handoffStreaming}
                        providerName={displayName}
                        onRefreshCensus={() => { void dispatchCensusForceRefresh(); }}
                        onRefreshMedicationSafety={(patientId) => { void dispatchMedsForceRefresh(patientId); }}
                      />
                    ) : (
                      <span style={{ color: SURFACE.subtle }}>…</span>
                    )}
                    {(() => {
                      const chartTarget = chartPatientIdForResponse(msg.response, patientIds);
                      if (!chartTarget) return null;
                      const patientName =
                        typeof msg.response?.metadata?.patient_name === 'string' && msg.response.metadata.patient_name
                          ? (msg.response.metadata.patient_name as string)
                          : 'patient';
                      return (
                        <div style={{ marginTop: 10, display: 'flex', justifyContent: 'flex-end' }}>
                          <button
                            type="button"
                            aria-label={`Open chart for ${patientName} in OpenEMR`}
                            onClick={() => openChartForPatient(chartTarget.patientId, chartTarget.openemrPid)}
                            style={{
                              fontSize: 12,
                              fontWeight: 500,
                              padding: '6px 12px',
                              minHeight: 32,
                              ...secondaryButtonStyle(),
                              borderRadius: 6,
                              cursor: 'pointer',
                              whiteSpace: 'nowrap',
                              fontFamily: 'inherit',
                            }}
                          >
                            Verify in Chart <span aria-hidden="true">↗</span>
                          </button>
                        </div>
                      );
                    })()}
                  </div>
                )}
              </div>
            );
          })}

          {loading && !handoffStreaming && (
            <div style={styles.assistantFrame}>
              <div style={{ ...styles.assistantBody, borderRadius: 8 }}>
                <ThinkingIndicator />
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input row */}
        <div style={styles.inputRow}>
          <label htmlFor="copilot-chat-input" style={styles.visuallyHidden}>
            Ask Clinical Copilot about a patient
          </label>
          <input
            id="copilot-chat-input"
            style={styles.input}
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Ask about a patient. Try "Brief Marcus Webb" or "why is ${displayName.split(' ')[0]} P1?"`}
            disabled={loading}
          />
          <button
            style={{
              ...styles.sendBtn,
              opacity: loading || !inputText.trim() ? 0.5 : 1,
              cursor: loading || !inputText.trim() ? 'not-allowed' : 'pointer',
            }}
            onClick={handleSend}
            disabled={loading || !inputText.trim()}
          >
            Send
          </button>
        </div>
      </div>
    </>
  );
}

// Input row sits ~56px tall (44px button + 12px padding); pin pill above it.
const PILL_BOTTOM_OFFSET = 72;

const styles: Record<string, React.CSSProperties> = {
  systemMsg: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 8,
    fontSize: 12,
    color: SURFACE.muted,
    fontWeight: 400,
    padding: '4px 4px',
    marginBottom: 12,
    lineHeight: 1.5,
  },
  systemGlyph: {
    color: BRAND.base,
    fontSize: 11,
    flexShrink: 0,
  },
  userRow: {
    display: 'flex',
    justifyContent: 'flex-end',
    marginBottom: 10,
  },
  userBubble: {
    background: BRAND.base,
    color: BRAND.onBrand,
    borderRadius: 8,
    padding: '9px 14px',
    maxWidth: '78%',
    fontSize: 13,
    lineHeight: 1.5,
  },
  assistantFrame: {
    marginBottom: 12,
    marginRight: '4%',
  },
  assistantBody: {
    background: SURFACE.bg,
    border: `1px solid ${SURFACE.border}`,
    borderTop: 'none',
    borderRadius: '0 0 8px 8px',
    padding: '10px 14px',
    fontSize: 13,
    lineHeight: 1.55,
    minWidth: 0,
  },
  inputRow: {
    display: 'flex',
    gap: 8,
    padding: '10px 14px',
    borderTop: `1px solid ${SURFACE.border}`,
    flex: '0 0 auto',
    background: SURFACE.bg,
  },
  input: {
    flex: 1,
    padding: '11px 12px',
    minHeight: 44,
    border: `1px solid ${SURFACE.borderStrong}`,
    borderRadius: 8,
    fontFamily: 'inherit',
    fontSize: 13,
    outline: 'none',
    background: SURFACE.panel,
    color: SURFACE.fgStrong,
    transition: 'border-color 0.15s',
  },
  sendBtn: {
    padding: '0 18px',
    minHeight: 44,
    background: BRAND.base,
    color: BRAND.onBrand,
    border: 'none',
    borderRadius: 8,
    fontSize: 13,
    fontWeight: 600,
    letterSpacing: '0.02em',
    transition: 'opacity 0.15s',
  },
  visuallyHidden: {
    position: 'absolute',
    width: 1,
    height: 1,
    padding: 0,
    margin: -1,
    overflow: 'hidden',
    clip: 'rect(0,0,0,0)',
    whiteSpace: 'nowrap',
    border: 0,
  },
};

