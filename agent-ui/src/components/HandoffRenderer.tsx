import type { Citation, HandoffData } from '../types';

interface HandoffRendererProps {
  data: HandoffData;
  narrative: string;
  citations: Citation[];
}

export default function HandoffRenderer({ data, narrative }: HandoffRendererProps) {
  if (!data?.patients?.length) {
    return <p style={{ fontSize: 13, color: '#555', margin: 0 }}>{narrative}</p>;
  }

  return (
    <div style={{ fontSize: 13, color: '#333' }}>
      {data.shift_end_time && (
        <div style={{ fontSize: 11, color: '#888', marginBottom: 8 }}>
          Shift end: {data.shift_end_time}
        </div>
      )}
      {data.patients.map((pt) => (
        <div key={pt.patient_id} style={{ marginBottom: 14, paddingBottom: 12, borderBottom: '1px solid #eee' }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{pt.name}</div>
          <p style={{ margin: '0 0 6px', lineHeight: 1.5, color: '#444' }}>{pt.status}</p>

          {pt.active_issues?.length > 0 && (
            <div style={{ marginBottom: 4 }}>
              <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: '#666' }}>Active Issues: </span>
              {pt.active_issues.join(' · ')}
            </div>
          )}
          {pt.pending_items?.length > 0 && (
            <div style={{ marginBottom: 4 }}>
              <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: '#666' }}>Pending: </span>
              {pt.pending_items.join(' · ')}
            </div>
          )}
          {pt.escalation_triggers?.length > 0 && (
            <div style={{ color: '#c0392b', fontSize: 12 }}>
              <span style={{ fontWeight: 700 }}>Escalate if: </span>
              {pt.escalation_triggers.join('; ')}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
