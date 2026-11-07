"""External production health check with an optional generic JSON alert webhook."""

import argparse
import json
import sys
import urllib.request


def check(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("status") != "ok" or payload.get("database") is not True:
        raise RuntimeError(f"unhealthy response: status={payload.get('status')}, database={payload.get('database')}")
    return payload


def alert(webhook: str, health_url: str, message: str) -> None:
    body = json.dumps({"text": f"AutoTrade production alert: {message}", "health_url": health_url}).encode()
    request = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--alert-webhook")
    args = parser.parse_args()
    try:
        result = check(args.url)
        print(json.dumps(result, separators=(",", ":")))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        if args.alert_webhook:
            try:
                alert(args.alert_webhook, args.url, message)
            except Exception as alert_exc:
                print(f"alert delivery failed: {alert_exc}", file=sys.stderr)
        raise SystemExit(1)
