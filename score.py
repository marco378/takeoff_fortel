#!/usr/bin/env python3
"""Score engine output against gold answers (area MAPE + count exact-match)."""
import json
from engine import takeoff

gold = {k: v for k, v in json.load(open("gold.json")).items() if not k.startswith("_")}


def score(params=None):
    params = params or {}
    rows, allpass = [], True
    for fn, g in gold.items():
        r = takeoff(f"drawings/{fn}",
                    scale_override=params.get("scale_override"),
                    marker_max_m2=params.get("marker_max_m2", 5.0))
        net = r.get("net_m2", 0)
        err = abs(net - g["net_m2"]) / g["net_m2"] * 100
        cnt_ok = r.get("marker_count") == g.get("marker_count")
        ok = err <= g["tol_pct"] and cnt_ok
        allpass &= ok
        rows.append((fn, net, g["net_m2"], round(err, 2), r.get("marker_count"), g.get("marker_count"), "PASS" if ok else "FAIL"))
    return allpass, rows


if __name__ == "__main__":
    ap, rows = score()
    print(f"{'file':26}{'net':>9}{'gold':>9}{'err%':>7}{'cnt':>5}{'gold':>5}  result")
    for r in rows:
        print(f"{r[0]:26}{r[1]:>9}{r[2]:>9}{r[3]:>7}{str(r[4]):>5}{str(r[5]):>5}  {r[6]}")
    print("\nALL PASS ✅" if ap else "\nFAILURES ❌")
