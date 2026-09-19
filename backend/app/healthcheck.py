"""Container health probes (avoids quoting pitfalls of ``python -c`` in compose files).

  python -m app.healthcheck http http://127.0.0.1:8000/api/health
  python -m app.healthcheck tcp 127.0.0.1 3128
"""

from __future__ import annotations

import socket
import sys
import urllib.request


def main(argv: list[str]) -> int:
    try:
        if argv[0] == "http":
            with urllib.request.urlopen(argv[1], timeout=3) as resp:  # noqa: S310 - fixed loopback URL
                return 0 if resp.status == 200 else 1
        if argv[0] == "tcp":
            socket.create_connection((argv[1], int(argv[2])), 3).close()
            return 0
    except Exception:
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
