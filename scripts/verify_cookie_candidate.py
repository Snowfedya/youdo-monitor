"""Decide whether a fresh cookie export is worth adopting.

The live cookie file currently authenticates. A pasted export that turns out
to be a GUEST session would silently downgrade the monitor to no-account mode
and quietly stop forwarding notifications. So: test the candidate against a
THROWAWAY browser profile first, report both results, and let the caller
decide. Never overwrite the live file from this script.

Usage: python3 verify_cookie_candidate.py <candidate.json>
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

LIVE = "/root/youdo-monitor/data/cookies_export.json"

PROBE_CODE = r"""
import json, os, sys, tempfile
sys.path.insert(0, "/root/youdo-monitor")
import youdo_monitor.config as cfg

cookie_file, profile_dir = sys.argv[1], sys.argv[2]

import youdo_monitor.client as client
client.COOKIE_FILE = cookie_file
client.PLAYWRIGHT_PROFILE_DIR = profile_dir
# Config imports resolve the profile at module import time; the client reads
# the module-level constant, so rebinding it there is what actually matters.
with client.YouDoClient(headless=True) as c:
    print("RESULT " + json.dumps(c.check_auth_status(), ensure_ascii=False))
"""


def run(cookie_file: str) -> dict:
    """Auth check for one cookie file, isolated profile, temp copies only."""
    profile_dir = tempfile.mkdtemp(prefix="ydcookieprobe-")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", PROBE_CODE, cookie_file, profile_dir],
            capture_output=True, text=True, timeout=240,
        )
        for line in (proc.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        return {"error": "no probe result", "stderr": (proc.stderr or "")[-400:]}
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def main() -> int:
    candidate = sys.argv[1]
    live = run(LIVE)
    print("LIVE     :", json.dumps(live, ensure_ascii=False))

    cand = run(candidate)
    print("CANDIDATE:", json.dumps(cand, ensure_ascii=False))

    live_auth = bool(live.get("authenticated"))
    cand_auth = bool(cand.get("authenticated"))
    verdict = {
        "live_authenticated": live_auth,
        "candidate_authenticated": cand_auth,
        # Adopt only if the candidate authenticates AND we are not losing
        # anything. An unauthenticated candidate is never an improvement.
        "adopt_candidate": cand_auth and cand_auth == live_auth,
    }
    print("VERDICT:", json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict["adopt_candidate"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
