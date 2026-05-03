import { describe, test, expect } from 'vitest';
import { buildCitationUrl, resolvePatientPid } from './citations';
import type { Citation } from '../types';

const baseCitation: Citation = {
  patient_id: 'pt-001',
  resource_type: 'Observation',
  resource_id: 'obs-123',
  effective_datetime: '2026-04-30T08:00:00Z',
  value_summary: 'K+ 5.9 mEq/L',
  claim_class: 'lab_value',
};

describe('buildCitationUrl', () => {
  test('Observation returns results_report URL with resolved numeric pid', () => {
    const url = buildCitationUrl(baseCitation);
    expect(url).not.toBeNull();
    expect(url).toContain('pid=1');
    expect(url).toContain('results_report');
  });

  test('DiagnosticReport returns results_report URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'DiagnosticReport' });
    expect(url).toContain('results_report');
  });

  test('MedicationRequest returns medications section URL with resolved pid', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'MedicationRequest' });
    expect(url).not.toBeNull();
    expect(url).toContain('medications');
    expect(url).toContain('set_pid=1');
  });

  test('AllergyIntolerance returns allergies section URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'AllergyIntolerance' });
    expect(url).not.toBeNull();
    expect(url).toContain('allergies');
  });

  test('Condition returns problems section URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'Condition' });
    expect(url).not.toBeNull();
    expect(url).toContain('problems');
  });

  test('Encounter returns encounter_top URL', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'Encounter' });
    expect(url).not.toBeNull();
    expect(url).toContain('encounter_top');
  });

  test('Flag (isolation) returns patient summary URL with resolved pid', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'Flag', claim_class: 'isolation' });
    expect(url).not.toBeNull();
    expect(url).toContain('set_pid=1');
  });

  test('Unknown resource type returns null', () => {
    const url = buildCitationUrl({ ...baseCitation, resource_type: 'UnknownResource' });
    expect(url).toBeNull();
  });

  test('All URLs contain the resolved numeric pid (not the synthetic pt-NNN)', () => {
    const types = ['Observation', 'MedicationRequest', 'Condition', 'AllergyIntolerance', 'Encounter', 'DiagnosticReport', 'Flag'];
    for (const resource_type of types) {
      const url = buildCitationUrl({ ...baseCitation, resource_type });
      // OpenEMR's set_pid / pid query param requires the numeric integer pid,
      // not the synthetic "pt-001" identifier. Resolution happens inside
      // buildCitationUrl via resolvePatientPid().
      expect(url).toMatch(/(?:set_)?pid=1(?:#|$)/);
      expect(url).not.toContain('pt-001');
    }
  });

  test('regression: pt-001 resolves to numeric pid 1 in URL', () => {
    // OpenEMR's demographics_full.php?set_pid=N expects an integer. The
    // synthetic "pt-NNN" identifiers from FHIR fixtures must be converted
    // before being placed in a chart-navigation URL. See PatientCard.tsx
    // and CensusRenderer.openPatientChart for the parallel resolution path.
    expect(resolvePatientPid('pt-001')).toBe('1');
    expect(resolvePatientPid('pt-010')).toBe('10');
    const url = buildCitationUrl({ ...baseCitation, patient_id: 'pt-001', resource_type: 'MedicationRequest' });
    expect(url).toBe('/interface/patient_file/summary/demographics_full.php?set_pid=1#medications');
  });

  test('plain numeric pid is passed through unchanged', () => {
    const url = buildCitationUrl({ ...baseCitation, patient_id: '42', resource_type: 'Observation' });
    expect(url).toContain('pid=42');
  });
});
