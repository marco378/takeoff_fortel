#!/usr/bin/env python3
"""
Regression: the unmarked pipeline must reproduce the Hemington/SGP areas end-to-end as a SCRIPT
(this is the test for "it works when you run the flow but the .py didn't"). Skips cleanly when the
client tender PDFs aren't present (they are not committed to the repo).

  python3 test_sgp_units.py
"""
import os, io, contextlib, sys
sys.path.insert(0, os.path.dirname(__file__))
import takeoff_unmarked as T

BASE_CANDIDATES = [
    "/sessions/friendly-charming-carson/mnt/Documents/2-Enquiry/01-Tender/SGP/",
    os.path.expanduser("~/Downloads/Documents/2-Enquiry/01-Tender/SGP/"),
]
BASE = next((b for b in BASE_CANDIDATES if os.path.isdir(b)), None)

# (tag, filename, expected_area_m2)  — expected from the validated DEMO2 run; tol 4%.
CASES = [
    ("D77",  "105301-SGP-01-ZZ-DR-A-131002-P03-Unit_1_(D77)_-_Hard_Landscaping.pdf",  3238),
    ("D147", "105301-SGP-02-XX-DR-A-131001-P07-Unit_2_(D147)_-_Hard_Landscaping.pdf", 6584),
    ("D410", "105301-SGP-03-ZZ-DR-A-131001-P06-Unit_3_(D410)_-_Hard_Landscaping.pdf", 16697),
    ("D219", "105301-SGP-04-ZZ-DR-A-131002-P02-Unit_4_(D219)_-_Hard_Landscaping.pdf", 7509),
]
TOL = 0.04


def run():
    if not BASE:
        print("SKIP: SGP tender PDFs not present (client files, not in repo).")
        return 0
    bad = 0
    for tag, fn, exp in CASES:
        path = BASE + fn
        if not os.path.exists(path):
            print(f"SKIP {tag}: file missing"); continue
        with contextlib.redirect_stdout(io.StringIO()):
            r = T.takeoff(path, S=float(os.getenv("TEST_S", "1.5")))
        a = r["area_m2"]
        if a is None:
            print(f"FAIL {tag}: no area emitted ({r['flags'][-1]})"); bad += 1; continue
        err = abs(a - exp) / exp
        ok = err <= TOL
        print(f"{'PASS' if ok else 'FAIL'} {tag}: {a:>7,.0f} m2 (exp ~{exp:,}, {err*100:4.1f}%)  "
              f"GBP {r['price_gbp']:,}  verified_scale={r['scale_verified']}")
        if not ok:
            bad += 1
    print(f"\n{'ALL PASS' if bad == 0 else str(bad)+' FAILED'}  (tol {int(TOL*100)}%)")
    return bad


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
