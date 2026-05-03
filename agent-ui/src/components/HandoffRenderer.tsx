import { useCallback, useEffect, useRef, useState } from 'react';
import type { Citation, HandoffData, HandoffPatient } from '../types';
import { RED, NEU, MUTED, AMB } from '../styles/tokens';
import { SectionHeading, ClaimRow, PatientRow, Markdown, CitationFooter } from './primitives';

interface HandoffRendererProps {
  data: HandoffData;
  narrative: string;
  citations: Citation[];
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

interface HandoffPatientBlockProps {
  patient: HandoffPatient;
  collapsed: boolean;
  onToggle: (patientId: string) => void;
}

// Cycling progress stages shown under each pending patient. Mirrors the
// chat-level ThinkingIndicator pattern but per-patient and per-stage.
// Each stage shows for ~1200ms; the final stage holds until the chunk
// arrives. Total elapsed ~3.6s before the user sees "Generating handoff…",
// matching the typical per-patient backend time when bundle is warm.
const PENDING_STAGES: readonly string[] = [
  'Pulling chart…',
  'Reviewing recent labs…',
  'Generating handoff…',
];
const PENDING_STAGE_INTERVAL_MS = 1200;

// When the cascade auto-expand needs to open multiple patient cards in
// the same render tick (cache-hit case where chunks arrive within ms of
// each other), space them by this many ms so the user perceives the
// expansion as a smooth wave rather than a synchronous flip.
const EXPAND_STAGGER_MS = 400;

function PendingPatientStatus({ patientName }: { patientName: string }) {
  const [stageIdx, setStageIdx] = useState(0);
  useEffect(() => {
    setStageIdx(0);
    const id = window.setInterval(() => {
      setStageIdx((cur) => (cur < PENDING_STAGES.length - 1 ? cur + 1 : cur));
    }, PENDING_STAGE_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, []);
  return (
    <div
      role="status"
      aria-live="polite"
      style={{ paddingLeft: 12, fontSize: 12, color: MUTED, fontStyle: 'italic' }}
    >
      {PENDING_STAGES[stageIdx]} ({patientName})
    </div>
  );
}

function HandoffPatientBlock({ patient, collapsed, onToggle }: HandoffPatientBlockProps) {
  const [hovered, setHovered] = useState(false);

  const headerLabel = patient.pending
    ? 'Generating handoff…'
    : patient.error
    ? 'Handoff unavailable'
    : patient.status;

  const color = patient.error ? AMB : NEU;

  const chevron = (
    <span
      style={{ marginLeft: 'auto', fontSize: 12, color: MUTED, paddingLeft: 8 }}
      aria-hidden="true"
    >
      {collapsed ? '▸' : '▾'}
    </span>
  );

  const headerButton = (
    <button
      type="button"
      role="button"
      aria-expanded={!collapsed}
      aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${patient.name} handoff`}
      onClick={() => onToggle(patient.patient_id)}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setHovered(true)}
      onBlur={() => setHovered(false)}
      style={{
        display: 'block',
        width: '100%',
        padding: 0,
        margin: 0,
        border: 'none',
        background: hovered ? '#f3f4f6' : 'transparent',
        borderRadius: 6,
        cursor: 'pointer',
        textAlign: 'left',
        fontFamily: 'inherit',
        transition: 'background 0.12s',
      }}
    >
      <PatientRow
        color={color}
        title={patient.name}
        subtitle={headerLabel}
        actions={chevron}
      />
    </button>
  );

  if (patient.pending) {
    return (
      <div style={{ marginBottom: 8 }}>
        {headerButton}
        {!collapsed && <PendingPatientStatus patientName={patient.name} />}
      </div>
    );
  }

  if (patient.error) {
    return (
      <div style={{ marginBottom: 8 }}>
        {headerButton}
        {!collapsed && (
          <div style={{ paddingLeft: 12, fontSize: 12, color: AMB.text }}>
            {patient.error}
          </div>
        )}
      </div>
    );
  }

  const hasActive = patient.active_issues && patient.active_issues.length > 0;
  const hasPending = patient.pending_items && patient.pending_items.length > 0;
  const hasEscalate = patient.escalation_triggers && patient.escalation_triggers.length > 0;

  return (
    <div style={{ marginBottom: 8 }}>
      {headerButton}
      {!collapsed && (
        <div style={{ paddingLeft: 12 }}>
          {hasActive && (
            <>
              <SectionHeading color={RED}>Active issues</SectionHeading>
              {patient.active_issues.map((it, i) => (
                <ClaimRow key={i} color={RED}>{it}</ClaimRow>
              ))}
            </>
          )}

          {hasPending && (
            <>
              <SectionHeading color={NEU}>Pending</SectionHeading>
              {patient.pending_items.map((it, i) => (
                <ClaimRow key={i} color={NEU}>{it}</ClaimRow>
              ))}
            </>
          )}

          {hasEscalate && (
            <>
              <SectionHeading color={RED}>Escalate if</SectionHeading>
              {patient.escalation_triggers.map((t, i) => (
                <ClaimRow key={i} color={RED}>{t}</ClaimRow>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function isDataReady(p: HandoffPatient): boolean {
  // Placeholder rows carry pending=true; once the SSE chunk lands the
  // dispatcher replaces the row with real data (pending falsy) or an error
  // card. Either transition counts as "ready" — we want errors to auto-expand
  // too so the failure message is visible.
  return !p.pending;
}

export default function HandoffRenderer({ data, narrative, citations }: HandoffRendererProps) {
  const count = citations?.length ?? 0;
  const [collapsedPatients, setCollapsedPatients] = useState<Set<string>>(new Set());
  const seenPatientsRef = useRef<Set<string>>(new Set());
  const userTouchedIdsRef = useRef<Set<string>>(new Set());

  const patients = data?.patients;
  // Tracks the wall-clock time of the last auto-expand so we can enforce
  // a MINIMUM gap between cards opening — even when chunks arrive ~5s
  // apart in live streaming, we still want each card to take a beat
  // before opening so the user perceives the cascade. Without this, a
  // single chunk landing per render-tick fires immediately and there's
  // no visual cascade at all (looks like cards just "appear" expanded).
  const lastExpandAtRef = useRef<number>(0);

  // Cascade auto-expand with always-visible stagger: every patient defaults
  // to COLLAPSED on first sighting. A patient auto-expands only when (a)
  // its own data is ready AND (b) every patient ABOVE it is also ready.
  //
  // Stagger applies in BOTH cases: (1) cache-hit batches where multiple
  // chunks land in the same tick — uses idx * EXPAND_STAGGER_MS; (2) live
  // streaming where chunks arrive 3-5s apart — enforces a minimum
  // EXPAND_STAGGER_MS gap from the previous expansion. Either way the
  // user always sees a deliberate top-to-bottom reveal.
  //
  // Manual user toggles win — anything in userTouchedIdsRef is left
  // alone forever.
  useEffect(() => {
    if (!patients?.length) return;
    // Step 1: compute newly-seen patients and which ones should expand.
    // Done OUTSIDE setState so we can call setCollapsedPatients later from
    // the timer callbacks without nesting setState calls (which would race:
    // the inner updater reads stale `cur` and the outer return overwrites
    // it — that bug stranded the first patient in collapsed state).
    const newlySeen: string[] = [];
    const toExpand: string[] = [];
    let allPriorReady = true;
    for (const p of patients) {
      const id = p.patient_id;
      const wasSeen = seenPatientsRef.current.has(id);
      if (!wasSeen) {
        seenPatientsRef.current.add(id);
        if (!userTouchedIdsRef.current.has(id)) {
          newlySeen.push(id);
        }
      }
      const ready = isDataReady(p);
      const shouldExpand = ready && allPriorReady && !userTouchedIdsRef.current.has(id);
      if (shouldExpand) {
        toExpand.push(id);
      }
      if (!ready) {
        allPriorReady = false;
      }
    }

    // Step 2: add newly-seen patients to collapsed state.
    if (newlySeen.length > 0) {
      setCollapsedPatients((prev) => {
        let changed = false;
        const next = new Set(prev);
        for (const id of newlySeen) {
          if (!next.has(id)) {
            next.add(id);
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    }

    // Step 3: schedule expansions with always-visible cascade. Every patient
    // — including idx 0 — defers through setTimeout so the collapsed-set
    // write from Step 2 commits first.
    const now = Date.now();
    const earliestNext = Math.max(now, lastExpandAtRef.current + EXPAND_STAGGER_MS);
    const expandedTimers: number[] = [];
    toExpand.forEach((id, idx) => {
      const fireAt = earliestNext + idx * EXPAND_STAGGER_MS;
      const delay = Math.max(0, fireAt - now);
      const t = window.setTimeout(() => {
        setCollapsedPatients((cur) => {
          if (!cur.has(id)) return cur;
          const updated = new Set(cur);
          updated.delete(id);
          return updated;
        });
        lastExpandAtRef.current = Date.now();
      }, delay);
      expandedTimers.push(t);
    });
    return () => {
      for (const t of expandedTimers) window.clearTimeout(t);
    };
  }, [patients]);

  const togglePatient = useCallback((patientId: string) => {
    userTouchedIdsRef.current.add(patientId);
    setCollapsedPatients((prev) => {
      const next = new Set(prev);
      if (next.has(patientId)) {
        next.delete(patientId);
      } else {
        next.add(patientId);
      }
      return next;
    });
  }, []);

  const collapseAll = useCallback(() => {
    if (!data?.patients?.length) return;
    for (const p of data.patients) {
      userTouchedIdsRef.current.add(p.patient_id);
    }
    setCollapsedPatients(new Set(data.patients.map((p) => p.patient_id)));
  }, [data]);

  const expandAll = useCallback(() => {
    if (data?.patients?.length) {
      for (const p of data.patients) {
        userTouchedIdsRef.current.add(p.patient_id);
      }
    }
    setCollapsedPatients(new Set());
  }, [data]);

  if (!data?.patients?.length) {
    return (
      <>
        <Markdown narrative={narrative} citations={citations} />
        <CitationFooter count={count}>
          <CitationsList citations={citations} />
        </CitationFooter>
      </>
    );
  }

  const bulkLinkStyle: React.CSSProperties = {
    background: 'none',
    border: 'none',
    padding: 0,
    margin: 0,
    fontSize: 11,
    color: MUTED,
    cursor: 'pointer',
    fontFamily: 'inherit',
    textDecoration: 'underline',
  };

  return (
    <div style={{ fontSize: 13, color: NEU.text, fontFamily: 'inherit' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>Shift handoff</div>
        {data.shift_end_time && (
          <span style={{ fontSize: 11, color: MUTED }}>{data.shift_end_time}</span>
        )}
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <button type="button" style={bulkLinkStyle} onClick={collapseAll}>
          Collapse all
        </button>
        <span style={{ fontSize: 11, color: MUTED }}>·</span>
        <button type="button" style={bulkLinkStyle} onClick={expandAll}>
          Expand all
        </button>
      </div>

      {data.patients.map((pt) => (
        <HandoffPatientBlock
          key={pt.patient_id}
          patient={pt}
          collapsed={collapsedPatients.has(pt.patient_id)}
          onToggle={togglePatient}
        />
      ))}

      <CitationFooter count={count}>
        <CitationsList citations={citations} />
      </CitationFooter>
    </div>
  );
}
