#!/usr/bin/env python3
"""Authenticated speed baseline: N calls to each main GET endpoint, p50/p95 in ms. Reads the session file written by nexus_login.py; prints a markdown table."""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

JAR = os.environ.get("NEXUS_SESSION_FILE", "/tmp/nexus_session.json")
N = int(os.environ.get("N", "20"))
ENDPOINTS = [
    "/api/v1/healthz", "/api/v1/auth/me", "/api/v1/brokers", "/api/v1/brokers/portfolio-summary", "/api/v1/brokers/dhan/portfolio",
    "/api/v1/trades/analytics/journal", "/api/v1/trades/journal", "/api/v1/brokers/dhan/reconciliation?day=2026-11-23",
    "/api/v1/research/multibagger", "/api/v1/market-data/watchlists", "/api/v1/market-data/rail-summary",
    "/api/v1/forward-tests", "/api/v1/crypto-delta/forward-tests", "/api/v1/delta-research/overview", "/api/v1/delta-scanner",
    "/api/v1/signals-lab/strategies", "/api/v1/market-data/settings",
]


def one(host: str, cookie: str, path: str) -> tuple[int, float]:
    request = urllib.request.Request(host + path, headers={"Cookie": cookie, "Accept": "application/json"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    return status, (time.perf_counter() - start) * 1000


def main() -> int:
    session = json.load(open(JAR))
    cookie = f"session_token={session['session_token']}; csrf_token={session['csrf_token']}"
    print(f"| endpoint | status | p50 ms | p95 ms | calls |\n|---|---|---|---|---|")
    for path in ENDPOINTS:
        times, statuses = [], set()
        for _ in range(N):
            status, ms = one(session["host"], cookie, path)
            statuses.add(status)
            times.append(ms)
        times.sort()
        p95 = times[min(len(times) - 1, int(round(0.95 * len(times))) - 1)]
        print(f"| {path} | {','.join(map(str, sorted(statuses)))} | {statistics.median(times):.0f} | {p95:.0f} | {N} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
