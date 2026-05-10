FROM openemr/openemr:latest

# Inject proxy trust config before Apache starts.
RUN printf '%s\n' \
    'SetEnvIf X-Forwarded-Proto "https" HTTPS=on' \
    'SetEnvIf X-Forwarded-Proto "https" REQUEST_SCHEME=https' \
    'Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains" env=HTTPS' \
    > /etc/apache2/conf.d/railway-proxy.conf

# Reverse-proxy /dashboard/* to the patient-dashboard Railway service so
# the embedded Next.js app appears at the same origin as OpenEMR. This
# eliminates the third-party-cookie problem that blocks iframe OAuth
# callbacks (Privacy Sandbox / CHIPS / Safari ITP). The dashboard's
# next.config.ts has basePath:'/dashboard' so its internal links + asset
# URLs match this proxy path. Falls through silently when the dashboard
# service isn't reachable (Apache returns 502; rest of OpenEMR keeps
# working since this directive is path-scoped).
RUN { \
      echo 'ProxyRequests Off'; \
      echo 'ProxyPreserveHost On'; \
      echo 'ProxyPass /dashboard http://patient-dashboard.railway.internal:3000/dashboard'; \
      echo 'ProxyPassReverse /dashboard http://patient-dashboard.railway.internal:3000/dashboard'; \
      echo 'ProxyTimeout 60'; \
    } > /etc/apache2/conf.d/dashboard-proxy.conf

# Pre-populate sqlconf.php so the entrypoint skips auto_configure.php
# and goes directly to starting Apache. DB was initialized separately.
RUN mkdir -p /var/www/localhost/htdocs/openemr/sites/default && \
    printf '<?php\n$host="mysql.railway.internal";\n$port="3306";\n$login="openemr";\n$pass="openemr123";\n$dbase="openemr";\n$sqlconf=[];\nglobal $sqlconf;\n$sqlconf["host"]=$host;\n$sqlconf["port"]=$port;\n$sqlconf["login"]=$login;\n$sqlconf["pass"]=$pass;\n$sqlconf["dbase"]=$dbase;\n$config=1;\n' \
    > /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php

# Layer our local source tree on top of the base image so any local fixes
# (e.g. SessionWrapperFactory::getActiveSession() added in upstream commit
# 623c6b8b5 but not yet in the published image) are present at runtime.
# Only directories that are pure PHP/JS/templates and do not depend on
# composer artifacts beyond what the base image already installed.
#
# We deliberately do NOT copy vendor/, public/assets/, or sites/ — those are
# either built/installed by the base image or contain runtime config we are
# already managing above (sqlconf.php).
COPY src/         /var/www/localhost/htdocs/openemr/src/
COPY library/     /var/www/localhost/htdocs/openemr/library/
COPY interface/   /var/www/localhost/htdocs/openemr/interface/
COPY templates/   /var/www/localhost/htdocs/openemr/templates/
COPY controllers/ /var/www/localhost/htdocs/openemr/controllers/
COPY scripts/     /var/www/localhost/htdocs/openemr/scripts/
# composer.json was updated upstream (PR #11412, 2026-04-02) to register
# library/global_functions.inc.php in `autoload.files`. The base image's
# vendor/composer/autoload_files.php predates that change, so functions
# moved out of globals.php (e.g. getLayoutRes used by interface/new/
# new_comprehensive.php:51) end up undefined at runtime. Pull our
# composer.json + lock and regen the autoloader so the registration takes
# effect. composer is present in the base image.
COPY composer.json composer.lock /var/www/localhost/htdocs/openemr/

# Docker COPY creates files owned by root:root. The openemr/openemr base image
# owns all files as apache:root, and its entrypoint only runs chmod (not chown).
# So without this fix, files end up root:root 400 — unreadable by Apache
# (which runs as user 'apache'). Match the base image ownership so chmod 400
# leaves them readable by Apache.
RUN chown -R apache:root \
    /var/www/localhost/htdocs/openemr/src/ \
    /var/www/localhost/htdocs/openemr/library/ \
    /var/www/localhost/htdocs/openemr/interface/ \
    /var/www/localhost/htdocs/openemr/templates/ \
    /var/www/localhost/htdocs/openemr/controllers/ \
    /var/www/localhost/htdocs/openemr/scripts/ \
    /var/www/localhost/htdocs/openemr/composer.json \
    /var/www/localhost/htdocs/openemr/composer.lock

# Regenerate the composer autoloader so library/global_functions.inc.php
# (and any other autoload.files entries the base image's autoloader missed)
# are require'd at runtime. --no-dev keeps the prod-only set; --optimize
# emits a classmap so prod has no filesystem walks per request.
RUN cd /var/www/localhost/htdocs/openemr && composer dump-autoload --no-dev --optimize 2>&1 || \
    (echo "composer dump-autoload failed; trying with --no-scripts" && \
     cd /var/www/localhost/htdocs/openemr && composer dump-autoload --no-dev --optimize --no-scripts)
