import type { Citation, MedicationSafetyData } from '../types';
import CitationLink from './CitationLink';

interface MedicationSafetyRendererProps {
  data: MedicationSafetyData;
  narrative: string;
  citations: Citation[];
}

export default function MedicationSafetyRenderer({ data, narrative, citations }: MedicationSafetyRendererProps) {
  const citationsOf = (cls: string) => citations.filter((c) => c.claim_class === cls);

  return (
    <div style={{ fontSize: 13, color: '#333' }}>
      {data.allergies?.length > 0 && (
        <div style={{ marginBottom: 10, padding: '6px 8px', background: '#fff5f5', borderLeft: '3px solid #e74c3c', borderRadius: 3 }}>
          <strong style={{ fontSize: 11, textTransform: 'uppercase', color: '#c0392b' }}>Allergies</strong>
          <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
            {data.allergies.map((a, i) => (
              <li key={i}>
                {a}
                {citationsOf('allergy').slice(i, i + 1).map((c, j) => (
                  <CitationLink key={j} citation={c} />
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.interactions?.length > 0 && (
        <div style={{ marginBottom: 10, padding: '6px 8px', background: '#fffbf0', borderLeft: '3px solid #f39c12', borderRadius: 3 }}>
          <strong style={{ fontSize: 11, textTransform: 'uppercase', color: '#d68910' }}>Interactions of Concern</strong>
          <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
            {data.interactions.map((x, i) => <li key={i}>{x}</li>)}
          </ul>
        </div>
      )}

      {data.current_medications?.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.5, color: '#666', marginBottom: 3 }}>
            Current Medications
          </div>
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {data.current_medications.map((m, i) => (
              <li key={i}>
                {m}
                {citationsOf('medication').slice(i, i + 1).map((c, j) => (
                  <CitationLink key={j} citation={c} />
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {narrative && (
        <p style={{ margin: '6px 0 0', color: '#555', fontSize: 12, lineHeight: 1.4 }}>{narrative}</p>
      )}
    </div>
  );
}
