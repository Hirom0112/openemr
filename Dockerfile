FROM openemr/openemr:latest

# Inject proxy trust config before Apache starts.
# Inlined to avoid build-context path resolution issues.
RUN printf '%s\n' \
    'SetEnvIf X-Forwarded-Proto "https" HTTPS=on' \
    'SetEnvIf X-Forwarded-Proto "https" REQUEST_SCHEME=https' \
    'Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains" env=HTTPS' \
    > /etc/apache2/conf.d/railway-proxy.conf

# Layer fork code over the base image.
# vendor/, node_modules/, and compiled assets from the base image are preserved.
# sites/ is a runtime Railway volume — not overwritten by COPY at container start.
COPY --chown=apache:apache . /var/www/localhost/htdocs/openemr/

# Remove dev and build artifacts that don't belong in the deployed image
RUN rm -rf /var/www/localhost/htdocs/openemr/docker \
           /var/www/localhost/htdocs/openemr/.git \
           /var/www/localhost/htdocs/openemr/node_modules
