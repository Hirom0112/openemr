import type { Citation } from '../types';
import { buildCitationUrl } from '../utils/citations';

interface CitationLinkProps {
  citation: Citation;
}

const RESOURCE_ABBREV: Record<string, string> = {
  Observation: 'Obs',
  MedicationRequest: 'Rx',
  MedicationStatement: 'Rx',
  Condition: 'Dx',
  AllergyIntolerance: 'Allergy',
  Encounter: 'Enc',
  DiagnosticReport: 'Rpt',
  Patient: 'Pt',
  Flag: 'Flag',
};

const CLAIM_COLORS: Record<string, string> = {
  lab_value:   '#1a6fa8',
  vital:       '#1a8a6f',
  medication:  '#7b4fa8',
  condition:   '#a84f1a',
  allergy:     '#a81a1a',
  code_status: '#4f4fa8',
  isolation:   '#4fa84f',
};

export default function CitationLink({ citation }: CitationLinkProps) {
  const label = RESOURCE_ABBREV[citation.resource_type] ?? citation.resource_type.slice(0, 4);
  const color = CLAIM_COLORS[citation.claim_class] ?? '#555';
  const href  = buildCitationUrl(citation);

  const badge = (
    <span
      title={`${citation.value_summary}${citation.effective_datetime ? ` (${citation.effective_datetime})` : ''}`}
      style={{
        display: 'inline-block',
        fontSize: 10,
        fontWeight: 600,
        color: '#fff',
        background: color,
        borderRadius: 3,
        padding: '1px 4px',
        marginLeft: 2,
        verticalAlign: 'super',
        cursor: href ? 'pointer' : 'default',
        textDecoration: 'none',
      }}
    >
      {label}
    </span>
  );

  if (href) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none' }}>
        {badge}
      </a>
    );
  }
  return badge;
}
