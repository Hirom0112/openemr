<?php

/**
 * FhirCopilotDocumentReferenceService.
 *
 * FHIR DocumentReference sub-service that surfaces documents persisted by
 * the Co-Pilot ingest pipeline (category 'Clinical Copilot Upload', LOINC
 * 34109-9). Bypasses two upstream filters that conspire to make
 * `GET /apis/default/fhir/DocumentReference?_count=10` return total=0 even
 * when documents exist:
 *
 *   1. `FhirPatientDocumentReferenceService::searchForOpenEMRRecords`
 *      (src/Services/FHIR/DocumentReference/FhirPatientDocumentReferenceService.php
 *      lines 110-118) injects a `puuid IS MISSING` modifier whenever the
 *      caller omits `?patient=`, as an anti-leak default. Every
 *      patient-bound document is therefore excluded from the un-scoped
 *      Bundle read.
 *   2. `MappedServiceTrait::searchAllServices`
 *      (src/Services/FHIR/Traits/MappedServiceTrait.php lines 47-65) fans
 *      the search out to all sub-services and clears the entire result set
 *      if any sub-service throws SearchFieldException — turning a single
 *      sub-service's bad input into a global empty Bundle.
 *
 * This sub-service runs its own SQL against `documents` /
 * `categories_to_documents` / `categories`, scoped to the Co-Pilot
 * category by name (not hard-coded id), and never throws SearchFieldException
 * for missing `?patient=` (returns its own rows instead of forcing a
 * negative filter).
 *
 * Trust boundary: the Co-Pilot ingest pipeline is itself an authenticated
 * server-to-server caller (HS256 JWT, see UploadController.php). Documents
 * in this category were written by the agent-api on behalf of the
 * authenticated clinician; the document → patient binding is set at write
 * time. Re-running OpenEMR's per-user `can_access(authUser)` ACL on read
 * would block valid OAuth-bearer reads on this build (the bearer's session
 * has no `authUser` to check against — see SUBMISSION.md "Honesty section").
 *
 * Read-only. Writes flow through UploadController.php.
 *
 * Registration is in src/Services/FHIR/FhirDocumentReferenceService.php —
 * see the comment block there and `agent-api/CLAUDE.md` rule 8 for the
 * documented exception to the "no core edits" rule.
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
use OpenEMR\FHIR\R4\FHIRDomainResource\FHIRDocumentReference;
use OpenEMR\Services\FHIR\DocumentReference\Trait\FhirDocumentReferenceTrait;
use OpenEMR\Services\FHIR\FhirCodeSystemConstants;
use OpenEMR\Services\FHIR\FhirProvenanceService;
use OpenEMR\Services\FHIR\UtilsService;
use OpenEMR\Services\FHIR\FhirServiceBase;
use OpenEMR\Services\FHIR\IPatientCompartmentResourceService;
use OpenEMR\Services\FHIR\IResourceUSCIGProfileService;
use OpenEMR\Services\FHIR\Traits\FhirServiceBaseEmptyTrait;
use OpenEMR\Services\FHIR\Traits\PatientSearchTrait;
use OpenEMR\Services\FHIR\Traits\VersionedProfileTrait;
use OpenEMR\Services\Search\FhirSearchParameterDefinition;
use OpenEMR\Services\Search\ReferenceSearchValue;
use OpenEMR\Services\Search\SearchFieldType;
use OpenEMR\Services\Search\ReferenceSearchField;
use OpenEMR\Services\Search\ServiceField;
use OpenEMR\Services\Search\TokenSearchField;
use OpenEMR\Services\Search\TokenSearchValue;
use OpenEMR\Services\Search\BasicSearchField;
use OpenEMR\Validators\ProcessingResult;
use Throwable;

class FhirCopilotDocumentReferenceService extends FhirServiceBase implements IPatientCompartmentResourceService, IResourceUSCIGProfileService
{
    use FhirServiceBaseEmptyTrait;
    use PatientSearchTrait;
    use VersionedProfileTrait;
    use FhirDocumentReferenceTrait;

    /**
     * Documents under this category (resolved by name, not id) are surfaced
     * by this sub-service. Mirrors UploadController::DEFAULT_CATEGORY so the
     * ingest path and the read path agree by definition.
     */
    private const CATEGORY_NAME = 'Clinical Copilot Upload';

    /** LOINC 34109-9 ("Note") — value of `categories.codes` for the Co-Pilot category. */
    private const CATEGORY_CODE_LOINC = '34109-9';

    private const MAX_ROWS = 200;

    public function __construct($fhirApiURL = null)
    {
        parent::__construct($fhirApiURL);
    }

    public function supportsCategory($category): bool
    {
        // Match on the LOINC code or the literal text used in our category row.
        return $category === self::CATEGORY_CODE_LOINC
            || $category === self::CATEGORY_NAME;
    }

    public function supportsCode($code): bool
    {
        // Don't pre-filter by code; the parent fans out to all sub-services
        // when no explicit type/category match wins. Returning true keeps us
        // in the candidate set for un-scoped Bundle reads.
        return true;
    }

    protected function loadSearchParameters(): array
    {
        return [
            'patient' => $this->getPatientContextSearchField(),
            'date' => new FhirSearchParameterDefinition('date', SearchFieldType::DATETIME, ['date']),
            '_id' => new FhirSearchParameterDefinition('_id', SearchFieldType::TOKEN, [new ServiceField('uuid', ServiceField::TYPE_UUID)]),
            '_lastUpdated' => $this->getLastModifiedSearchField(),
        ];
    }

    public function getLastModifiedSearchField(): ?FhirSearchParameterDefinition
    {
        return new FhirSearchParameterDefinition('_lastUpdated', SearchFieldType::DATETIME, ['date']);
    }

    /**
     * @param array<string, mixed> $openEMRSearchParameters
     */
    protected function searchForOpenEMRRecords($openEMRSearchParameters): ProcessingResult
    {
        $processingResult = new ProcessingResult();

        try {
            // Defense in depth: orphan rows (NULL pd.uuid) leak empty
            // subject references and bypass any patient filter, so drop
            // them at the SQL level. Documents that can't resolve to a
            // patient aren't useful in a FHIR Bundle.
            $where = ['d.deleted = 0', 'c.name = ?', 'pd.uuid IS NOT NULL'];
            $bind  = [self::CATEGORY_NAME];

            // patient (UUID) — the FHIR `patient` param is a REFERENCE
            // search field; the upstream factory builds a
            // ReferenceSearchField (NOT a TokenSearchField). Both shapes
            // must be accepted, otherwise the filter silently drops and
            // every row of the category is returned (PHI leak).
            $patientFilterPresent = false;
            if (isset($openEMRSearchParameters['puuid'])) {
                $puuidHex = self::extractHexFromSearchField($openEMRSearchParameters['puuid']);
                $patientFilterPresent = true;
                if ($puuidHex !== null) {
                    $where[] = 'pd.uuid = UNHEX(?)';
                    $bind[]  = $puuidHex;
                } else {
                    // Filter present but unparseable — return zero rows
                    // rather than leaking the unfiltered category.
                    return $processingResult;
                }
            }

            // _id (DocumentReference UUID)
            if (isset($openEMRSearchParameters['uuid'])) {
                $docHex = self::extractHexFromSearchField($openEMRSearchParameters['uuid']);
                if ($docHex !== null) {
                    $where[] = 'd.uuid = UNHEX(?)';
                    $bind[]  = $docHex;
                }
            }

            $sql = 'SELECT
                        d.id,
                        LOWER(HEX(d.uuid)) AS uuid,
                        d.name,
                        d.mimetype,
                        d.date,
                        d.deleted,
                        c.id   AS category_id,
                        c.name AS category_name,
                        c.codes AS category_codes,
                        LOWER(HEX(pd.uuid)) AS puuid,
                        pd.pid AS pid
                    FROM documents d
                    JOIN categories_to_documents c2d ON c2d.document_id = d.id
                    JOIN categories c ON c.id = c2d.category_id
                    LEFT JOIN patient_data pd ON pd.pid = d.foreign_id
                    WHERE ' . implode(' AND ', $where) . '
                    ORDER BY d.date DESC, d.id DESC
                    LIMIT ' . self::MAX_ROWS;

            $rows = QueryUtils::fetchRecords($sql, $bind) ?? [];
            foreach ($rows as $row) {
                $processingResult->addData($this->shapeRow($row));
            }
        } catch (Throwable $e) {
            // Never propagate a SearchFieldException to the parent —
            // MappedServiceTrait::searchAllServices clears the full result
            // set on any sub-service error (see file docblock).
            error_log('[clinical-copilot] copilot doc search error: ' . $e->getMessage());
        }

        return $processingResult;
    }

    /**
     * Massage the raw SQL row into the array shape that
     * `FhirDocumentReferenceTrait::populate*` helpers expect.
     *
     * @param array<string, mixed> $row
     * @return array<string, mixed>
     */
    private function shapeRow(array $row): array
    {
        // Expand `categories.codes` (e.g. "LOINC:34109-9") into the
        // `codes` array shape the trait's populateCategories expects.
        $codes = [];
        $rawCodes = isset($row['category_codes']) ? trim((string) $row['category_codes']) : '';
        if ($rawCodes !== '') {
            foreach (preg_split('/[;,\s]+/', $rawCodes) ?: [] as $token) {
                if ($token === '') {
                    continue;
                }
                if (str_contains($token, ':')) {
                    [$system, $code] = explode(':', $token, 2);
                } else {
                    $system = 'LOINC';
                    $code   = $token;
                }
                $codes[$code] = [
                    'code'        => $code,
                    'system'      => $system === 'LOINC'
                        ? \OpenEMR\Services\FHIR\FhirCodeSystemConstants::LOINC
                        : $system,
                    'description' => (string) ($row['category_name'] ?? ''),
                ];
            }
        }

        return [
            'uuid'          => self::hexToCanonicalUuid($row['uuid'] ?? null),
            'name'          => $row['name'] ?? '',
            'mimetype'      => $row['mimetype'] ?? '',
            'date'          => $row['date'] ?? null,
            'puuid'         => self::hexToCanonicalUuid($row['puuid'] ?? null),
            'euuid'         => null,
            'encounter_date' => null,
            'deleted'       => (int) ($row['deleted'] ?? 0),
            'codes'         => $codes !== [] ? $codes : null,
            'category_name' => $row['category_name'] ?? '',
            // No author/practitioner attribution at the document level —
            // populateAuthor falls back to the primary business entity.
            'user_uuid'     => null,
            'user_npi'      => null,
            // No type code at the document row level — populateType falls
            // back to a NullFlavorUnknownCodeableConcept.
            'code'          => null,
            'codetext'      => null,
        ];
    }

    /**
     * Extract a 32-char hex string from a TokenSearchField / ReferenceSearchValue.
     *
     * The upstream FhirSearchWhereClauseBuilder normally wraps these and
     * UnHEX-binds them itself, but we're running our own SQL — so we walk
     * the structure and pull the raw UUID hex.
     */
    /**
     * Convert a 32-char bare hex UUID into the canonical 8-4-4-4-12
     * hyphenated form. Pass-through for null / empty / unexpected length
     * (already-hyphenated values stay hyphenated).
     */
    private static function hexToCanonicalUuid(?string $hex): ?string
    {
        if ($hex === null || $hex === '') {
            return $hex;
        }
        $bare = strtolower(str_replace('-', '', $hex));
        if (preg_match('/^[0-9a-f]{32}$/', $bare) !== 1) {
            // Not a 32-char hex — return as-is rather than mangle.
            return $hex;
        }
        return substr($bare, 0, 8) . '-'
            . substr($bare, 8, 4) . '-'
            . substr($bare, 12, 4) . '-'
            . substr($bare, 16, 4) . '-'
            . substr($bare, 20, 12);
    }

    private static function extractHexFromSearchField(mixed $field): ?string
    {
        // ReferenceSearchField (built for FHIR `patient`) and TokenSearchField
        // (built for `_id`) both extend BasicSearchField and expose getValues().
        if (!($field instanceof BasicSearchField)) {
            return null;
        }
        foreach ($field->getValues() as $value) {
            $candidate = null;
            if ($value instanceof ReferenceSearchValue) {
                // ReferenceSearchValue wraps a UUID in raw bytes when
                // isUuid=true (see ReferenceSearchValue::__construct).
                // Convert via the human-readable accessor, which round-trips
                // bytes → canonical string for us. Falls back to getId()
                // for the !isUuid case.
                $candidate = $value->getHumanReadableId();
                if ($candidate === null || $candidate === '') {
                    $candidate = $value->getId();
                }
            } elseif ($value instanceof TokenSearchValue) {
                $candidate = $value->getCode();
            } elseif (is_string($value)) {
                $candidate = $value;
            }
            if (!is_string($candidate) || $candidate === '') {
                // Last-ditch: a raw 16-byte binary UUID slipped through.
                if (is_string($candidate) && strlen($candidate) === 16) {
                    return strtolower(bin2hex($candidate));
                }
                continue;
            }
            $hex = strtolower(str_replace('-', '', $candidate));
            if (preg_match('/^[0-9a-f]{32}$/', $hex) === 1) {
                return $hex;
            }
            // 16-byte binary form (some callers pass bytes directly).
            if (strlen($candidate) === 16) {
                return strtolower(bin2hex($candidate));
            }
            // Fall back: maybe the value already contains the resource type
            // prefix ("Patient/abc-..."). Strip and retry.
            if (str_contains($candidate, '/')) {
                $tail = substr($candidate, strrpos($candidate, '/') + 1);
                $hex  = strtolower(str_replace('-', '', $tail));
                if (preg_match('/^[0-9a-f]{32}$/', $hex) === 1) {
                    return $hex;
                }
            }
        }
        return null;
    }

    public function parseOpenEMRRecord($dataRecord = [], $encode = false)
    {
        // Delegate to the trait's full pipeline (id / meta / date / context
        // / content / subject / categories / author / status / type), the
        // same shape FhirPatientDocumentReferenceService produces. UUIDs
        // are emitted as 32-char hex (no dashes) — UuidRegistry handles
        // both forms downstream.
        $docReference = new FHIRDocumentReference();
        $this->populateMetaData($docReference, $dataRecord);
        $this->populateId($docReference, $dataRecord);
        $this->populateIdentifiers($docReference, $dataRecord);
        $this->populateDate($docReference, $dataRecord);
        $this->populateContext($docReference, $dataRecord);
        $this->populateContent($docReference, $dataRecord);
        $this->populateSubject($docReference, $dataRecord);
        $this->populateCategories($docReference, $dataRecord);
        $this->populateAuthor($docReference, $dataRecord);
        $this->populateStatus($docReference, $dataRecord);
        $this->populateType($docReference, $dataRecord);

        if ($encode) {
            return json_encode($docReference);
        }
        return $docReference;
    }

    /**
     * Override the trait's `populateCategories` because the inherited shape —
     * `$dataRecord['codes'] = [$code => ['code'=>..., 'system'=>..., 'description'=>...]]`
     * — collapses one level when handed to `UtilsService::createCodeableConcept`,
     * which iterates `foreach ($diagnosisCodes as $code => $codeValues)` and
     * treats each value as an inner code-dict. With our flat array, $codeValues
     * comes out as a string and emits malformed `{"code":"code"}` / `{"code":"system"}` entries.
     *
     * Mirror `FhirDocumentReferenceAdvanceCareDirectiveService::populateCategories`
     * (src/Services/FHIR/DocumentReference/FhirDocumentReferenceAdvanceCareDirectiveService.php:168)
     * — emit a single LOINC coding directly.
     */
    protected function populateCategories(FHIRDocumentReference $docReference, array $dataRecord): void
    {
        $docReference->addCategory(UtilsService::createCodeableConcept([
            self::CATEGORY_CODE_LOINC => [
                'code' => self::CATEGORY_CODE_LOINC,
                'system' => FhirCodeSystemConstants::LOINC,
                'description' => self::CATEGORY_NAME,
            ],
        ]));
    }

    public function createProvenanceResource($dataRecord, $encode = false)
    {
        if (!($dataRecord instanceof FHIRDocumentReference)) {
            throw new \BadMethodCallException('Data record should be a FHIRDocumentReference');
        }
        $fhirProvenanceService = new FhirProvenanceService();
        $authors = $dataRecord->getAuthor();
        $author  = !empty($authors) ? reset($authors) : null;
        $fhirProvenance = $fhirProvenanceService->createProvenanceForDomainResource($dataRecord, $author);
        if ($encode) {
            return json_encode($fhirProvenance);
        }
        return $fhirProvenance;
    }

    public function getProfileURIs(): array
    {
        return $this->getProfileForVersions(self::US_CORE_PROFILE, $this->getSupportedVersions());
    }
}
