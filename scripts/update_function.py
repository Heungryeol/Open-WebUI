#!/usr/bin/env python3
"""Push functions/pais_multi_agent_pipe.py into a running Open WebUI (admin API key).

    OPENWEBUI_URL=https://openwebui.rangers.lab OPENWEBUI_KEY=sk-... python3 scripts/update_function.py [--insecure]

Reads the function as it is in Open WebUI (name, meta), replaces only `content` with this repo's file and
saves it through /api/v1/functions/id/<id>/update. Valves live in the DB and stay; still check the Oidc
Client Id / Password afterwards (README). The function id is the model id's prefix
("pais_multi_agent_pipe_authentik_oauth2_password_grant")."""
import json
import os
import ssl
import sys
import urllib.request

FUNCTION_ID = os.environ.get("OPENWEBUI_FUNCTION_ID", "pais_multi_agent_pipe_authentik_oauth2_password_grant")
PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "functions", "pais_multi_agent_pipe.py")


def main() -> int:
    base = os.environ.get("OPENWEBUI_URL", "").rstrip("/")
    key = os.environ.get("OPENWEBUI_KEY", "")
    if not base or not key:
        print("set OPENWEBUI_URL and OPENWEBUI_KEY (Settings > Account > API keys, an admin)")
        return 2
    ctx = ssl._create_unverified_context() if "--insecure" in sys.argv else None     # noqa: S323 - lab CA
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=60) as r:          # noqa: S310
            return json.loads(r.read().decode() or "null")

    current = call("GET", f"/api/v1/functions/id/{FUNCTION_ID}")
    if not current:
        print(f"function {FUNCTION_ID} not found")
        return 1
    content = open(PATH, encoding="utf-8").read()
    if content == current.get("content"):
        print("already up to date")
        return 0
    updated = call("POST", f"/api/v1/functions/id/{FUNCTION_ID}/update",
                   {"id": FUNCTION_ID, "name": current.get("name") or FUNCTION_ID,
                    "content": content, "meta": current.get("meta") or {}})
    print("updated:", updated.get("id"), "| now check Valves: Oidc Client Id / Oidc Password")
    return 0


if __name__ == "__main__":
    sys.exit(main())
