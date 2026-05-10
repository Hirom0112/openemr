<?php

/**
 * Project Co-Pilot shadow tables (copilot_conditions, copilot_observations)
 * into the legacy `lists` table so the OpenEMR demographics chart sidebar
 * (Allergies / Medical Problems / Medications cards) reflects the same data
 * the Modern Dashboard sees.
 *
 * Idempotent: rerunning never duplicates rows. We tag every projected row
 * with `lists.extrainfo = 'copilot:<source_id>'` and use that tag as the
 * dedupe key.
 *
 * Mapping:
 *   - copilot_conditions (verification_status != 'entered-in-error')
 *       -> lists.type = 'medical_problem'
 *   - copilot_observations whose LOINC code matches the allergies panel
 *       (LOINC 48765-2 "Allergies and adverse reactions Document",
 *        or category text == 'allergy')
 *       -> lists.type = 'allergy'
 *   - copilot_observations whose LOINC code matches medication-list panels
 *       (LOINC 10160-0 "History of Medication use Narrative")
 *       -> lists.type = 'medication'
 *
 * Today the live agent-api persists allergies / medications inside the raw
 * extraction JSON only — the `copilot_observations` table is currently
 * lab-LOINC driven — so the medication / allergy projections are no-ops
 * until the writer starts emitting those LOINCs. The script tolerates this:
 * it walks every observation row but only projects rows whose LOINC code is
 * recognised. New LOINCs added later require no script change.
 *
 * Connection:
 *   - Locally:  reads MYSQL_HOST/USER/PASS/DB env vars.
 *               Falls back to root@127.0.0.1:3306 for the dev compose stack
 *               when no env is supplied (pass MYSQL_HOST=mysql.railway.internal
 *               etc. inside Railway).
 *
 * Usage:
 *   php scripts/project_copilot_to_lists.php [--pid=27] [--dry-run]
 *
 *   --pid       Limit projection to a single patient_id. Default: all.
 *   --dry-run   Print INSERT plan but skip writes.
 *
 * @package   OpenEMR
 * @author    Clinical Co-Pilot
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU GPL 3
 */

declare(strict_types=1);

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

$opts = getopt('', ['pid::', 'dry-run']);
$onlyPid = isset($opts['pid']) && $opts['pid'] !== '' && $opts['pid'] !== false
    ? (int) $opts['pid']
    : null;
$dryRun = array_key_exists('dry-run', $opts);

// ---------------------------------------------------------------------------
// DB connection
// ---------------------------------------------------------------------------

function envOr(string $key, string $fallback): string
{
    $val = getenv($key);
    if ($val === false || $val === '') {
        return $fallback;
    }
    return $val;
}

$host    = envOr('MYSQL_HOST', '127.0.0.1');
$port    = (int) envOr('MYSQL_PORT', '3306');
$user    = envOr('MYSQL_USER', 'root');
$pass    = envOr('MYSQL_PASS', envOr('MYSQL_PASSWORD', 'root'));
$dbName  = envOr('MYSQL_DATABASE', envOr('MYSQL_DB', 'openemr'));

$mysqli = @new mysqli($host, $user, $pass, $dbName, $port);
if ($mysqli->connect_errno !== 0) {
    fwrite(STDERR, sprintf(
        "[project] DB connect failed (%s@%s:%d/%s): %s\n",
        $user,
        $host,
        $port,
        $dbName,
        $mysqli->connect_error
    ));
    exit(1);
}
$mysqli->set_charset('utf8mb4');

fwrite(STDOUT, sprintf(
    "[project] connected %s@%s:%d/%s pid_filter=%s dry_run=%s\n",
    $user,
    $host,
    $port,
    $dbName,
    $onlyPid !== null ? (string) $onlyPid : 'ALL',
    $dryRun ? 'YES' : 'no'
));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function tagOf(string $sourceTable, string $sourceId): string
{
    // Bound to varchar(255) extrainfo. copilot_*.id is varchar(128) so this
    // always fits with margin for the prefix.
    return 'copilot:' . $sourceTable . ':' . $sourceId;
}

/**
 * @param array<string, scalar|null> $assignments column => value (already escaped via params)
 */
function upsertList(
    mysqli $db,
    string $tag,
    array $assignments,
    bool $dryRun
): string {
    // Idempotency key: extrainfo = $tag.
    $stmt = $db->prepare('SELECT id FROM lists WHERE extrainfo = ? LIMIT 1');
    if ($stmt === false) {
        throw new RuntimeException('prepare failed: ' . $db->error);
    }
    $stmt->bind_param('s', $tag);
    $stmt->execute();
    $res = $stmt->get_result();
    $existing = $res ? $res->fetch_assoc() : null;
    $stmt->close();

    if ($existing !== null) {
        return 'skip';
    }
    if ($dryRun) {
        return 'plan';
    }

    $assignments['extrainfo'] = $tag;

    $cols = array_keys($assignments);
    $placeholders = implode(', ', array_map(static fn($c) => "`$c` = ?", $cols));
    $sql = 'INSERT INTO lists SET date = NOW(), modifydate = NOW(), ' . $placeholders;

    $stmt = $db->prepare($sql);
    if ($stmt === false) {
        throw new RuntimeException('prepare insert failed: ' . $db->error);
    }
    $types = '';
    $vals = [];
    foreach ($cols as $c) {
        $v = $assignments[$c];
        if (is_int($v)) {
            $types .= 'i';
        } elseif (is_float($v)) {
            $types .= 'd';
        } else {
            $types .= 's';
            $v = $v === null ? null : (string) $v;
        }
        $vals[] = $v;
    }
    $stmt->bind_param($types, ...$vals);
    $stmt->execute();
    $stmt->close();
    return 'insert';
}

// ---------------------------------------------------------------------------
// Project copilot_conditions -> lists.type='medical_problem'
// ---------------------------------------------------------------------------

$conditionsSql = 'SELECT id, patient_id, icd10_code, condition_text, onset_date,
                         clinical_status, verification_status
                  FROM copilot_conditions
                  WHERE (verification_status IS NULL OR verification_status <> ?)';
$bind = ['entered-in-error'];
$types = 's';
if ($onlyPid !== null) {
    $conditionsSql .= ' AND patient_id = ?';
    $bind[] = $onlyPid;
    $types .= 'i';
}
$stmt = $mysqli->prepare($conditionsSql);
$stmt->bind_param($types, ...$bind);
$stmt->execute();
$res = $stmt->get_result();
$conditions = [];
while ($row = $res->fetch_assoc()) {
    $conditions[] = $row;
}
$stmt->close();

$cInsert = 0;
$cSkip = 0;
foreach ($conditions as $row) {
    $tag = tagOf('copilot_conditions', (string) $row['id']);
    $title = (string) ($row['condition_text'] ?? '');
    if ($title === '') {
        $title = (string) ($row['icd10_code'] ?? 'Unknown');
    }
    $diagnosis = !empty($row['icd10_code']) ? 'ICD10:' . $row['icd10_code'] : '';
    $begdate = null;
    $onsetRaw = $row['onset_date'] ?? null;
    if (is_string($onsetRaw) && preg_match('/^\d{4}-\d{2}-\d{2}/', $onsetRaw)) {
        $begdate = substr($onsetRaw, 0, 10) . ' 00:00:00';
    }

    $assignments = [
        'type'      => 'medical_problem',
        'title'     => $title,
        'pid'       => (int) $row['patient_id'],
        'activity'  => 1,
        'diagnosis' => $diagnosis,
        'begdate'   => $begdate,
        'comments'  => 'Imported from Clinical Co-Pilot',
    ];
    $action = upsertList($mysqli, $tag, $assignments, $dryRun);
    if ($action === 'insert' || $action === 'plan') {
        $cInsert++;
    } else {
        $cSkip++;
    }
}

// ---------------------------------------------------------------------------
// Project copilot_observations
// ---------------------------------------------------------------------------

// Allergy / medication LOINC sets. Today the agent-api writer only emits
// lab observations (CBC, etc.) but the projector is forward-compatible.
$ALLERGY_LOINCS = [
    '48765-2', // Allergies and adverse reactions Document
    '52473-6', // Allergies, adverse reactions, alerts
];
$MED_LOINCS = [
    '10160-0', // History of Medication use Narrative
    '57828-6', // Prescription list
];

$obsSql = 'SELECT id, patient_id, loinc_code, loinc_display, value_string,
                  effective_date
           FROM copilot_observations';
$obsBind = [];
$obsTypes = '';
if ($onlyPid !== null) {
    $obsSql .= ' WHERE patient_id = ?';
    $obsBind[] = $onlyPid;
    $obsTypes .= 'i';
}
$stmt = $mysqli->prepare($obsSql);
if ($obsBind !== []) {
    $stmt->bind_param($obsTypes, ...$obsBind);
}
$stmt->execute();
$res = $stmt->get_result();
$observations = [];
while ($row = $res->fetch_assoc()) {
    $observations[] = $row;
}
$stmt->close();

$aInsert = 0;
$aSkip = 0;
$mInsert = 0;
$mSkip = 0;
$skippedLab = 0;
foreach ($observations as $row) {
    $loinc = (string) ($row['loinc_code'] ?? '');
    $display = (string) ($row['loinc_display'] ?? '');
    $value = (string) ($row['value_string'] ?? '');
    if ($loinc === '') {
        $skippedLab++;
        continue;
    }

    if (in_array($loinc, $ALLERGY_LOINCS, true)) {
        $tag = tagOf('copilot_observations', (string) $row['id']);
        $title = $value !== '' ? $value : ($display !== '' ? $display : 'Allergy');
        $assignments = [
            'type'     => 'allergy',
            'title'    => $title,
            'pid'      => (int) $row['patient_id'],
            'activity' => 1,
            'comments' => 'Imported from Clinical Co-Pilot (LOINC ' . $loinc . ')',
        ];
        $action = upsertList($mysqli, $tag, $assignments, $dryRun);
        if ($action === 'insert' || $action === 'plan') {
            $aInsert++;
        } else {
            $aSkip++;
        }
        continue;
    }

    if (in_array($loinc, $MED_LOINCS, true)) {
        $tag = tagOf('copilot_observations', (string) $row['id']);
        $title = $value !== '' ? $value : ($display !== '' ? $display : 'Medication');
        $assignments = [
            'type'     => 'medication',
            'title'    => $title,
            'pid'      => (int) $row['patient_id'],
            'activity' => 1,
            'comments' => 'Imported from Clinical Co-Pilot (LOINC ' . $loinc . ')',
        ];
        $action = upsertList($mysqli, $tag, $assignments, $dryRun);
        if ($action === 'insert' || $action === 'plan') {
            $mInsert++;
        } else {
            $mSkip++;
        }
        continue;
    }

    // Lab / vital — out of scope for the chart sidebar cards.
    $skippedLab++;
}

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

fwrite(STDOUT, sprintf(
    "[project] medical_problem: %s=%d skip=%d  | allergy: %s=%d skip=%d  | medication: %s=%d skip=%d  | obs_unmapped(lab/etc)=%d\n",
    $dryRun ? 'plan' : 'insert',
    $cInsert,
    $cSkip,
    $dryRun ? 'plan' : 'insert',
    $aInsert,
    $aSkip,
    $dryRun ? 'plan' : 'insert',
    $mInsert,
    $mSkip,
    $skippedLab
));

if ($onlyPid !== null) {
    $stmt = $mysqli->prepare(
        "SELECT type, COUNT(*) c FROM lists WHERE pid = ? GROUP BY type"
    );
    $stmt->bind_param('i', $onlyPid);
    $stmt->execute();
    $res = $stmt->get_result();
    $counts = [];
    while ($r = $res->fetch_assoc()) {
        $counts[(string) $r['type']] = (int) $r['c'];
    }
    $stmt->close();
    fwrite(STDOUT, sprintf(
        "[project] post-projection lists counts for pid=%d: %s\n",
        $onlyPid,
        json_encode($counts) ?: '{}'
    ));
}

$mysqli->close();
exit(0);
