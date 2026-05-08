-- =============================================================================
-- oe-module-patient-dashboard-port — install script
-- =============================================================================
--
-- Registers the module in OpenEMR's module registry without going through the
-- admin Manage Modules UI. Mirrors `oe-module-clinical-copilot`'s footprint
-- exactly: one row in `modules`, one row in `module_acl_sections`, no rows in
-- any other module table (Co-Pilot leaves those empty too).
--
-- Use cases:
--   - Local dev: when the OpenEMR Manage Modules UI is brittle.
--   - Railway / production: as the deploy-time module-install step.
--
-- IMPORTANT: this script assumes no existing row with
-- `mod_directory = 'oe-module-patient-dashboard-port'`. If you need to
-- re-run it (e.g. after a wipe), DELETE the previous rows first:
--
--   DELETE FROM module_acl_sections WHERE module_id =
--       (SELECT mod_id FROM modules WHERE mod_directory = 'oe-module-patient-dashboard-port');
--   DELETE FROM modules WHERE mod_directory = 'oe-module-patient-dashboard-port';
--
-- =============================================================================

-- Block 1: register the module itself.
-- Mirrors Co-Pilot's row shape:
--   mod_active=1            module is enabled
--   sql_run=1               module's optional sql_install ran (none for us)
--   mod_relative_link       points at index.php under the module directory
-- The auto-incremented mod_id is captured for the next block via
-- LAST_INSERT_ID().
INSERT INTO modules (
    mod_name, mod_directory, mod_parent, mod_type, mod_active,
    mod_ui_name, mod_relative_link, mod_ui_order, mod_ui_active,
    mod_description, mod_nick_name, mod_enc_menu, permissions_item_table,
    directory, date, sql_run, type, sql_version, acl_version
) VALUES (
    'oe-module-patient-dashboard-port',
    'oe-module-patient-dashboard-port',
    '', '', 1,
    'Oe-module-patient-dashboard-port',
    'oe-module-patient-dashboard-port/index.php',
    0, 0,
    '', '', '',
    NULL,
    '', NOW(), 1, 0, '0', ''
);
SET @new_mod_id := LAST_INSERT_ID();

-- Block 2: register the matching ACL section.
-- Mirrors Co-Pilot's `module_acl_sections` row where section_id and module_id
-- are both equal to the module's mod_id. parent_section=0 means top-level.
INSERT INTO module_acl_sections
    (section_id, section_name, parent_section, section_identifier, module_id)
VALUES
    (@new_mod_id, 'oe-module-patient-dashboard-port', 0, 'oe-module-patient-dashboard-port', @new_mod_id);

-- Verification — should print one matching row in each table.
SELECT mod_id, mod_directory, mod_active, sql_run
  FROM modules
 WHERE mod_directory = 'oe-module-patient-dashboard-port';

SELECT section_id, section_name, parent_section, section_identifier, module_id
  FROM module_acl_sections
 WHERE module_id = @new_mod_id;
