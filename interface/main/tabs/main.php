<?php

/**
 * main.php
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Kevin Yeh <kevin.y@integralemr.com>
 * @author    Brady Miller <brady.g.miller@gmail.com>
 * @author    Ranganath Pathak <pathak@scrs1.org>
 * @author    Jerry Padgett <sjpadgett@gmail.com>
 * @author    Stephen Nielson <snielson@discoverandchange.com>
 * @author    Michael A. Smith <michael@opencoreemr.com>
 * @copyright Copyright (c) 2016 Kevin Yeh <kevin.y@integralemr.com>
 * @copyright Copyright (c) 2016-2019 Brady Miller <brady.g.miller@gmail.com>
 * @copyright Copyright (c) 2019 Ranganath Pathak <pathak@scrs1.org>
 * @copyright Copyright (c) 2024 Care Management Solutions, Inc. <stephen.waite@cmsvt.com>
 * @copyright Copyright (c) 2026 OpenCoreEMR Inc <https://opencoreemr.com/>
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

$sessionAllowWrite = true;
require_once(__DIR__ . '/../../globals.php');
require_once \OpenEMR\Core\OEGlobalsBag::getInstance()->getSrcDir() . '/ESign/Api.php';

use ESign\Api;
use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionUtil;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Twig\TwigContainer;
use OpenEMR\Core\Header;
use OpenEMR\Core\OEEnvBag;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Events\Main\Tabs\RenderEvent;
use OpenEMR\Menu\MainMenuRole;
use OpenEMR\Services\LogoService;
use OpenEMR\Services\ProductRegistrationService;
use OpenEMR\Services\VersionService;
use OpenEMR\Telemetry\TelemetryService;
use Symfony\Component\Filesystem\Path;

const ENV_DISABLE_TELEMETRY = 'OPENEMR_DISABLE_TELEMETRY';

$session = SessionWrapperFactory::getInstance()->getActiveSession();

$logoService = new LogoService();
$menuLogo = $logoService->getLogo('core/menu/primary/');
$versionService = new VersionService();
$softwareVersion = text((string) $versionService->getSoftwareVersion());
// Registration status and options.
$productRegistration = new ProductRegistrationService();
$product_row = $productRegistration->getProductDialogStatus();
$allowRegisterDialog = $product_row['allowRegisterDialog'] ?? false;
$allowTelemetry = $product_row['allowTelemetry'] ?? null; // for dialog
$allowEmail = $product_row['allowEmail'] ?? null; // for dialog

// Check if telemetry is disabled via environment variable
$disableTelemetry = OEEnvBag::getInstance()->getBoolean(ENV_DISABLE_TELEMETRY);
// Check if background service piggybacking is disabled via environment variable
$noBackgroundTasks = OEEnvBag::getInstance()->getBoolean('OPENEMR__NO_BACKGROUND_TASKS');
if ($disableTelemetry) {
    $allowRegisterDialog = false;
    $allowTelemetry = false;
}

// If running unit tests, then disable the registration dialog
if ($session->get('testing_mode', false)) {
    $allowRegisterDialog = false;
}
// If the user is not a super admin, then disable the registration dialog
if (!AclMain::aclCheckCore('admin', 'super')) {
    $allowRegisterDialog = false;
}

// Ensure token_main matches so this script can not be run by itself
//  If tokens do not match, then destroy the session and go back to log in screen
$token_main_php = $session->get('token_main_php');
if (
    $token_main_php === null ||
    (!array_key_exists('token_main', $_GET) || $_GET['token_main'] === '') ||
    $_GET['token_main'] !== $token_main_php
) {
// Below functions are from auth.inc, which is included in globals.php
    authCloseSession();
    authLoginScreen(false);
}
// this will not allow copy/paste of the link to this main.php page or a refresh of this main.php page
//  (default behavior, however, this behavior can be turned off in the prevent_browser_refresh global)
if (OEGlobalsBag::getInstance()->get('prevent_browser_refresh') > 1) {
    SessionUtil::unsetSession('token_main_php');
}

$esignApi = new Api();
$twig = (new TwigContainer(null, OEGlobalsBag::getInstance()->getKernel()))->getTwig();

?>
<!DOCTYPE html>
<html>

<head>
    <title><?php echo text($openemr_name); ?></title>

    <script>
        // This is to prevent users from losing data by refreshing or backing out of OpenEMR.
        //  (default behavior, however, this behavior can be turned off in the prevent_browser_refresh global)
        <?php if (OEGlobalsBag::getInstance()->get('prevent_browser_refresh') > 0) { ?>
        window.addEventListener('beforeunload', (event) => {
            if (!timed_out) {
                event.returnValue = <?php echo xlj('Recommend not leaving or refreshing or you may lose data.'); ?>;
            }
        });
        <?php } ?>

        <?php require(OEGlobalsBag::getInstance()->getSrcDir() . "/restoreSession.php"); ?>

        // Since this should be the parent window, this is to prevent calls to the
        // window that opened this window. For example when a new window is opened
        // from the Patient Flow Board or the Patient Finder.
        window.opener = null;
        window.name = "main";

        // This flag indicates if another window or frame is trying to reload the login
        // page to this top-level window.  It is set by javascript returned by auth.inc.php
        // and is checked by handlers of beforeunload events.
        var timed_out = false;
        // some globals to access using top.variable
        // note that 'let' or 'const' does not allow global scope here.
        // only use var
        var isPortalEnabled = "<?php echo OEGlobalsBag::getInstance()->getBoolean('portal_onsite_two_enable') ?>";
        // Set the csrf_token_js token that is used in the below js/tabs_view_model.js script
        var csrf_token_js = <?php echo js_escape(CsrfUtils::collectCsrfToken($session)); ?>;
        // Separate CSRF token for calls to the REST/LocalApi stack (sent as the APICSRFTOKEN header).
        var api_csrf_token_js = <?php echo js_escape(CsrfUtils::collectCsrfToken($session, 'api')); ?>;
        <?php
        $sessionSiteId = $session->get('site_id');
        $sessionSiteIdString = is_string($sessionSiteId) ? $sessionSiteId : '';
        ?>
        var site_id_js = <?php echo js_escape($sessionSiteIdString); ?>;
        var userDebug = <?php echo js_escape(OEGlobalsBag::getInstance()->get('user_debug')); ?>;
        var webroot_url = <?php echo js_escape($web_root); ?>;
        var jsLanguageDirection = <?php echo js_escape($session->get('language_direction')); ?> ||
        'ltr';
        var jsGlobals = {};
        // used in tabs_view_model.js.
        jsGlobals.enable_group_therapy = <?php echo js_escape((int) OEGlobalsBag::getInstance()->getBoolean('enable_group_therapy')); ?>;
        jsGlobals.languageDirection = jsLanguageDirection;
        jsGlobals.date_display_format = <?php echo js_escape(OEGlobalsBag::getInstance()->get('date_display_format')); ?>;
        jsGlobals.time_display_format = <?php echo js_escape(OEGlobalsBag::getInstance()->get('time_display_format')); ?>;
        jsGlobals.timezone = <?php echo js_escape(OEGlobalsBag::getInstance()->get('gbl_time_zone') ?? ''); ?>;
        jsGlobals.assetVersion = <?php echo js_escape(OEGlobalsBag::getInstance()->get('v_js_includes')); ?>;
        var WindowTitleAddPatient = <?php echo(OEGlobalsBag::getInstance()->getBoolean('window_title_add_patient_name') ? 'true' : 'false'); ?>;
        var WindowTitleBase = <?php echo js_escape($openemr_name); ?>;
        const isSms = "<?php echo !empty(OEGlobalsBag::getInstance()->get('oefax_enable_sms') ?? null); ?>";
        const isFax = "<?php echo !empty(OEGlobalsBag::getInstance()->get('oefax_enable_fax')) ?? null?>";
        const isServicesOther = (isSms || isFax);
        var telemetryEnabled = <?php echo js_escape((new TelemetryService())->isTelemetryEnabled()); ?>;
        var noBackgroundTasks = <?php echo $noBackgroundTasks ? 'true' : 'false'; ?>;

        /**
         * Async function to get session value from the server
         * Usage Example
         * let authUser;
         * let sessionPid = await top.getSessionValue('pid');
         * // If using then() method a promise is returned instead of the value.
         * await top.getSessionValue('authUser').then(function (auth) {
         *    authUser = auth;
         *    console.log('authUser', authUser);
         * });
         * console.log('session pid', sessionPid);
         * console.log('auth User', authUser);
         */
        async function getSessionValue(key) {
            restoreSession();
            let csrf_token_js = <?php echo js_escape(CsrfUtils::collectCsrfToken($session)); ?>;
            const config = {
                url: `${webroot_url}/library/ajax/set_pt.php?csrf_token_form=${csrf_token_js}`,
                method: 'POST',
                data: {
                    mode: 'session_key',
                    key: key
                }
            };
            try {
                const response = await $.ajax(config);
                restoreSession();
                return response;
            } catch (error) {
                throw error;
            }
        }

        function goRepeaterServices() {
            // Ensure send the skip_timeout_reset parameter to not count this as a manual entry in the
            // timing out mechanism in OpenEMR.

            // Send the skip_timeout_reset parameter to not count this as a manual entry in the
            // timing out mechanism in OpenEMR. Notify App for various portal and reminder alerts.
            // Combined portal and reminders ajax to fetch sjp 06-07-2020.
            // Incorporated timeout mechanism in 2021
            restoreSession();
            let request = new FormData;
            request.append("skip_timeout_reset", "1");
            request.append("isPortal", isPortalEnabled);
            request.append("isServicesOther", isServicesOther);
            request.append("isSms", isSms);
            request.append("isFax", isFax);
            request.append("csrf_token_form", csrf_token_js);
            fetch(webroot_url + "/library/ajax/dated_reminders_counter.php", {
                method: 'POST',
                credentials: 'same-origin',
                body: request
            }).then((response) => {
                if (response.status !== 200) {
                    console.log('Reminders start failed. Status Code: ' + response.status);
                    return;
                }
                return response.json();
            }).then((data) => {
                if (data.timeoutMessage && (data.timeoutMessage == 'timeout')) {
                    // timeout has happened, so logout
                    timeoutLogout();
                }
                if (isPortalEnabled) {
                    let mail = data.mailCnt;
                    let chats = data.chatCnt;
                    let audits = data.auditCnt;
                    let payments = data.paymentCnt;
                    let total = data.total;
                    let enable = ((1 * mail) + (1 * audits)); // payments are among audits.
                    // Send portal counts to notification button model
                    // Will turn off button display if no notification!
                    app_view_model.application_data.user().portal(enable);
                    if (enable > 0) {
                        app_view_model.application_data.user().portalAlerts(total);
                        app_view_model.application_data.user().portalAudits(audits);
                        app_view_model.application_data.user().portalMail(mail);
                        app_view_model.application_data.user().portalChats(chats);
                        app_view_model.application_data.user().portalPayments(payments);
                    }
                }
                if (isServicesOther) {
                    let sms = data.smsCnt;
                    let fax = data.faxCnt;
                    let total = data.serviceTotal;
                    let enable = ((1 * sms) + (1 * fax));
                    // Will turn off button display if no notification!
                    app_view_model.application_data.user().servicesOther(enable);
                    if (enable > 0) {
                        app_view_model.application_data.user().serviceAlerts(total);
                        app_view_model.application_data.user().smsAlerts(sms);
                        app_view_model.application_data.user().faxAlerts(fax);
                    }
                }
                // Always send reminder count text to model
                app_view_model.application_data.user().messages(data.reminderText);
            }).catch(function (error) {
                console.log('Request failed', error);
            });

            // run background-services
            // delay 10 seconds to prevent both utility trigger at close to same time.
            // Both call globals so that is my concern.
            if (!noBackgroundTasks) {
                setTimeout(function () {
                    restoreSession();
                    // Call the REST "run all due" endpoint via LocalApi (APICSRFTOKEN header).
                    // The REST stack does not touch SessionTracker, so no skip_timeout_reset
                    // equivalent is needed to avoid resetting the session expiration timer.
                    fetch(webroot_url + "/apis/" + site_id_js + "/api/background_service/$run", {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {
                            'APICSRFTOKEN': api_csrf_token_js
                        }
                    }).then((response) => {
                        if (response.status !== 200) {
                            console.log('Background Service start failed. Status Code: ' + response.status);
                        }
                    }).catch(function (error) {
                        console.log('HTML Background Service start Request failed: ', error);
                    });
                }, 10000);
            }

            // auto run this function every 60 seconds
            var repeater = setTimeout("goRepeaterServices()", 60000);
        }

        function isEncounterLocked(encounterId) {
            <?php if ($esignApi->lockEncounters()) { ?>
            // If encounter locking is enabled, make a synchronous call (async=false) to check the
            // DB to see if the encounter is locked.
            // Call restore session, just in case
            // @TODO next clean up pass, turn into await promise then modify tabs_view_model.js L-309
            restoreSession();
            let url = webroot_url + "/interface/esign/index.php?module=encounter&method=esign_is_encounter_locked";
            $.ajax({
                type: 'POST',
                url: url,
                data: {
                    encounterId: encounterId
                },
                success: function (data) {
                    encounter_locked = data;
                },
                dataType: 'json',
                async: false
            });
            return encounter_locked;
            <?php } else { ?>
            // If encounter locking isn't enabled then always return false
            return false;
            <?php } ?>
        }
    </script>

    <?php Header::setupHeader(['knockout', 'tabs-theme', 'i18next', 'hotkeys', 'i18formatting']); ?>
    <script>
        // set up global translations for js
        function setupI18n(lang_id) {
            restoreSession();
            return fetch(<?php echo js_escape(OEGlobalsBag::getInstance()->getWebRoot()) ?> +"/library/ajax/i18n_generator.php?lang_id=" + encodeURIComponent(lang_id) + "&csrf_token_form=" + encodeURIComponent(csrf_token_js), {
                credentials: 'same-origin',
                method: 'GET'
            }).then((response) => {
                if (response.status !== 200) {
                    console.log('I18n setup failed. Status Code: ' + response.status);
                    return [];
                }
                return response.json();
            })
        }

        setupI18n(<?php echo js_escape($session->get('language_choice')); ?>).then(translationsJson => {
            i18next.init({
                lng: 'selected',
                debug: false,
                nsSeparator: false,
                keySeparator: false,
                resources: {
                    selected: {
                        translation: translationsJson
                    }
                }
            });
        }).catch(error => {
            console.log(error.message);
        });

        /**
         * Assign and persist documents to portal patients
         * @var int patientId pid
         */
        function assignPatientDocuments(patientId) {
            let url = top.webroot_url + '/portal/import_template_ui.php?from_demo_pid=' + encodeURIComponent(patientId);
            dlgopen(url, 'pop-assignments', 'modal-lg', 850, '', '', {
                allowDrag: true,
                allowResize: true,
                sizeHeight: 'full',
            });
        }
    </script>

    <script src="js/custom_bindings.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/user_data_view_model.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/patient_data_view_model.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/therapy_group_data_view_model.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/tabs_view_model.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/application_view_model.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/frame_proxies.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/dialog_utils.js?v=<?php echo $v_js_includes; ?>"></script>
    <script src="js/shortcuts.js?v=<?php echo $v_js_includes; ?>"></script>

    <?php
    // Below code block is to prepare certain elements for deciding what links to show on the menu
    // prepare Ensora eRx globals that are used in creating the menu
    if (OEGlobalsBag::getInstance()->getBoolean('erx_enable')) {
        $newcrop_user_role_sql = sqlQuery("SELECT `newcrop_user_role` FROM `users` WHERE `username` = ?", [$session->get('authUser')]);
        OEGlobalsBag::getInstance()->set('newcrop_user_role', $newcrop_user_role_sql['newcrop_user_role']);
        if (OEGlobalsBag::getInstance()->get('newcrop_user_role') === 'erxadmin') {
            OEGlobalsBag::getInstance()->set('newcrop_user_role_erxadmin', 1);
        }
    }

    // prepare track anything to be used in creating the menu
    $track_anything_sql = sqlQuery("SELECT `state` FROM `registry` WHERE `directory` = 'track_anything'");
    OEGlobalsBag::getInstance()->set('track_anything_state', $track_anything_sql['state'] ?? 0);
    // prepare Issues popup link global that is used in creating the menu
    OEGlobalsBag::getInstance()->set('allow_issue_menu_link', (AclMain::aclCheckCore('encounters', 'notes', '', 'write')
    || AclMain::aclCheckCore('encounters', 'notes_a', '', 'write'))
    && AclMain::aclCheckCore('patients', 'med', '', 'write'));

    // we use twig templates here so modules can customize some of these files
    // at some point we will twigify all of main.php so we can extend it.
    echo $twig->render("interface/main/tabs/tabs_template.html.twig", []);
    echo $twig->render("interface/main/tabs/menu_template.html.twig", []);
    // TODO: patient_data_template.php is a more extensive refactor that could be done in a future feature request but to not jeopardize 7.0.3 release we will hold off.
    ?>
    <?php require_once("templates/patient_data_template.php"); ?>
    <?php
    echo $twig->render("interface/main/tabs/therapy_group_template.html.twig", []);
    echo $twig->render("interface/main/tabs/user_data_template.html.twig", [
        'openemr_name' => OEGlobalsBag::getInstance()->getString('openemr_name')
    ]);
    // Collect the menu then build it
    $menuMain = new MainMenuRole(OEGlobalsBag::getInstance()->getKernel()->getEventDispatcher());
    $menu_restrictions = $menuMain->getMenu();
    echo $twig->render("interface/main/tabs/menu_json.html.twig", ['menu_restrictions' => $menu_restrictions]);
    ?>
    <?php $userQuery = sqlQuery("select * from users where username = ?", [$session->get('authUser')]); ?>

    <script>
        <?php
        if ($session->get('default_open_tabs')) :
            // For now, only the first tab is visible, this could be improved upon by further customizing the list options in a future feature request
            $visible = "true";
            $default_open_tabs = $session->get('default_open_tabs');
            foreach ($default_open_tabs as $i => $tab) :
                $_unsafe_url = preg_replace('/(\?.*)/m', '', Path::canonicalize($fileroot . DIRECTORY_SEPARATOR . $tab['notes']));
                if (realpath($_unsafe_url) === false || !str_starts_with($_unsafe_url, (string) $fileroot)) {
                    unset($default_open_tabs[$i]);
                    $session->set('default_open_tabs', $default_open_tabs);
                    continue;
                }
                $url = json_encode($webroot . "/" . $tab['notes']);
                $target = json_encode($tab['option_id']);
                $label = json_encode(xl("Loading") . " " . $tab['title']);
                $loading = xlj("Loading");
                echo "app_view_model.application_data.tabs.tabsList.push(new tabStatus($label, $url, $target, $loading, true, $visible, false));\n";
                $visible = "false";
            endforeach;
        endif;
        ?>

        app_view_model.application_data.user(new user_data_view_model(<?php echo json_encode($session->get("authUser"))
            . ',' . json_encode($userQuery['fname'])
            . ',' . json_encode($userQuery['lname'])
            . ',' . json_encode($session->get('authProvider')); ?>));
    </script>
    <style>
      html,
      body {
        width: max-content;
        min-height: 100% !important;
        height: 100% !important;
      }
      #userdropdown.dropdown-menu {
        white-space: nowrap;        /* prevents multi-line wrapping */
        min-width: max-content;     /* expands to fit the widest item */
      }
    </style>
</head>

<body class="min-vw-100">
    <?php
    // fire off an event here
    if (OEGlobalsBag::getInstance()->hasKernel()) {
        $dispatcher = OEGlobalsBag::getInstance()->getKernel()->getEventDispatcher();
        $dispatcher->dispatch(new RenderEvent(), RenderEvent::EVENT_BODY_RENDER_PRE);
    }
    ?>
    <!-- Below iframe is to support logout, which needs to be run in an inner iframe to work as intended -->
    <iframe name="logoutinnerframe" id="logoutinnerframe" style="visibility:hidden; position:absolute; left:0; top:0; height:0; width:0; border:none;" src="about:blank"></iframe>
    <?php // mdsupport - app settings
    $disp_mainBox = '';
    $app1 = $session->get('app1');
    if (!empty($app1)) {
        $rs = sqlquery(
            "SELECT title app_url FROM list_options WHERE activity=1 AND list_id=? AND option_id=?",
            ['apps', $app1]
        );
        if ($rs['app_url'] != "main/main_screen.php") {
            echo '<iframe name="app1" src="../../' . attr($rs['app_url']) . '"
            style="position: absolute; left: 0; top: 0; height: 100%; width: 100%; border: none;" />';
            $disp_mainBox = 'style="display: none;"';
        }
    }
    ?>
    <div id="mainBox" <?php echo $disp_mainBox ?>>
        <nav class="navbar navbar-expand-xl navbar-light bg-light py-0">
            <?php if (OEGlobalsBag::getInstance()->getBoolean('display_main_menu_logo')) {
                $bag = OEGlobalsBag::getInstance();
                $logoLinkDefault = 'https://www.open-emr.org/';
                $logoTitleDefault = xl('OpenEMR Website');
                $logoLink = trim($bag->getString('main_menu_logo_link', $logoLinkDefault));
                $logoTitle = trim($bag->getString('main_menu_logo_title', $logoTitleDefault));
                $logoImg = '<img src="' . attr($menuLogo) . '" class="d-inline-block align-middle" height="16" alt="' . xla('Main Menu Logo') . '">';
                if ($logoLink !== '') {
                    echo '<a class="navbar-brand" href="' . attr($logoLink) . '" title="' . attr($logoTitle) . '" rel="noopener" target="_blank">' . $logoImg . '</a>' . "\n";
                } else {
                    echo '<span class="navbar-brand">' . $logoImg . '</span>' . "\n";
                }
            } ?>
            <button class="navbar-toggler mr-auto" type="button" data-toggle="collapse" data-target="#mainMenu" aria-controls="mainMenu" aria-expanded="false" aria-label="Toggle navigation">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="mainMenu" data-bind="template: {name: 'menu-template', data: application_data}"></div>
            <?php if (OEGlobalsBag::getInstance()->get('search_any_patient') != 'none') : ?>
                <form name="frm_search_globals" class="form-inline">
                    <div class="input-group">
                        <input type="text" id="anySearchBox" class="form-control-sm <?php echo $any_search_class ?> form-control" name="anySearchBox" placeholder="<?php echo xla("Search by any demographics") ?>" autocomplete="off">
                        <div class="input-group-append">
                            <button type="button" id="search_globals" class="btn btn-sm btn-secondary <?php echo $search_globals_class ?>" title='<?php echo xla("Search for patient by entering whole or part of any demographics field information"); ?>' data-bind="event: {mousedown: viewPtFinder.bind( $data, '<?php echo xla("The search field cannot be empty. Please enter a search term") ?>', '<?php echo attr($search_any_type); ?>')}">
                                <i class="fa fa-search">&nbsp;</i></button>
                        </div>
                    </div>
                </form>
            <?php endif; ?>
            <!--Below is the user data section that contains the user information and the attendant data-->
            <span id="userData" data-bind="template: {name: 'user-data-template', data: application_data}"></span>
            <?php
            // fire off a nav event
            $dispatcher->dispatch(new RenderEvent(), RenderEvent::EVENT_BODY_RENDER_NAV);
            ?>
        </nav>
        <div id="attendantData" class="body_title acck" data-bind="template: {name: app_view_model.attendant_template_type, data: application_data}"></div>
        <div class="body_title pt-1" id="tabs_div" data-bind="template: {name: 'tabs-controls', data: application_data}"></div>
        <div class="mainFrames d-flex flex-row" id="mainFrames_div">
            <div id="framesDisplay" data-bind="template: {name: 'tabs-frames', data: application_data}"></div>
        </div>
        <?php echo $twig->render("product_registration/product_registration_modal.html.twig", [
            'webroot' => $webroot,
            'allowEmail' => $allowEmail ?? false,
            'allowTelemetry' => $allowTelemetry ?? false]); ?>
    </div>
    <div id="versionFooter" class="text-muted" style="position:fixed; bottom:4px; inset-inline-end:8px; font-size:11px; pointer-events:none; z-index:4;">
        <?php echo $softwareVersion; ?>
    </div>
    <script>
        ko.applyBindings(app_view_model);

        $(function () {
            $('.dropdown-toggle').dropdown();
            $('#patient_caret').click(function () {
                $('#attendantData').slideToggle();
                $('#patient_caret').toggleClass('fa-caret-down').toggleClass('fa-caret-up');
            });
            if ($('body').css('direction') == "rtl") {
                $('.dropdown-menu-right').each(function () {
                    $(this).removeClass('dropdown-menu-right');
                });
            }
        });
        $(function () {
            $('#logo_menu').focus();
        });
        $('#anySearchBox').keypress(function (event) {
            if (event.which === 13 || event.keyCode === 13) {
                event.preventDefault();
                $('#search_globals').mousedown();
            }
        });
        document.addEventListener('touchstart', {}); //specifically added for iOS devices, especially in iframes
        $(function () {
            goRepeaterServices();
        });
    </script>
    <?php

    // fire off an event here
    $dispatcher->dispatch(new RenderEvent(), RenderEvent::EVENT_BODY_RENDER_POST);

    if ($allowRegisterDialog !== false) { // disable if running unit tests.
        // Include the product registration js, telemetry and usage data reporting dialog
        echo $twig->render("product_registration/product_reg.js.twig", ['webroot' => $webroot]);
    }

    // ── Clinical Co-Pilot prefetch (warm agent caches before the user clicks the tab) ──
    // Skip silently when the module isn't installed or isn't configured. The
    // session_id formula MUST match interface/modules/custom_modules/oe-module-clinical-copilot/index.php
    // so the warmed cache lands in the same checkpointer session the iframe will open.
    $copilotModuleDir = $GLOBALS['fileroot'] . '/interface/modules/custom_modules/oe-module-clinical-copilot';
    if (is_dir($copilotModuleDir)) {
        $copilotProviderId = (int) ($_SESSION['authUserID'] ?? ($_SESSION['OpenEMR']['authUserID'] ?? 0));
        $copilotEnvUrl     = getenv('COPILOT_AGENT_API_URL');
        $copilotGlobalUrl  = $GLOBALS['copilot_agent_api_url'] ?? null;
        $copilotHttpHost   = (string) ($_SERVER['HTTP_HOST'] ?? '');
        $copilotIsLocal    = $copilotHttpHost === ''
            || str_contains($copilotHttpHost, 'localhost')
            || str_contains($copilotHttpHost, '127.0.0.1');
        $copilotDevMode    = getenv('COPILOT_DEV_MODE') === '1';
        $copilotAgentUrl   = '';
        if (is_string($copilotEnvUrl) && $copilotEnvUrl !== '') {
            $copilotAgentUrl = $copilotEnvUrl;
        } elseif (is_string($copilotGlobalUrl) && $copilotGlobalUrl !== '') {
            $copilotAgentUrl = $copilotGlobalUrl;
        } elseif ($copilotIsLocal || $copilotDevMode) {
            $copilotAgentUrl = 'http://localhost:8400';
        }
        $copilotPatientIds = [];
        if ($copilotProviderId > 0 && $copilotAgentUrl !== '') {
            // Same query as the iframe's auto-populate path in index.php — open
            // encounters assigned to this provider in the last 7 days.
            $copilotEncRes = sqlStatement(
                "SELECT DISTINCT pid
                   FROM form_encounter
                  WHERE provider_id = ?
                    AND (date_end IS NULL OR date_end = '0000-00-00 00:00:00')
                    AND date >= DATE_SUB(NOW(), INTERVAL 7 DAY)
                  ORDER BY date ASC",
                [$copilotProviderId]
            );
            while ($copilotRow = sqlFetchArray($copilotEncRes)) {
                $copilotPatientIds[] = (string) (int) $copilotRow['pid'];
            }
        }
        if ($copilotProviderId > 0 && $copilotAgentUrl !== '' && $copilotPatientIds !== []) {
            // Each fresh OpenEMR login starts a brand-new chat. We mint a
            // nonce stored in $_SESSION so main.php and the iframe's
            // index.php (same PHP session) share the SAME copilot session
            // id within one login, but a NEW one after logout/login.
            // Without the nonce, the per-day formula meant yesterday's
            // chat stuck around all morning; users had to manually press
            // Refresh on each cached bubble.
            if (empty($_SESSION['copilot_session_nonce'])) {
                $_SESSION['copilot_session_nonce'] = bin2hex(random_bytes(8));
            }
            $copilotSessionId = 'copilot-' . hash('sha256', $copilotProviderId . '|' . date('Y-m-d') . '|' . $_SESSION['copilot_session_nonce']);
            // Mint a short-lived HS256 JWT for the prefetch POST. Same secret
            // (COPILOT_JWT_SECRET) the agent-api Python middleware verifies.
            // Returns null when the secret is missing/short — omit the key in
            // that case so the client doesn't send "Bearer " with no payload.
            require_once $copilotModuleDir . '/src/JwtMinter.php';
            $copilotJwt = \OpenEMR\Modules\ClinicalCopilot\JwtMinter::mint($copilotProviderId, $copilotSessionId);
            // 5-minute bucket so a page reload re-fires the prefetch after 5 min
            // (matches census_cache_ttl). Per-day debounce was too sticky — once
            // the script ran on the first reload of the morning, every subsequent
            // reload skipped the prefetch and the user kept seeing stale data.
            $copilotPrefetchKey = 'copilot_prefetched_' . session_id() . '_' . date('Y-m-d-H') . '-' . str_pad((string) (intdiv((int) date('i'), 5) * 5), 2, '0', STR_PAD_LEFT);
            $copilotConfig = [
                'agentApiUrl' => $copilotAgentUrl,
                'sessionId'   => $copilotSessionId,
                'providerId'  => (string) $copilotProviderId,
                'patientIds'  => $copilotPatientIds,
                'flagKey'     => $copilotPrefetchKey,
            ];
            if (is_string($copilotJwt) && $copilotJwt !== '') {
                $copilotConfig['jwt'] = $copilotJwt;
            }
            $copilotConfigJson = json_encode(
                $copilotConfig,
                JSON_HEX_TAG | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_HEX_AMP | JSON_THROW_ON_ERROR
            );
            ?>
    <script>
    (function () {
        try {
            var c = <?php echo $copilotConfigJson; ?>;
            // Window-scoped guard to prevent double-fire when this script
            // executes more than once in the same page context (observed
            // 2× POST /agent/prefetch within 200ms). sessionStorage gates
            // across page reloads but not in-page re-execution.
            if (window.__copilotPrefetchFired) { return; }
            window.__copilotPrefetchFired = true;
            function fire() {
                try {
                    if (sessionStorage.getItem(c.flagKey) === '1') { return; }
                    sessionStorage.setItem(c.flagKey, '1');
                } catch (e) { /* private mode — fire once anyway */ }
                try {
                    // force_refresh ensures the cached census + bundles +
                    // briefings + medication-safety reports reflect the actual
                    // current FHIR state, not whatever was cached from the
                    // previous shift. Cost: one-time warm of ~10 patients
                    // takes ~20-30s of background Anthropic + FHIR work; runs
                    // invisibly while the user reads emails before clicking
                    // Co-Pilot. Server-side gated by
                    // PREFETCH_FORCE_REFRESH_ON_LOGIN — when False (default
                    // for prod), the backend silently downgrades to a normal
                    // EXISTS-checked warm.
                    fetch(c.agentApiUrl + '/agent/prefetch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            session_id: c.sessionId,
                            provider_id: c.providerId,
                            patient_ids: c.patientIds,
                            force_refresh: true
                        }),
                        keepalive: true
                    }).catch(function () {});
                } catch (e) { /* silent */ }
            }
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', fire);
            } else {
                fire();
            }
        } catch (e) { /* never break page render */ }
    })();
    </script>
            <?php
        }
    }

    ?>
</body>

</html>
