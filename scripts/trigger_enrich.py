"""One-off: mint an admin session and trigger the catalog_enrich job locally.

Usage (on the server, with the service venv python):
    python trigger_enrich.py steps=tags force=1
"""

import json
import sys
import urllib.request
from datetime import datetime, timedelta, UTC

import jwt

CONFIG_PATH = "/etc/kryten-webqueue/config.json"


def main() -> int:
    params = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    cfg = json.load(open(CONFIG_PATH))
    secret = cfg["secret_key"]
    port = cfg.get("port", 2010)
    token = jwt.encode(
        {
            "sub": "backfill-admin",
            "rank": 3,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )
    body = json.dumps({"params": params}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/admin/jobs/catalog_enrich/run",
        data=body,
        headers={"Content-Type": "application/json", "Cookie": f"session={token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(resp.status, resp.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
