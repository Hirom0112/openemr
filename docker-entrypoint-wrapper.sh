#!/bin/sh
# Run the real OpenEMR entrypoint in the background, wait for Apache to come up,
# then fix module file permissions that the entrypoint resets to 400/500.
# The www user owns the files (set by entrypoint), 400 = readable by owner only,
# which should work — but something in the Alpine/PHP setup prevents is_readable()
# from returning true. Explicitly re-chmod to 644/755 after Apache starts.

/run.sh &
OPENEMR_PID=$!

# Wait until Apache is actually serving requests (up to 60s)
i=0
while [ $i -lt 60 ]; do
    if wget -q -O /dev/null "http://localhost:80/" 2>/dev/null; then
        break
    fi
    sleep 1
    i=$((i+1))
done

# Fix module permissions so PHP can read the bootstrap file
MODULE_DIR="/var/www/localhost/htdocs/openemr/interface/modules/custom_modules/oe-module-clinical-copilot"
find "$MODULE_DIR" -type f -exec chmod 644 {} \;
find "$MODULE_DIR" -type d -exec chmod 755 {} \;

wait $OPENEMR_PID
