import type { BriefingSection, Citation } from '../types';
import CitationLink from './CitationLink';

interface BriefingRendererProps {
  data: BriefingSection;
  narrative: string;
  citations: Citation[];
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.5, color: '#666', marginBottom: 3 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

export default function BriefingRenderer({ data, narrative, citations }: BriefingRendererProps) {
  const citationsFor = (keyword: string) =>
    citations.filter((c) => c.claim_class === keyword || c.value_summary.toLowerCase().includes(keyword));

  return (
    <div style={{ fontSize: 13, color: '#333' }}>
      {data.clinical_summary && (
        <Section title="Summary">
          <p style={{ margin: 0, lineHeight: 1.5 }}>{data.clinical_summary}</p>
        </Section>
      )}

      {data.active_problems?.length > 0 && (
        <Section title="Active Problems">
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {data.active_problems.map((p, i) => (
              <li key={i} style={{ marginBottom: 2 }}>
                {p}
                {citationsFor('condition').slice(i, i + 1).map((c, j) => (
                  <CitationLink key={j} citation={c} />
                ))}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {data.recent_vitals && Object.keys(data.recent_vitals).length > 0 && (
        <Section title="Recent Vitals">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 12px' }}>
            {Object.entries(data.recent_vitals).map(([k, v]) => (
              <span key={k} style={{ fontSize: 12 }}>
                <strong>{k}:</strong> {v}
                {citationsFor('vital').slice(0, 1).map((c, i) => (
                  <CitationLink key={i} citation={c} />
                ))}
              </span>
            ))}
          </div>
        </Section>
      )}

      {data.medications?.length > 0 && (
        <Section title="Medications">
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {data.medications.map((m, i) => (
              <li key={i} style={{ marginBottom: 2 }}>
                {m}
                {citationsFor('medication').slice(i, i + 1).map((c, j) => (
                  <CitationLink key={j} citation={c} />
                ))}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {data.pending_results?.length > 0 && (
        <Section title="Pending Results">
          <ul style={{ margin: 0, paddingLeft: 16 }}>
            {data.pending_results.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </Section>
      )}

      {!data.clinical_summary && narrative && (
        <p style={{ margin: 0, lineHeight: 1.5, color: '#444' }}>{narrative}</p>
      )}
    </div>
  );
}
