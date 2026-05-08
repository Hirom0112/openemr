<?php

/**
 * FhirConditionCopilotService.
 *
 * FHIR Condition sub-service that projects rows from the Co-Pilot's own
 * `copilot_conditions` table into FHIR R4 Condition resources, so
 * approved problem-list facts surface through the standard /Condition
 * search alongside core OpenEMR `lists` rows. Read-only; writes still
 * flow through ConditionController.php (the agent dispatch endpoint).
 *
 * Companion to FhirObservationCopilotService — same registry posture
 * (one-line addMappedService call in core FhirConditionService.php),
 * same QueryUtils pattern, same JOIN to patient_data for puuid, same
 * deterministic-id discipline (id is a literal copilot-string, not a
 * UUID, so we set FHIRId directly rather than going through
 * UuidRegistry).
 *
 * Registration is in src/Services/FHIR/FhirConditionService.php — see
 * the comment block there and agent-api/CLAUDE.md for the documented
 * exception to the "no core edits" rule (this is the second exception
 * after FhirObservationCopilotService; the pattern is identical).
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
use OpenEMR\FHIR\R4\FHIRDomainResource\FHIRCondition;
use OpenEMR\FHIR\R4\FHIRDomainResource\FHIRProvenance;
use OpenEMR\FHIR\R4\FHIRElement\FHIRId;
use OpenEMR\FHIR\R4\FHIRElement\FHIRMeta;
use OpenEMR\FHIR\R4\FHIRElement\FHIRReference;
use OpenEMR\FHIR\R4\FHIRElement\FHIRString;
use OpenEMR\Services\FHIR\Condition\Enum\FhirConditionCategory;
use OpenEMR\Services\FHIR\FhirProvenanceService;
use OpenEMR\Services\FHIR\FhirServiceBase;
use OpenEMR\Services\FHIR\IPatientCompartmentResourceService;
use OpenEMR\Services\FHIR\IResourceUSCIGProfileService;
use OpenEMR\Services\FHIR\Traits\FhirServiceBaseEmptyTrait;
use OpenEMR\Services\FHIR\Traits\VersionedProfileTrait;
use OpenEMR\Services\FHIR\UtilsService;
use OpenEMR\Services\Search\FhirSearchParameterDefinition;
use OpenEMR\Services\Search\FhirSearchWhereClauseBuilder;
use OpenEMR\Services\Search\SearchFieldException;
use OpenEMR\Services\Search\SearchFieldType;
use OpenEMR\Services\Search\ServiceField;
use OpenEMR\Validators\ProcessingResult;

class FhirConditionCopilotService extends FhirServiceBase implements IPatientCompartmentResourceService, IResourceUSCIGProfileService
{
    use FhirServiceBaseEmptyTrait;
    use VersionedProfileTrait;

    public const CATEGORY = 'problem-list-item';
    public const CATEGORY_SYSTEM = 'http://terminology.hl7.org/CodeSystem/condition-category';
    public const CLINICAL_STATUS_SYSTEM = 'http://terminology.hl7.org/CodeSystem/condition-clinical';
    public const VERIFICATION_STATUS_SYSTEM = 'http://terminology.hl7.org/CodeSystem/condition-ver-status';
    public const ICD10_SYSTEM = 'http://hl7.org/fhir/sid/icd-10-cm';
    public const SNOMED_SYSTEM = 'http://snomed.info/sct';

    public const USCDI_PROFILE = 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition-problems-health-concerns';

    private const TABLE = 'copilot_conditions';

    public function __construct($fhirApiURL = null)
    {
        parent::__construct($fhirApiURL);
    }

    public function supportsCategory($category): bool
    {
        // Same posture as FhirObservationCopilotService re category co-existence:
        // both this service and FhirConditionProblemListItemService run for
        // ?category=problem-list-item searches; both contribute rows; no
        // dedup needed because the deterministic copilot ids never collide
        // with core lists.uuid rows.
        return $category === self::CATEGORY;
    }

    public function supportsCode($code): bool
    {
        // Don't pre-filter by ICD-10 / SNOMED system — let the where-clause
        // builder do the binding. Mirrors FhirObservationCopilotService::supportsCode.
        return true;
    }

    protected function loadSearchParameters(): array
    {
        return [
            'patient' => $this->getPatientContextSearchField(),
            'code' => new FhirSearchParameterDefinition(
                'code',
                SearchFieldType::TOKEN,
                ['c.icd10_code']
            ),
            'category' => new FhirSearchParameterDefinition(
                'category',
                SearchFieldType::TOKEN,
                ['category']
            ),
            'clinical-status' => new FhirSearchParameterDefinition(
                'clinical-status',
                SearchFieldType::TOKEN,
                ['c.clinical_status']
            ),
            'verification-status' => new FhirSearchParameterDefinition(
                'verification-status',
                SearchFieldType::TOKEN,
                ['c.verification_status']
            ),
            // copilot_conditions.id is a deterministic string id (e.g. "copilot-444-i48-91"),
            // not a UUID — plain TOKEN match, no UuidRegistry round-trip.
            '_id' => new FhirSearchParameterDefinition('_id', SearchFieldType::TOKEN, ['c.id']),
            '_lastUpdated' => $this->getLastModifiedSearchField(),
        ];
    }

    public function getPatientContextSearchField(): FhirSearchParameterDefinition
    {
        return new FhirSearchParameterDefinition(
            'patient',
            SearchFieldType::REFERENCE,
            [new ServiceField('pd.uuid', ServiceField::TYPE_UUID)]
        );
    }

    public function getLastModifiedSearchField(): ?FhirSearchParameterDefinition
    {
        return new FhirSearchParameterDefinition('_lastUpdated', SearchFieldType::DATETIME, ['c.updated_at']);
    }

    protected function searchForOpenEMRRecords($openEMRSearchParameters): ProcessingResult
    {
        $processingResult = new ProcessingResult();

        try {
            // category is virtual — every copilot_conditions row projects as
            // category=problem-list-item. Drop it from the where clause so it
            // doesn't try to bind to a non-existent column.
            unset($openEMRSearchParameters['category']);

            $whereClause = FhirSearchWhereClauseBuilder::build($openEMRSearchParameters, true);
            $sqlFrag = $whereClause->getFragment();
            $bindArray = $whereClause->getBoundValues();

            $sql = 'SELECT
                        c.id, c.document_id, c.patient_id,
                        c.icd10_code, c.snomed_code, c.condition_text,
                        c.onset_date, c.clinical_status, c.verification_status,
                        c.fhir_resource, c.citations, c.created_at, c.updated_at,
                        pd.uuid AS patient_uuid,
                        d.uuid  AS document_uuid
                    FROM ' . self::TABLE . ' c
                    JOIN patient_data pd ON pd.pid = c.patient_id
                    LEFT JOIN documents d ON d.id  = c.document_id'
                . (empty($sqlFrag) ? '' : $sqlFrag)
                . ' ORDER BY c.updated_at DESC, c.id DESC';

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
            'icd10_code' => $r['icd10_code'] ?? null,
            'snomed_code' => $r['snomed_code'] ?? null,
            'condition_text' => $r['condition_text'] ?? '',
            'onset_date' => $r['onset_date'] ?? null,
            'clinical_status' => $r['clinical_status'] ?? 'active',
            'verification_status' => $r['verification_status'] ?? 'unconfirmed',
            'updated_at' => $r['updated_at'] ?? null,
            'citations' => $r['citations'] ?? null,
        ];
    }

    public function parseOpenEMRRecord($dataRecord = [], $encode = false)
    {
        $condition = new FHIRCondition();

        // id — literal copilot id, not a UUID. FHIR.id allows up to 64 chars
        // of [A-Za-z0-9-.] which matches our deterministic format.
        $id = new FHIRId();
        $id->setValue($dataRecord['id']);
        $condition->setId($id);

        // meta
        $meta = new FHIRMeta();
        $meta->setVersionId('1');
        if (!empty($dataRecord['updated_at'])) {
            $meta->setLastUpdated(UtilsService::getLocalDateAsUTC($dataRecord['updated_at']));
        } else {
            $meta->setLastUpdated(UtilsService::getDateFormattedAsUTC());
        }
        $meta = $this->addProfilesToMeta([self::USCDI_PROFILE], $meta);
        $condition->setMeta($meta);

        // category — always problem-list-item.
        $condition->addCategory(UtilsService::createCodeableConcept([
            self::CATEGORY => [
                'code' => self::CATEGORY,
                'system' => self::CATEGORY_SYSTEM,
                'description' => 'Problem List Item',
            ],
        ]));

        // clinicalStatus — required by USCDI.
        $clinical = (string) ($dataRecord['clinical_status'] ?? 'active');
        $condition->setClinicalStatus(UtilsService::createCodeableConcept([
            $clinical => [
                'code' => $clinical,
                'system' => self::CLINICAL_STATUS_SYSTEM,
                'description' => ucfirst($clinical),
            ],
        ]));

        // verificationStatus — required by USCDI.
        $verification = (string) ($dataRecord['verification_status'] ?? 'unconfirmed');
        $condition->setVerificationStatus(UtilsService::createCodeableConcept([
            $verification => [
                'code' => $verification,
                'system' => self::VERIFICATION_STATUS_SYSTEM,
                'description' => ucwords(str_replace('-', ' ', $verification)),
            ],
        ]));

        // code — project both ICD-10 and SNOMED codings when present, plus
        // the human-readable text. UtilsService::createCodeableConcept takes
        // a coding-keyed assoc array; we build one programmatically.
        $codingPairs = [];
        $icd10 = $dataRecord['icd10_code'] ?? null;
        if (is_string($icd10) && $icd10 !== '') {
            $codingPairs[$icd10] = [
                'code' => $icd10,
                'system' => self::ICD10_SYSTEM,
                'description' => $dataRecord['condition_text'] ?? $icd10,
            ];
        }
        $snomed = $dataRecord['snomed_code'] ?? null;
        if (is_string($snomed) && $snomed !== '') {
            $codingPairs[$snomed] = [
                'code' => $snomed,
                'system' => self::SNOMED_SYSTEM,
                'description' => $dataRecord['condition_text'] ?? $snomed,
            ];
        }
        if ($codingPairs !== []) {
            $codeable = UtilsService::createCodeableConcept($codingPairs);
            // Set text on top of the coding so chart viewers always have a
            // human-readable label.
            $text = (string) ($dataRecord['condition_text'] ?? '');
            if ($text !== '') {
                $textEl = new FHIRString();
                $textEl->setValue($text);
                $codeable->setText($textEl);
            }
            $condition->setCode($codeable);
        } else {
            // No structured codes — fall back to a text-only CodeableConcept
            // so the resource still validates against US Core (text is one
            // of the must-support paths for problems).
            $codeable = new \OpenEMR\FHIR\R4\FHIRElement\FHIRCodeableConcept();
            $textEl = new FHIRString();
            $textEl->setValue((string) ($dataRecord['condition_text'] ?? 'Problem'));
            $codeable->setText($textEl);
            $condition->setCode($codeable);
        }

        // subject — joined patient uuid, mirroring the Observation projection.
        if (!empty($dataRecord['puuid'])) {
            $condition->setSubject(UtilsService::createRelativeReference('Patient', $dataRecord['puuid']));
        }

        // onsetDateTime — only when the source persisted an ISO-parseable
        // value. Imprecise strings ("~2018", "adolescence") survive in the
        // private fhir_resource JSON and the citation note; we don't try
        // to coerce them into a FHIR dateTime.
        $onset = $dataRecord['onset_date'] ?? null;
        if (is_string($onset) && $onset !== '' && self::looksLikeIsoDate($onset)) {
            $condition->setOnsetDateTime(UtilsService::getLocalDateAsUTC($onset));
        }

        // derivedFrom — only when the source document landed in OpenEMR's
        // documents table. Mirrors the Observation pattern.
        if (!empty($dataRecord['document_uuid'])) {
            $ref = new FHIRReference();
            $ref->setReference(new FHIRString('DocumentReference/' . $dataRecord['document_uuid']));
            // Condition has no `derivedFrom`, so we encode the document
            // link via `evidence.detail` — the standard FHIR R4 way to
            // associate provenance to a Condition. Each evidence entry
            // carries a list of detail references; a single document ref
            // is the minimal shape.
            $evidence = new \OpenEMR\FHIR\R4\FHIRBackboneElement\FHIRConditionEvidence();
            $evidence->addDetail($ref);
            $condition->addEvidence($evidence);
        }

        // note — single Annotation summarising citation locators (PDF
        // bbox refs / DOCX paragraph anchors). Same pattern as the
        // Observation projection.
        if (!empty($dataRecord['citations'])) {
            $note = $this->buildCitationNote((string) $dataRecord['citations']);
            if ($note !== null) {
                $condition->addNote(['text' => $note]);
            }
        }

        return $condition;
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

    private static function looksLikeIsoDate(string $s): bool
    {
        // Accept full ISO-8601 datetimes and bare YYYY / YYYY-MM /
        // YYYY-MM-DD strings. Reject everything else (including
        // "~2018", "adolescence", "Unknown") so we don't pass an
        // unparseable value into UtilsService::getLocalDateAsUTC.
        return (bool) preg_match('/^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?)?)?$/', $s);
    }

    public function createProvenanceResource($dataRecord, $encode = false)
    {
        if (!($dataRecord instanceof FHIRCondition)) {
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

    protected function getSupportedVersions(): array
    {
        return ['', '7.0.0', '8.0.0'];
    }
}
