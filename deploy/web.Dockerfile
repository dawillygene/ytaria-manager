# Build the SPA, then serve it with unprivileged Nginx (which also reverse-proxies /api).
# Build context: the repository root.
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
# Same-origin in the web deployment: no API base URL is baked in.
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
USER root
RUN rm -f /etc/nginx/conf.d/default.conf
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/site.inc deploy/nginx/proxy_api.inc deploy/nginx/security_headers.inc /etc/nginx/
COPY deploy/nginx/templates /etc/nginx/templates-all
COPY deploy/nginx/extra_headers.http.inc /etc/nginx/extra_headers.http.inc
COPY deploy/nginx/extra_headers.https.inc /etc/nginx/extra_headers.https.inc
COPY deploy/web-entrypoint.sh /docker-entrypoint.d/05-ytaria-mode.sh
COPY --from=build /app/dist /usr/share/nginx/html
RUN chmod +x /docker-entrypoint.d/05-ytaria-mode.sh && chown -R 101:0 /etc/nginx && chmod -R g+rwX /etc/nginx
USER 101
ENV NGINX_ENVSUBST_FILTER=YTARIA_ NGINX_ENVSUBST_TEMPLATE_DIR=/etc/nginx/templates
