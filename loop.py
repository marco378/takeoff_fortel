#!/usr/bin/env python3
"""
Self-improving takeoff loop:  run -> score -> diagnose -> adjust -> repeat.

This is the reusable harness. It starts from a DELIBERATELY wrong calibration
(scale 1:100) to demonstrate self-correction, detects the large area error,
falls back to the auto-detected scale, and converges. The `diagnose_and_adjust`
function is where richer fixes plug in (LLM re-classification of zones, void
re-selection, raster fallback, etc.).
"""
import json
from engine import takeoff
from score import gold


def run(params):
    rows, allpass = [], True
    for fn, g in gold.items():
        r = takeoff(f"drawings/{fn}",
                    scale_override=params.get("scale_override"),
                    marker_max_m2=params.get("marker_max_m2", 5.0))
        net = r.get("net_m2", 0)
        err = abs(net - g["net_m2"]) / g["net_m2"] * 100
        ok = err <= g["tol_pct"] and r.get("marker_count") == g.get("marker_count")
        allpass &= ok
        rows.append({"file": fn, "net": net, "gold": g["net_m2"], "err_pct": round(err, 2),
                     "count": r.get("marker_count"), "count_gold": g.get("marker_count"),
                     "detected_scale": r.get("scale"), "pass": ok})
    return allpass, rows


def diagnose_and_adjust(params, rows):
    for r in rows:
        if not r["pass"] and r["err_pct"] > 20 and params.get("scale_override"):
            p = dict(params); p["scale_override"] = None
            return p, "large area error + forced scale -> drop override, use auto-detected scale"
    return params, "no automatic fix — escalate to human / LLM-judge"


if __name__ == "__main__":
    params = {"scale_override": 100.0}   # wrong on purpose, to show convergence
    history, ap = [], False
    for it in range(5):
        ap, rows = run(params)
        print(f"iter {it}: params={params} -> {'PASS' if ap else 'FAIL'} "
              f"(net={rows[0]['net']}, err={rows[0]['err_pct']}%, count={rows[0]['count']})")
        history.append({"iter": it, "params": dict(params), "all_pass": ap, "rows": rows})
        if ap:
            break
        params, why = diagnose_and_adjust(params, rows)
        print("   -> adjust:", why)
    json.dump(history, open("loop_history.json", "w"), indent=2)
    print("\nCONVERGED ✅" if ap else "\nSTOPPED — needs human", "(history -> loop_history.json)")
