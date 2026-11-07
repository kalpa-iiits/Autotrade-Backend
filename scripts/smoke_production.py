"""Read-only authenticated smoke test for the deployed API."""

import json
import os
import urllib.request


def get(base_url: str, path: str, token: str):
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def smoke(base_url: str, token: str, account_id: int) -> None:
    me = get(base_url, "/api/auth/me", token)
    if not me.get("username"):
        raise RuntimeError("authenticated identity response is incomplete")
    account = get(base_url, f"/api/accounts/{account_id}", token)
    if account.get("id") != account_id:
        raise RuntimeError("account isolation smoke check returned the wrong account")
    get(base_url, f"/api/accounts/{account_id}/funds", token)
    get(base_url, "/api/strategies/catalog", token)


if __name__ == "__main__":
    url = os.environ.get("AUTOTRADE_SMOKE_URL", "")
    token = os.environ.get("AUTOTRADE_SMOKE_TOKEN", "")
    account = os.environ.get("AUTOTRADE_SMOKE_ACCOUNT_ID", "")
    if not url or not token or not account:
        raise SystemExit("AUTOTRADE_SMOKE_URL, AUTOTRADE_SMOKE_TOKEN and AUTOTRADE_SMOKE_ACCOUNT_ID are required")
    smoke(url, token, int(account))
    print("Authenticated production smoke test passed")
