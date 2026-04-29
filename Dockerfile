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
