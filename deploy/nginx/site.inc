# Shared by the HTTP (dev) and HTTPS (prod) server blocks.
root /usr/share/nginx/html;
client_max_body_size 64k;

include /etc/nginx/security_headers.inc;

location = /healthz { access_log off; add_header Content-Type text/plain; return 200 "ok\n"; }

location /api/auth/ {
  limit_req zone=auth burst=10 nodelay;
  include /etc/nginx/proxy_api.inc;
}
location /api/ {
  limit_req zone=api burst=60 nodelay;
  limit_conn perip 20;
  include /etc/nginx/proxy_api.inc;
}

location /assets/ {
  expires 1y;
  add_header Cache-Control "public, immutable";
  include /etc/nginx/security_headers.inc;
  try_files $uri =404;
}
location / {
  add_header Cache-Control "no-cache";
  include /etc/nginx/security_headers.inc;
  try_files $uri /index.html;
}
location ~ /\. { deny all; }
