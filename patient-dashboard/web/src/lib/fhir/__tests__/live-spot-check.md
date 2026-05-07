# Live FHIR client spot-check

Deferred contract: this file records the curl commands + expected response
shapes that will be exercised against the live OpenEMR after Phase 3.2 lands
(`src/auth.ts` Auth.js wiring + a logged-in browser session). One block per
`FhirClient` method.

Reference patient: Gloria Tran, pid=4, uuid=`a1af78de-c153-42e7-89b2-55749a3da9ca`
(verified 1.5, 2026-05-07).

Common shell setup (mint a token via password grant exactly as the 1.5 pass
did — see `auth-notes.md` for caveats; this is dev-only):

```bash
TOKEN=$(curl -sk -X POST https://localhost:9300/oauth2/default/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "grant_type=password&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&user_role=users&scope=openid%20fhirUser%20user/Patient.read%20user/AllergyIntolerance.read%20user/Condition.read%20user/MedicationRequest.read%20user/CareTeam.read%20user/Observation.read&username=$OPENEMR_USER&password=$OPENEMR_PASS" \
  | jq -r .access_token)
```

---

### `getPatientByPid("4")`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/Patient?identifier=4'
expect: bundle.entry[0].resource.id      = "a1af78de-c153-42e7-89b2-55749a3da9ca"
        bundle.entry[0].resource.name[0].family    = "Tran"
        bundle.entry[0].resource.name[0].given[0]  = "Gloria"
        bundle.entry[0].resource.birthDate         = "1958-09-03"
        bundle.entry[0].resource.gender            = "female"
```

### `getPatient("a1af78de-c153-42e7-89b2-55749a3da9ca")`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/Patient/a1af78de-c153-42e7-89b2-55749a3da9ca'
expect: resource.resourceType = "Patient"
        resource.id           = "a1af78de-c153-42e7-89b2-55749a3da9ca"
        resource.name[0].family = "Tran"
        resource.birthDate    = "1958-09-03"
```

### `getAllergyIntolerances("a1af78de-c153-42e7-89b2-55749a3da9ca")`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/AllergyIntolerance?patient=a1af78de-c153-42e7-89b2-55749a3da9ca'
expect: bundle.entry[].resource.resourceType        = "AllergyIntolerance"
        bundle.entry[0].resource.clinicalStatus.coding[0].code     = "active"
        bundle.entry[0].resource.verificationStatus.coding[0].code = "confirmed"
        bundle.entry[0].resource.code.coding[0].display            = "Unknown"  # data-absent-reason
        bundle.entry[0].resource.text.div                          contains "Sulfonamide"
        # criticality is absent on Gloria's record — see allergyDisplay() fallback rule
```

### `getConditions("a1af78de-...", { category: "problem-list-item" })`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/Condition?patient=a1af78de-c153-42e7-89b2-55749a3da9ca&category=problem-list-item'
expect: bundle.entry[0].resource.resourceType                      = "Condition"
        bundle.entry[0].resource.code.text                         = "Heart failure"
        bundle.entry[0].resource.clinicalStatus.coding[0].code     = "active"
        bundle.entry[0].resource.onsetDateTime                     populated
```

### `getMedicationRequests("a1af78de-...")` — empirical 1.5 result

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/MedicationRequest?patient=a1af78de-c153-42e7-89b2-55749a3da9ca'
expect: every bundle.entry[].resource.intent  = "plan"     # NOT "order" (known limitation)
        every bundle.entry[].resource.requester is absent
        bundle.entry[].resource.medicationCodeableConcept.text contains "Furosemide" or "Lisinopril"
# Consequence: filterPrescriptions(...) returns [] for Gloria, matching the
# original dashboard's "None" empty state.
```

### `getCareTeams("a1af78de-...")`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/CareTeam?patient=a1af78de-c153-42e7-89b2-55749a3da9ca&status=active'
expect: bundle.total = 0   # synthetic dataset has no care_teams rows for any patient
        bundle.entry omitted or empty
```

### `getObservations("a1af78de-...", { category: "vital-signs" })`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/Observation?patient=a1af78de-c153-42e7-89b2-55749a3da9ca&category=vital-signs'
expect: bundle.entry length = 15        # 1.5 verified count
        at least one entry with code.coding[0].code = "85354-9"  (BP panel; uses component[])
        at least one entry with code.coding[0].code = "8867-4"   (heart rate; valueQuantity)
        at least one entry with code.coding[0].code = "9279-1"   (respiratory rate)
        every entry has effectiveDateTime populated
```

### `getObservations("a1af78de-...", { category: "vital-signs", sort: "-date", count: 1 })`

```
curl: curl -sk -H "Authorization: Bearer $TOKEN" \
  'https://localhost:9300/apis/default/fhir/Observation?patient=a1af78de-c153-42e7-89b2-55749a3da9ca&category=vital-signs&_sort=-date&_count=1'
expect: bundle.entry length <= 1
# Open question Q8 (api-map): confirm OpenEMR honours _sort/_count on
# Observation. If it doesn't, the Vitals card must fetch the full bundle and
# sort client-side.
```
