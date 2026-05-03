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
        {!collapsed && (
          <div style={{ paddingLeft: 12, fontSize: 12, color: MUTED, fontStyle: 'italic' }}>
            Working on I-PASS summary for {patient.name}…
          </div>
        )}
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

  // Default behavior: every patient renders EXPANDED. The earlier
  // collapse-by-default + auto-expand-on-data approach made cards LOOK
  // like the stream was broken — patients appeared as collapsed name
  // rows and stayed that way until the auto-expand fired (or didn't,
  // for any patient whose chunk landed atypically). Just render
  // expanded; the user can manually collapse via the chevron, and the
  // bulk Collapse all / Expand all controls still work.
  useEffect(() => {
    if (!patients?.length) return;
    for (const p of patients) {
      seenPatientsRef.current.add(p.patient_id);
    }
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
