proxy_pass http://api:8000;
proxy_http_version 1.1;
# $http_host keeps the port (Origin checks compare against it); $host would drop it.
proxy_set_header Host $http_host;
# Overwrite (never append) so a client cannot spoof its address for rate limiting.
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Connection "";
# File transfers stream straight through; nothing is buffered to disk.
proxy_buffering off;
proxy_request_buffering on;
proxy_connect_timeout 5s;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
