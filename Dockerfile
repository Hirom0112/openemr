FROM openemr/openemr:flex

# Inject proxy trust config before Apache starts
COPY docker/railway/apache-proxy.conf /etc/apache2/conf.d/railway-proxy.conf

# Layer fork code over the base image.
# vendor/, node_modules/, and compiled assets from the base image are preserved.
# sites/ is a runtime Railway volume — not overwritten by COPY at container start.
COPY --chown=apache:apache . /var/www/localhost/htdocs/openemr/

# Remove dev and build artifacts that don't belong in the deployed image
RUN rm -rf /var/www/localhost/htdocs/openemr/docker \
           /var/www/localhost/htdocs/openemr/.git \
           /var/www/localhost/htdocs/openemr/node_modules
