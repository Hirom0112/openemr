<?php

/**
 * FhirObservationCopilotService.
 *
 * FHIR Observation sub-service that projects rows from the Co-Pilot's own
 * `copilot_observations` table into FHIR R4 Observation resources, so
 * approved facts surface through the standard /Observation search alongside
 * core OpenEMR procedure_result rows.
 *
 * Read-only. Writes still flow through ObservationController.php (the agent
 * dispatch endpoint).
 *
 * Registration is in src/Services/FHIR/FhirObservationService.php — see the
 * comment block there and agent-api/CLAUDE.md for the documented exception
 * to the "no core edits" rule.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2026 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot\FHIR;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\FHIR\R4\FHIRDomainResource\FHIRObservation;
use OpenEMR\FHIR\R4\FHIRDomainResource\FHIRProvenance;
use OpenEMR\FHIR\R4\FHIRElement\FHIRId;
use OpenEMR\FHIR\R4\FHIRElement\FHIRMeta;
use OpenEMR\FHIR\R4\FHIRElement\FHIRReference;
use OpenEMR\FHIR\R4\FHIRElement\FHIRString;
use OpenEMR\Services\FHIR\FhirCodeSystemConstants;
use OpenEMR\Services\FHIR\FhirProvenanceService;
use OpenEMR\Services\FHIR\FhirServiceBase;
use OpenEMR\Services\FHIR\IPatientCompartmentResourceService;
use OpenEMR\Services\FHIR\IResourceUSCIGProfileService;
use OpenEMR\Services\FHIR\Observation\Trait\FhirObservationTrait;
use OpenEMR\Services\FHIR\Traits\FhirServiceBaseEmptyTrait;
use OpenEMR\Services\FHIR\Traits\VersionedProfileTrait;
use OpenEMR\Services\FHIR\UtilsService;
use OpenEMR\Services\Search\FhirSearchParameterDefinition;
use OpenEMR\Services\Search\FhirSearchWhereClauseBuilder;
use OpenEMR\Services\Search\SearchFieldException;
use OpenEMR\Services\Search\SearchFieldType;
use OpenEMR\Services\Search\ServiceField;
use OpenEMR\Services\Search\TokenSearchField;
use OpenEMR\Services\Search\TokenSearchValue;
use OpenEMR\Validators\ProcessingResult;

class FhirObservationCopilotService extends FhirServiceBase implements IPatientCompartmentResourceService, IResourceUSCIGProfileService
{
    use FhirServiceBaseEmptyTrait;
    use VersionedProfileTrait;
    use FhirObservationTrait;

    public const DEFAULT_OBSERVATION_STATUS = 'final';
    public const CATEGORY = 'laboratory';
    public const USCDI_PROFILE = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-observation-lab';

    private const TABLE = 'copilot_observations';

    public function __construct($fhirApiURL = null)
    {
        parent::__construct($fhirApiURL);
    }

    public function getResourcePathForCode($code)
    {
        return 'category=' . self::CATEGORY . '&code=' . $code;
    }

    public function getCodeFromResourcePath($resourcePath)
    {
        $query_vars = [];
        parse_str((string) $resourcePath, $query_vars);
        return $query_vars['code'] ?? null;
    }

    public function supportsCategory($category): bool
    {
        return $category === self::CATEGORY;
    }

    public function supportsCode($code): bool
    {
        // Same posture as the core lab service: don't pre-filter by LOINC.
        // Lets ?code=718-7 route to us in addition to the core lab service —
        // both run, both contribute rows, no dedup needed (different ids).
        return true;
    }

    protected function loadSearchParameters(): array
    {
        return [
            'patient' => $this->getPatientContextSearchField(),
            'code' => new FhirSearchParameterDefinition('code', SearchFieldType::TOKEN, ['o.loinc_code']),
            'category' => new FhirSearchParameterDefinition('category', SearchFieldType::TOKEN, ['category']),
            'date' => new FhirSearchParameterDefinition('date', SearchFieldType::DATETIME, ['o.effective_date']),
            // copilot_observations.id is a deterministic string id (e.g. "copilot-739682725-718-7"),
            // not a UUID. Use a plain TOKEN match — no UuidRegistry round-trip.
            '_id' => new FhirSearchParameterDefinition('_id', SearchFieldType::TOKEN, ['o.id']),
            '_lastUpdated' => $this->getLastModifiedSearchField(),
        ];
    }

    public function getPatientContextSearchField(): FhirSearchParameterDefinition
    {
        return new FhirSearchParameterDefinition('patient', SearchFieldType::REFERENCE, [new ServiceField('pd.uuid', ServiceField::TYPE_UUID)]);
    }

    public function getLastModifiedSearchField(): ?FhirSearchParameterDefinition
    {
        return new FhirSearchParameterDefinition('_lastUpdated', SearchFieldType::DATETIME, ['o.updated_at']);
    }

    protected function searchForOpenEMRRecords($openEMRSearchParameters): ProcessingResult
    {
        $processingResult = new ProcessingResult();

        try {
            // category is virtual — every copilot_observations row projects as
            // category=laboratory. Drop it from the where clause so it doesn't
            // try to bind to a non-existent column.
            unset($openEMRSearchParameters['category']);

            // Strip the LOINC system prefix off ?code= values so the bound
            // value matches the bare code stored in copilot_observations.loinc_code.
            // Mirrors FhirObservationLaboratoryService::searchForOpenEMRRecords.
            if (!empty($openEMRSearchParameters['o.loinc_code']) && $openEMRSearchParameters['o.loinc_code'] instanceof TokenSearchField) {
                $openEMRSearchParameters['o.loinc_code']->transformValues(function ($tokenValue) {
                    if ($tokenValue instanceof TokenSearchValue && $tokenValue->getSystem() === FhirCodeSystemConstants::LOINC) {
                        return new TokenSearchValue($tokenValue->getCode());
                    }
                    return $tokenValue;
                });
            }

            $whereClause = FhirSearchWhereClauseBuilder::build($openEMRSearchParameters, true);
            $sqlFrag = $whereClause->getFragment();
            $bindArray = $whereClause->getBoundValues();

            $sql = 'SELECT
                        o.id, o.document_id, o.patient_id, o.loinc_code, o.loinc_display,
                        o.value_string, o.value_numeric, o.unit, o.effective_date,
                        o.citations, o.created_at, o.updated_at,
                        pd.uuid AS patient_uuid,
                        d.uuid  AS document_uuid
                    FROM ' . self::TABLE . ' o
                    JOIN patient_data pd ON pd.pid = o.patient_id
                    LEFT JOIN documents d ON d.id  = o.document_id'
                . (empty($sqlFrag) ? '' : $sqlFrag)
                . ' ORDER BY o.updated_at DESC, o.id DESC';

            $rows = QueryUtils::fetchRecords($sql, $bindArray) ?? [];
            foreach ($rows as $row) {
                $processingResult->addData($this->transformRow($row));
            }
        } catch (SearchFieldException $exception) {
            $processingResult->setValidationMessages([$exception->getField() => $exception->getMessage()]);
        }

        return $processingResult;
    }

    /**
     * @param array<string, mixed> $r
     * @return array<string, mixed>
     */
    private function transformRow(array $r): array
    {
        return [
            'id' => $r['id'],
            'puuid' => !empty($r['patient_uuid']) ? UuidRegistry::uuidToString($r['patient_uuid']) : null,
            'document_uuid' => !empty($r['document_uuid']) ? UuidRegistry::uuidToString($r['document_uuid']) : null,
            'loinc_code' => $r['loinc_code'] ?? '',
            'loinc_display' => $r['loinc_display'] ?? '',
            'value_string' => $r['value_string'] ?? null,
            'value_numeric' => isset($r['value_numeric']) && $r['value_numeric'] !== '' ? (float) $r['value_numeric'] : null,
            'unit' => $r['unit'] ?? null,
            'effective_date' => $r['effective_date'] ?? null,
            'updated_at' => $r['updated_at'] ?? null,
            'citations' => $r['citations'] ?? null,
        ];
    }

    public function parseOpenEMRRecord($dataRecord = [], $encode = false)
    {
        $observation = new FHIRObservation();

        // id — literal copilot id, not a UUID. FHIR.id allows up to 64 chars
        // of [A-Za-z0-9-.] which matches our deterministic format.
        $id = new FHIRId();
        $id->setValue($dataRecord['id']);
        $observation->setId($id);

        // meta
        $meta = new FHIRMeta();
        $meta->setVersionId('1');
        if (!empty($dataRecord['updated_at'])) {
            $meta->setLastUpdated(UtilsService::getLocalDateAsUTC($dataRecord['updated_at']));
        } else {
            $meta->setLastUpdated(UtilsService::getDateFormattedAsUTC());
        }
        $meta = $this->addProfilesToMeta([self::USCDI_PROFILE], $meta);
        $observation->setMeta($meta);

        // status
        $observation->setStatus(self::DEFAULT_OBSERVATION_STATUS);

        // category — always laboratory for v1 (schema has no category column).
        $observation->addCategory(UtilsService::createCodeableConcept([
            self::CATEGORY => [
                'code' => self::CATEGORY,
                'system' => FhirCodeSystemConstants::HL7_OBSERVATION_CATEGORY,
                'description' => 'Laboratory',
            ],
        ]));

        // code — keep loinc_code as-is, including the LP-UNKNOWN sentinel for
        // pre-Bug-4 rows. ONC requires text, so fall back to a generic display
        // when loinc_display is empty.
        $code = $dataRecord['loinc_code'] ?: 'LP-UNKNOWN';
        $display = $dataRecord['loinc_display'] ?: 'Unknown laboratory analyte';
        $observation->setCode(UtilsService::createCodeableConcept([
            $code => [
                'code' => $code,
                'system' => FhirCodeSystemConstants::LOINC,
                'description' => $display,
            ],
        ]));

        // subject
        if (!empty($dataRecord['puuid'])) {
            $observation->setSubject(UtilsService::createRelativeReference('Patient', $dataRecord['puuid']));
        }

        // effective
        if (!empty($dataRecord['effective_date'])) {
            $observation->setEffectiveDateTime(UtilsService::getLocalDateAsUTC($dataRecord['effective_date']));
        } else {
            $observation->setEffectiveDateTime(UtilsService::createDataMissingExtension());
        }

        // value — prefer numeric quantity; fall back to string; missing
        // surfaces as a data-missing extension on valueString.
        if ($dataRecord['value_numeric'] !== null) {
            $observation->setValueQuantity(UtilsService::createQuantity(
                $dataRecord['value_numeric'],
                $dataRecord['unit'] ?? '',
                $dataRecord['unit'] ?? ''
            ));
        } elseif (!empty($dataRecord['value_string'])) {
            $observation->setValueString($dataRecord['value_string']);
        } else {
            $observation->setValueString(UtilsService::createDataMissingExtension());
        }

        // derivedFrom — only when the source document landed in OpenEMR's
        // documents table (post-Bug-1). Pre-Bug-1 rows were written via
        // local-disk fallback and have no real DocumentReference; skip
        // rather than emit a stub.
        if (!empty($dataRecord['document_uuid'])) {
            $ref = new FHIRReference();
            $ref->setReference(new FHIRString('DocumentReference/' . $dataRecord['document_uuid']));
            $observation->addDerivedFrom($ref);
        }

        // note — single Annotation summarising citation locators (HL7 segment
        // ids or PDF bbox refs). Structured citations remain on the staging
        // payload; this is a human-readable provenance hint for chart viewers.
        if (!empty($dataRecord['citations'])) {
            $note = $this->buildCitationNote($dataRecord['citations']);
            if ($note !== null) {
                $observation->addNote(['text' => $note]);
            }
        }

        return $observation;
    }

    private function buildCitationNote(string $citationsJson): ?string
    {
        $decoded = json_decode($citationsJson, true);
        if (!is_array($decoded) || $decoded === []) {
            return null;
        }
        $locators = [];
        foreach ($decoded as $cit) {
            if (!is_array($cit)) {
                continue;
            }
            $locator = $cit['bbox_id'] ?? $cit['field_or_chunk_id'] ?? null;
            if (is_string($locator) && $locator !== '') {
                $locators[] = $locator;
            }
        }
        if ($locators === []) {
            return null;
        }
        return 'Source: ' . implode(', ', $locators);
    }

    /**
     * Provenance — performer omitted for v1. copilot_observations does not
     * carry the approving clinician's identity (decided_by lives on the
     * staging row, not here). Revisit if USCDI compliance demands it.
     */
    public function createProvenanceResource($dataRecord, $encode = false)
    {
        if (!($dataRecord instanceof FHIRObservation)) {
            throw new \BadMethodCallException('Data record should be correct instance class');
        }
        $fhirProvenanceService = new FhirProvenanceService();
        $fhirProvenance = $fhirProvenanceService->createProvenanceForDomainResource($dataRecord, null);
        if ($encode) {
            return json_encode($fhirProvenance);
        }
        return $fhirProvenance;
    }

    public function getProfileURIs(): array
    {
        return $this->getProfileForVersions(self::USCDI_PROFILE, $this->getSupportedVersions());
    }
}
