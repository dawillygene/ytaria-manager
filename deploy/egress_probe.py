import socket, urllib.request, urllib.error, os
def tcp(host, port):
    try:
        socket.create_connection((host, port), 3).close(); return "CONNECTED"
    except Exception as e:
        return f"blocked ({type(e).__name__})"
print("direct 1.1.1.1:443      ->", tcp("1.1.1.1", 443))
print("direct 169.254.169.254  ->", tcp("169.254.169.254", 80))
try:
    print("dns archive.org         ->", socket.gethostbyname("archive.org"))
except Exception as e:
    print("dns archive.org         -> fails (%s)" % type(e).__name__)
print("worker -> api:8000      ->", tcp("api", 8000))
print("worker -> web:8080      ->", tcp("web", 8080))
print("worker -> db:5432       ->", tcp("db", 5432), "(required)")
print("worker -> redis:6379    ->", tcp("redis", 6379), "(required)")
print("worker -> egress:3128   ->", tcp("egress", 3128), "(required)")
proxy = urllib.request.ProxyHandler({"http": "http://egress:3128", "https": "http://egress:3128"})
op = urllib.request.build_opener(proxy)
def via(url):
    try:
        r = op.open(url, timeout=15); return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.headers.get('X-Ytaria-Egress','')}"
    except Exception as e:
        return f"error {type(e).__name__}: {str(e)[:70]}"
for u in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/", "http://db:5432/", "http://api:8000/api/health", "https://10.0.0.1/", "http://[::1]/", "https://archive.org/robots.txt"):
    print("via proxy", u.ljust(44), "->", via(u))
