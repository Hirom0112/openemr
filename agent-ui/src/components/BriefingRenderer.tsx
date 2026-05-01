import type { BriefingSection, BriefingResponseSection, Citation } from '../types';
import DisclaimerIcon from './DisclaimerIcon';

const RED = { bg: '#FCEBEB', border: '#E24B4A', text: '#7F1D1D', secondary: '#991B1B' };
const AMB = { bg: '#FAEEDA', border: '#EF9F27', text: '#78350F', secondary: '#92400E' };
const NEU = { bg: '#F8F9FA', border: '#E5E7EB', text: '#374151', secondary: '#6B7280' };

type ColorToken = typeof NEU;

const SECTION_META: Record<string, { label: string; color: ColorToken }> = {
  diagnosis:   { label: 'Active problems',    color: NEU },
  vitals:      { label: 'Recent vitals',      color: NEU },
  labs:        { label: 'Labs',               color: AMB },
  medications: { label: 'Medications',        color: AMB },
  allergies:   { label: 'Allergies',          color: RED },
  alerts:      { label: 'Alerts',             color: RED },
};

interface BriefingRendererProps {
  data: BriefingSection;
  narrative: string;
  citations: Citation[];
}

function SectionLabel({ label, color }: { label: string; color: ColorToken }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 500, textTransform: 'uppercase',
      letterSpacing: '0.06em', color: color.text, marginBottom: 6, marginTop: 14,
    }}>
      {label}
    </div>
  );
}

function ClaimRow({ text, color }: { text: string; color: ColorToken }) {
  return (
    <div style={{
      background: color.bg, borderLeft: `3px solid ${color.border}`,
      borderRadius: 6, padding: '6px 10px', marginBottom: 3,
      fontSize: 13, color: color.text, lineHeight: 1.5,
    }}>
      {text}
    </div>
  );
}

function VitalPill({ text, color }: { text: string; color: ColorToken }) {
  return (
    <span style={{
      background: color.bg, color: color.text, border: `1px solid ${color.border}`,
      borderRadius: 999, padding: '3px 10px', fontSize: 12, fontWeight: 500,
    }}>
      {text}
    </span>
  );
}

function renderSection(sec: BriefingResponseSection) {
  const meta = SECTION_META[sec.section] ?? { label: sec.section, color: NEU };
  const { label, color } = meta;
  const isVitals = sec.section === 'vitals';

  const items = sec.claims.length > 0
    ? sec.claims.map((c) => c.text)
    : sec.summary
      ? [sec.summary]
      : [];

  if (items.length === 0) return null;

  return (
    <div key={sec.section}>
      <SectionLabel label={label} color={color} />
      {isVitals ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {items.map((text, i) => <VitalPill key={i} text={text} color={color} />)}
        </div>
      ) : (
        items.map((text, i) => <ClaimRow key={i} text={text} color={color} />)
      )}
    </div>
  );
}

export default function BriefingRenderer({ data, narrative, citations }: BriefingRendererProps) {
  if (!data?.sections) {
    return <p style={{ margin: 0, fontSize: 13, color: '#374151', lineHeight: 1.6 }}>{narrative}</p>;
  }

  return (
    <div style={{ fontSize: 13, fontFamily: 'inherit' }}>
      {/* Patient header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>
          {data.name}
        </div>
        {data.generated_at && (
          <div style={{ fontSize: 11, color: '#9CA3AF' }}>
            {new Date(data.generated_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })}
          </div>
        )}
      </div>

      {/* Hard alerts — always shown first */}
      {data.alerts?.length > 0 && (
        <div style={{
          background: RED.bg, border: `1px solid ${RED.border}`,
          borderRadius: 8, padding: '10px 12px', marginBottom: 10,
        }}>
          <div style={{
            fontSize: 11, fontWeight: 500, textTransform: 'uppercase',
            letterSpacing: '0.06em', color: RED.text, marginBottom: 6,
          }}>
            Alerts
          </div>
          {data.alerts.map((a, i) => (
            <div key={i} style={{ fontSize: 13, color: RED.text, lineHeight: 1.5 }}>
              {a}
            </div>
          ))}
        </div>
      )}

      {/* Sections in order returned by backend */}
      {data.sections.map((sec) => renderSection(sec))}

      <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid #e5e7eb', display: 'flex', justifyContent: 'flex-end' }}>
        <DisclaimerIcon citations={citations} date={data.generated_at} />
      </div>
    </div>
  );
}
