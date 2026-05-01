FROM openemr/openemr:latest

# Inject proxy trust config before Apache starts.
RUN printf '%s\n' \
    'SetEnvIf X-Forwarded-Proto "https" HTTPS=on' \
    'SetEnvIf X-Forwarded-Proto "https" REQUEST_SCHEME=https' \
    'Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains" env=HTTPS' \
    > /etc/apache2/conf.d/railway-proxy.conf

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
COPY src/        /var/www/localhost/htdocs/openemr/src/
COPY interface/  /var/www/localhost/htdocs/openemr/interface/

# Docker COPY creates files owned by root:root. The openemr/openemr base image
# owns all files as apache:root, and its entrypoint only runs chmod (not chown).
# So without this fix, files end up root:root 400 — unreadable by Apache
# (which runs as user 'apache'). Match the base image ownership so chmod 400
# leaves them readable by Apache.
RUN chown -R apache:root \
    /var/www/localhost/htdocs/openemr/src/ \
    /var/www/localhost/htdocs/openemr/interface/
