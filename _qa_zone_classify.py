"""Portal QA for the zone-classification control an assessor could not find.

On the 16 Sep 2026 handover call the control that unblocks approval sat in the last column of
a table wider than the 280px panel holding it, off the right edge behind a horizontal
scrollbar. It took Aryan forty seconds of spoken directions -- "right hand side... scroll
down... scroll up... stop here" -- to talk Inderjit to it, and Inderjit's conclusion was that
classification lives "in two different places".

It now has its own full-width row. This drives the path he was actually on: trace a region,
submit it with no category (which is what the portal sends for a manual outline), and check
that the control is BOTH present and inside the panel's own width -- because "it renders" and
"you can reach it without scrolling sideways" are different claims, and only the second one
was ever the problem.

Both JOBS_FILE and DRAWINGS_DIR point at scratch.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error

PORT = int(os.environ.get("QA_PORT", "8793"))
BASE = f"http://127.0.0.1:{PORT}"
SHEET = os.environ.get("QA_SHEET",
    "drawings/inderjit_13sep/radlett_2_Surface_Finishes_Plan.pdf")
SCRATCH = tempfile.mkdtemp(prefix="qa_zoneclass_")
SHOTS = os.path.join(SCRATCH, "shots")
os.makedirs(SHOTS, exist_ok=True)
FAILS = []


def ck(name, cond, got=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {got}")
    if not cond:
        FAILS.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read())


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


env = dict(os.environ, APPROVAL_PORT=str(PORT),
           JOBS_FILE=os.path.join(SCRATCH, "jobs.json"),
           DRAWINGS_DIR=os.path.join(SCRATCH, "drawings"),
           LEARNING_ENVIRONMENT="qa")
os.makedirs(env["DRAWINGS_DIR"], exist_ok=True)
server = subprocess.Popen([".venv/bin/python", "approval_server.py"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "/status", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("server never came up")

    boundary = "----qaboundary"
    pdf = open(SHEET, "rb").read()
    parts = []
    for key, value in (("project_name", "QA Zone Classify"), ("project_ref", "QA-ZC-001")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                     f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; "
                 f"filename=\"zoneclass.pdf\"\r\nContent-Type: application/pdf\r\n\r\n".encode()
                 + pdf + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + "/upload", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        job_id = json.loads(r.read()).get("job_id")
    ck("upload accepted", bool(job_id), f"-> {job_id}")
    for _ in range(600):
        job = get(f"/job/{job_id}")
        if job.get("status") != "processing":
            break
        time.sleep(1)

    # What the portal sends for a hand-drawn outline the assessor has not categorised: a
    # region with category 'unclassified'. That is the exact state that blocks approval and
    # sends him looking for the control.
    square = [[600, 600], [1400, 600], [1400, 1400], [600, 1400]]
    status, body = post(f"/adjust/{job_id}", {
        "action": "adjust", "assessed_area_m2": None, "scale_k": 0.075,
        "regions": [square], "vertices": square,
        "region_categories": ["unclassified"],
    })
    ck("an uncategorised traced region is accepted", status == 200,
       f"-> {status} {str(body)[:160]}")

    job = get(f"/job/{job_id}")
    zones = job.get("zones") or (job.get("result") or {}).get("zones") or []
    unclassified = [z for z in zones if str(z.get("category") or "").lower() == "unclassified"]
    ck("...and lands as an unclassified zone, which is what blocks approval",
       bool(unclassified), f"-> {[z.get('category') for z in zones]}")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.goto(f"{BASE}/portal", wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.evaluate(f"selectJob({job_id!r})")
        page.wait_for_timeout(3000)

        # Match the panel by its own header div, then take that div's parent. Matching any
        # div whose text merely STARTS with the header caught the whole info panel instead.
        panel_text = page.evaluate(
            "(() => {const h=[...document.querySelectorAll('div')]"
            ".find(d=>d.children.length===0 && d.textContent.trim()==='Measured zones');"
            " return h && h.parentElement ? h.parentElement.innerText : '';})()")
        # The header is upper-cased by CSS, so compare case-insensitively.
        ck("the Measured zones panel is on screen",
           panel_text.strip().upper().startswith("MEASURED ZONES"), f"-> {panel_text[:60]!r}")
        ck("...and it carries the classify prompt on its own row",
           "CLASSIFY THIS ZONE" in panel_text.upper(), f"-> {panel_text[:160]!r}")

        # The actual complaint was reachability, not existence.
        fits = page.evaluate(
            "(() => {const s=document.querySelector('select[id^=\"zoneClass-\"]');"
            " if(!s) return null;"
            " const panel=document.getElementById('infoPanel');"
            " const sr=s.getBoundingClientRect(), pr=panel.getBoundingClientRect();"
            " return {selRight: Math.round(sr.right), panelRight: Math.round(pr.right),"
            "  selLeft: Math.round(sr.left), panelLeft: Math.round(pr.left)};})()")
        ck("the classify dropdown exists", fits is not None, f"-> {fits}")
        ck("...and sits inside the panel, not off its right edge",
           bool(fits) and fits["selRight"] <= fits["panelRight"]
           and fits["selLeft"] >= fits["panelLeft"], f"-> {fits}")

        no_h_scroll = page.evaluate(
            "(() => {const t=[...document.querySelectorAll('table')]"
            ".find(x=>x.innerText.includes('Bluebeam subject'));"
            " if(!t) return null;"
            " const w=t.parentElement;"
            " return {scrollW: w.scrollWidth, clientW: w.clientWidth};})()")
        ck("...and the zones table no longer needs sideways scrolling",
           bool(no_h_scroll) and no_h_scroll["scrollW"] <= no_h_scroll["clientW"] + 2,
           f"-> {no_h_scroll}")

        page.evaluate("document.querySelector('select[id^=\"zoneClass-\"]')"
                      ".scrollIntoView({block:'center'})")
        page.wait_for_timeout(600)
        page.screenshot(path=os.path.join(SHOTS, "01_zone_classify_row.png"))
        browser.close()

    size = os.path.getsize(os.path.join(SHOTS, "01_zone_classify_row.png"))
    ck("screenshot captured", size > 20000, f"-> {size} bytes")
    print("     screenshots:", SHOTS)
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except Exception:
        server.kill()

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
print("scratch:", SCRATCH)
sys.exit(1 if FAILS else 0)
