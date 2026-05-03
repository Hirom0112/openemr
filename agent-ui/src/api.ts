import type { AgentResponse, TriageRationaleData } from './types';

const cfg = () => window.__COPILOT_CONFIG__;

interface ClientTimingPayload {
  action: string;
  duration_ms: number;
  request_id?: string;
  session_id?: string;
  t_navigation_start_ms?: number;
  extra?: Record<string, unknown>;
}

function generateRequestId(): string {
  const c: Crypto | undefined = typeof crypto !== 'undefined' ? crypto : undefined;
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID();
  }
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function postClientTiming(payload: ClientTimingPayload): void {
  try {
    const url = `${cfg().agentApiUrl}/agent/client-timing`;
    void fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch((err: unknown) => {
      console.debug('[copilot] postClientTiming failed', err);
    });
  } catch (err: unknown) {
    console.debug('[copilot] postClientTiming threw', err);
  }
}

interface PostResult<T> {
  data: T;
  requestId: string;
}

async function post<T>(path: string, body: unknown, timeoutMs = 30_000): Promise<T> {
  const result = await postWithMeta<T>(path, body, timeoutMs);
  return result.data;
}

async function postWithMeta<T>(
  path: string,
  body: unknown,
  timeoutMs = 30_000,
  onFirstByte?: (requestId: string) => void,
): Promise<PostResult<T>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const isAgentPath = path.startsWith('/agent/');
  const clientRequestId = isAgentPath ? generateRequestId() : '';
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (isAgentPath) {
      headers['X-Request-ID'] = clientRequestId;
    }
    const res = await fetch(`${cfg().agentApiUrl}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const serverRequestId = res.headers.get('X-Request-ID') ?? clientRequestId;
    if (onFirstByte) {
      try { onFirstByte(serverRequestId); } catch (err: unknown) { console.debug('[copilot] onFirstByte threw', err); }
    }
    if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
    const data = (await res.json()) as T;
    return { data, requestId: serverRequestId };
  } finally {
    clearTimeout(timer);
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${cfg().agentApiUrl}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
  return res.json();
}

export async function fetchHealth() {
  const url = `${cfg().agentApiUrl}/health`;
  console.log('[copilot] fetchHealth →', url);
  const result = await get<{ status: string; redis: boolean }>('/health');
  console.log('[copilot] fetchHealth ←', result);
  return result;
}

/** Fire-and-forget signal to warm the FHIR cache on panel mount. */
export async function prefetchPatientData(sessionId: string, patientIds: string[]): Promise<void> {
  try {
    await post('/agent/prefetch', {
      session_id: sessionId,
      provider_id: String(cfg().providerId),
      patient_ids: patientIds,
    });
  } catch {
    // Non-blocking — pre-fetch failure must never block the panel
  }
}

export interface SendAgentMessageResult {
  response: AgentResponse;
  requestId: string;
  durationMs: number;
}

/** Main dispatcher — all conversational turns route through here. */
export async function sendAgentMessage(
  message: string,
  sessionId: string,
  censusContext?: string,
): Promise<AgentResponse> {
  const result = await sendAgentMessageWithMeta(message, sessionId, censusContext);
  return result.response;
}

export async function sendAgentMessageWithMeta(
  message: string,
  sessionId: string,
  censusContext?: string,
  onFirstByte?: (requestId: string) => void,
): Promise<SendAgentMessageResult> {
  const t0 = performance.now();
  // 90s: handoffs over a full census fire N parallel LLM calls and can legitimately take >30s
  const result = await postWithMeta<AgentResponse>('/agent/query', {
    message,
    session_id: sessionId,
    provider_id: String(cfg().providerId),
    patient_ids: cfg().patientIds ?? [],
    provider_name: cfg().providerName ?? 'Provider',
    ...(censusContext ? { census_context: censusContext } : {}),
  }, 90_000, onFirstByte);
  const durationMs = performance.now() - t0;
  postClientTiming({
    action: 'agent_message',
    duration_ms: Math.round(durationMs),
    request_id: result.requestId,
    session_id: sessionId,
  });
  return { response: result.data, requestId: result.requestId, durationMs };
}

/** Direct-call triage rationale — bypasses dispatcher for <2s budget. */
export async function fetchTriageRationale(patientId: string, sessionId?: string): Promise<TriageRationaleData> {
  return post<TriageRationaleData>(`/agent/triage_rationale/${patientId}`, {
    patient_id: patientId,
    session_id: sessionId ?? cfg().sessionId ?? null,
  });
}

/**
 * Direct-call briefing — bypasses the dispatcher's two extra Anthropic round-trips
 * (~7s overhead) for explicit Brief clicks where we already know patient_id.
 *
 * The free-text "Brief Marcus Webb" path still routes through /agent/query.
 */
export interface GetBriefingResult {
  response: AgentResponse;
  requestId: string;
  durationMs: number;
}

export async function getBriefing(
  patientId: string,
  sessionId: string,
  onFirstByte?: (requestId: string) => void,
  forceRefresh?: boolean,
): Promise<GetBriefingResult> {
  const t0 = performance.now();
  const result = await postWithMeta<import('./types').BriefingSection>(
    `/briefing/${patientId}`,
    forceRefresh ? { force_refresh: true } : {},
    90_000,
    onFirstByte,
  );
  const durationMs = performance.now() - t0;
  postClientTiming({
    action: 'brief_direct_total',
    duration_ms: Math.round(durationMs),
    request_id: result.requestId,
    session_id: sessionId,
    extra: { patient_id: patientId },
  });
  const response: AgentResponse = {
    type: 'briefing',
    data: result.data,
    narrative: '',
    citations: [],
  };
  return { response, requestId: result.requestId, durationMs };
}

/**
 * Direct-call medication safety — bypasses the dispatcher's planner round-trips
 * for explicit Meds clicks where we already know patient_id.
 *
 * The free-text "show meds for Marcus" path still routes through /agent/query.
 */
export interface GetMedicationSafetyResult {
  response: AgentResponse;
  requestId: string;
  durationMs: number;
}

export async function getMedicationSafety(
  patientId: string,
  sessionId: string,
  onFirstByte?: (requestId: string) => void,
): Promise<GetMedicationSafetyResult> {
  const t0 = performance.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 90_000);
  const clientRequestId = generateRequestId();
  try {
    const res = await fetch(`${cfg().agentApiUrl}/medication/safety/${patientId}`, {
      method: 'GET',
      headers: { 'X-Request-ID': clientRequestId },
      signal: controller.signal,
    });
    const serverRequestId = res.headers.get('X-Request-ID') ?? clientRequestId;
    if (onFirstByte) {
      try { onFirstByte(serverRequestId); } catch (err: unknown) { console.debug('[copilot] onFirstByte threw', err); }
    }
    if (!res.ok) throw new Error(`API error ${res.status}: /medication/safety/${patientId}`);
    const data = (await res.json()) as import('./types').MedicationSafetyData;
    const durationMs = performance.now() - t0;
    postClientTiming({
      action: 'meds_direct_total',
      duration_ms: Math.round(durationMs),
      request_id: serverRequestId,
      session_id: sessionId,
      extra: { patient_id: patientId },
    });
    const response: AgentResponse = {
      type: 'medication_safety',
      data,
      narrative: '',
      citations: [],
    };
    return { response, requestId: serverRequestId, durationMs };
  } finally {
    clearTimeout(timer);
  }
}

// ── Streaming handoff (SSE) ───────────────────────────────────────────────────

/**
 * Raw I-PASS handoff summary as emitted by POST /handoff/generate/stream.
 * Mirrors agent-api/handoff/generator.py::HandoffSummary.
 */
export interface HandoffSummaryPayload {
  patient_id: string;
  name: string;
  mrn: string;
  triage_level: number;
  illness_severity: string;
  patient_summary: string;
  action_list: string[];
  situation_awareness: string;
  contingency_plan: string;
  generated_at: string;
  error?: string | null;
}

export interface HandoffStreamStats {
  total: number;
  succeeded: number;
  failed: number;
  duration_ms: number;
}

export interface StreamHandoffCallbacks {
  onChunk: (patientId: string, summary: HandoffSummaryPayload) => void;
  onError: (patientId: string, error: string, summary: HandoffSummaryPayload) => void;
  onDone: (stats: HandoffStreamStats) => void;
}

interface ParsedSseEvent {
  event: string;
  data: string;
}

/**
 * Parse an SSE event block (text between \n\n separators).
 * Only the "event:" and "data:" fields are honored — handoff stream is simple.
 */
function parseSseBlock(block: string): ParsedSseEvent | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const rawLine of block.split('\n')) {
    if (!rawLine || rawLine.startsWith(':')) continue;
    const colonIdx = rawLine.indexOf(':');
    const field = colonIdx === -1 ? rawLine : rawLine.slice(0, colonIdx);
    const value = colonIdx === -1 ? '' : rawLine.slice(colonIdx + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join('\n') };
}

/**
 * Stream per-patient handoff summaries from POST /handoff/generate/stream.
 *
 * The fetch body reader is parsed as SSE manually — TCP reads can split
 * mid-event, so we keep an unparsed-tail buffer between reads and only
 * dispatch complete blocks (delimited by \n\n).
 *
 * Returns an abort function. Call it on unmount or on a new request.
 */
export function streamHandoff(
  patientIds: string[],
  sessionId: string,
  callbacks: StreamHandoffCallbacks,
): () => void {
  const controller = new AbortController();
  const t0 = performance.now();
  let firstByteLogged = false;

  const run = async (): Promise<void> => {
    let res: Response;
    try {
      res = await fetch(`${cfg().agentApiUrl}/handoff/generate/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({ patient_ids: patientIds }),
        signal: controller.signal,
      });
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === 'AbortError') return;
      throw err;
    }
    if (!res.ok || !res.body) {
      throw new Error(`Handoff stream error ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    const dispatch = (parsed: ParsedSseEvent): void => {
      if (!firstByteLogged && parsed.event === 'handoff_chunk') {
        firstByteLogged = true;
        postClientTiming({
          action: 'chat_submit_to_first_byte',
          duration_ms: Math.round(performance.now() - t0),
          session_id: sessionId,
          extra: { action: 'handoff_stream_first_byte', patient_count: patientIds.length },
        });
      }
      let payload: unknown;
      try {
        payload = JSON.parse(parsed.data);
      } catch {
        return;
      }
      if (parsed.event === 'handoff_chunk') {
        const p = payload as { patient_id?: string; summary?: HandoffSummaryPayload };
        if (p.patient_id && p.summary) {
          callbacks.onChunk(p.patient_id, p.summary);
        }
      } else if (parsed.event === 'error') {
        const p = payload as {
          patient_id?: string; message?: string; error_class?: string; summary?: HandoffSummaryPayload;
        };
        if (p.patient_id) {
          const fallback: HandoffSummaryPayload = p.summary ?? {
            patient_id: p.patient_id, name: 'Unknown', mrn: '', triage_level: 10,
            illness_severity: '', patient_summary: '', action_list: [],
            situation_awareness: '', contingency_plan: '', generated_at: '',
          };
          callbacks.onError(p.patient_id, p.message ?? 'Handoff failed', fallback);
        }
      } else if (parsed.event === 'done') {
        const stats = payload as HandoffStreamStats;
        postClientTiming({
          action: 'chat_submit_to_done',
          duration_ms: Math.round(performance.now() - t0),
          session_id: sessionId,
          extra: { action: 'handoff_stream_done', ...stats },
        });
        callbacks.onDone(stats);
      }
    };

    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sepIdx: number;
        // Drain every fully terminated block from the buffer; keep the tail.
        while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx + 2);
          const parsed = parseSseBlock(block);
          if (parsed) dispatch(parsed);
        }
      }
      // Flush any trailing event without final \n\n.
      if (buffer.trim().length > 0) {
        const parsed = parseSseBlock(buffer);
        if (parsed) dispatch(parsed);
      }
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === 'AbortError') return;
      throw err;
    }
  };

  void run().catch((err: unknown) => {
    console.warn('[copilot] streamHandoff failed', err);
  });

  return () => {
    try { controller.abort(); } catch { /* noop */ }
  };
}

// ── Legacy endpoints (kept for backward compatibility) ────────────────────────

export async function fetchCensus(patientIds: string[], sessionId: string) {
  return post<{ census: import('./types').TriageEntry[]; total: number }>(
    '/triage/census',
    { patient_ids: patientIds, session_id: sessionId }
  );
}

export async function sendQuery(sessionId: string, patientId: string, query: string) {
  return post<{ answer: string; route: unknown; turn: number }>(
    `/session/${sessionId}/query`,
    { patient_id: patientId, query }
  );
}
