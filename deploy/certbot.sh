#!/usr/bin/env bash
# HTTPS bootstrap and renewal with Let's Encrypt (HTTP-01 through the running Nginx).
#   ./deploy/certbot.sh init  DOMAIN EMAIL     first certificate (creates a temporary self-signed one so Nginx can start)
#   ./deploy/certbot.sh renew                  renew if due, then reload Nginx  (run from cron/systemd timer, e.g. twice a day)
# Requires: DNS for DOMAIN points at this server, ports 80/443 open, `.env` filled in.
set -euo pipefail
cd "$(dirname "$0")/.."
CERTS="$PWD/data/certs"
CB=(docker run --rm -v ytaria_letsencrypt:/etc/letsencrypt -v ytaria_certbot-www:/var/www/certbot
    -v "$CERTS:/certs" -v "$PWD/deploy/certbot-deploy-hook.sh:/hook.sh:ro" --entrypoint certbot certbot/certbot:latest)

case "${1:-}" in
  init)
    DOMAIN="${2:?usage: certbot.sh init DOMAIN EMAIL}"; EMAIL="${3:?usage: certbot.sh init DOMAIN EMAIL}"
    mkdir -p "$CERTS"
    docker volume create ytaria_letsencrypt >/dev/null; docker volume create ytaria_certbot-www >/dev/null
    if [ ! -s "$CERTS/fullchain.pem" ]; then
      echo "Creating a temporary self-signed certificate so Nginx can start..."
      openssl req -x509 -nodes -newkey rsa:2048 -days 1 -subj "/CN=$DOMAIN" -keyout "$CERTS/privkey.pem" -out "$CERTS/fullchain.pem" 2>/dev/null
      docker run --rm -v "$CERTS:/certs" alpine sh -c 'chown 101:101 /certs/*.pem && chmod 444 /certs/fullchain.pem && chmod 400 /certs/privkey.pem'
    fi
    docker compose up -d web
    echo "Requesting a certificate for $DOMAIN..."
    "${CB[@]}" certonly --webroot -w /var/www/certbot -d "$DOMAIN" --email "$EMAIL" --agree-tos --no-eff-email \
      --deploy-hook "sh /hook.sh"
    docker compose exec web nginx -s reload
    echo "Done. Test with: curl -I https://$DOMAIN/"
    ;;
  renew)
    "${CB[@]}" renew --webroot -w /var/www/certbot --deploy-hook "sh /hook.sh"
    docker compose exec web nginx -s reload
    ;;
  *) echo "usage: $0 init DOMAIN EMAIL | renew" >&2; exit 2 ;;
esac
