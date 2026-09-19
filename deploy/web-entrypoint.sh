#!/bin/sh
# Pick the HTTP (dev) or HTTPS (prod) server template before Nginx's own templating step runs.
set -eu
mode="${YTARIA_TLS:-on}"
rm -rf /etc/nginx/templates && mkdir -p /etc/nginx/templates
if [ "$mode" = "on" ]; then
  cp /etc/nginx/templates-all/https/default.conf.template /etc/nginx/templates/
  cp /etc/nginx/extra_headers.https.inc /etc/nginx/extra_headers.inc
else
  cp /etc/nginx/templates-all/http/default.conf.template /etc/nginx/templates/
  cp /etc/nginx/extra_headers.http.inc /etc/nginx/extra_headers.inc
fi
