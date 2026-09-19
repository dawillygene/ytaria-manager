#!/bin/sh
# Runs inside the certbot container after every successful issue/renewal.
# Let's Encrypt keys are root-only; Nginx runs as uid 101, so publish a copy it can read.
set -eu
cp "$RENEWED_LINEAGE/fullchain.pem" /certs/fullchain.pem
cp "$RENEWED_LINEAGE/privkey.pem" /certs/privkey.pem
chown 101:101 /certs/fullchain.pem /certs/privkey.pem
chmod 444 /certs/fullchain.pem
chmod 400 /certs/privkey.pem
