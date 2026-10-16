"""Merge a fresh cookie export into the client's cookie file.

Strategy: a fresh browser export legitimately drops cookies the user never
touches (analytics beacons) but may also omit session-critical ones. Blindly
overwriting is how a working session dies. So: overwrite by name, KEEP any
existing cookie the new export does not mention (unless it is an analytics
beacon), and back up before writing.
"""
import json
import os
import shutil
import sys
import time

TARGET = "/root/youdo-monitor/data/cookies_export.json"

# Analytics-only cookies: safe to drop if the new export omits them.
BEACONS = {"_ga", "_ga_62CM61Z0G9", "_ym_isad", "_ym_visorc", "_ym_d",
           "_ym_uid", "mdd", "accessCookies", "xguid", "xidx", "_ga_fp",
           "_utm_yf", "_utm_ydmd", "mindboxDeviceUUID", "directCrm-session"}


def main(new_path: str) -> int:
    with open(new_path, encoding="utf-8") as f:
        fresh = json.load(f)
    old = []
    if os.path.exists(TARGET):
        with open(TARGET, encoding="utf-8") as f:
            old = json.load(f)

    bak = f"{TARGET}.bak-{int(time.time())}"
    shutil.copy2(TARGET, bak)
    print(f"backup -> {bak}")

    out = {c["name"]: c for c in fresh}
    dropped = []
    for c in old:
        name = c["name"]
        if name in out:
            continue
        if name in BEACONS:
            dropped.append(name)
            continue
        out[name] = c
        print(f"kept from old export: {name}")
    if dropped:
        print(f"dropped beacons: {dropped}")

    merged = list(out.values())
    with open(TARGET, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(merged)} cookies ({len(fresh)} fresh, {len(merged) - len(fresh)} carried over)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
