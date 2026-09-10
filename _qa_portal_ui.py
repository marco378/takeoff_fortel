"""QA the portal UI fixes on a REAL Fortel drawing (project-8 hatch sheet), headless.

Isolated jobs file so nothing lands in approval_jobs.json.
"""
import asyncio, os, json, re, time, uuid, urllib.request, sys

PORT = os.environ.get("QA_PORT", "5111")
BASE = f"http://127.0.0.1:{PORT}"
OUT  = os.environ["QA_OUT"]
P8   = "drawings/inderjit_p8/8_14173-TCG-XX_XX-XX-SK-C-0003_CONCRETE_SLAB_MSA.pdf"
INDURENT = ("drawings/inderjit_p9p10/11_Indurent_Park_Newport_22513-RLL-25-00-DR-C-3151"
            "_P02_Proposed_Pavement_Construction.pdf")
# The other three sheets Inderjit sent on 4 Sep. All three are correctly UNMEASURED; what was
# wrong was that the portal never said so where he could see it.
REFUSED_SHEETS = [
    ("roscoe", "drawings/inderjit_p9p10/10_26051-ROS-00-XX-DR-C-05101.pdf"),
]
# South Mimms measures from its CAD layer name (9 Sep) and 2105 measures from the only
# stipple in its own legend (10 Sep). Both were in REFUSED_SHEETS and had to come out --
# leaving them would have asserted the wrong behaviour and passed while the product changed.
MIMMS = "drawings/inderjit_p9p10/12_South_Mimms.pdf"
SPEC2105 = ("drawings/inderjit_p9p10/9_25010-RLL-26-XX-DR-C-2105"
            "_P01_External_Construction_Specification.pdf")
# LDSS2 refuses -- nothing on it says which layer is the priced surface -- but it DOES carry
# four closed boundaries the engineer drew. They must reach the SCREEN, not just the flags:
# the pipeline dropped them on the refusal branch because yard_regions was only copied when
# an area existed, which is server-side-correct and browser-invisible, the exact failure the
# client caught last time.
LDSS2 = "drawings/aryan_10sep/LDSS2-01-DR-C-151-XX-ZZ-PVMT-ARP.pdf"

def upload(path, name, ref, client):
    boundary = "----qa" + uuid.uuid4().hex
    fields = {"project_name": name, "project_ref": ref, "client_name": client}
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; "
             f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
    body += open(path, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/upload", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.load(urllib.request.urlopen(req))["job_id"]

def wait_done(jid):
    for _ in range(90):
        jobs = json.load(urllib.request.urlopen(f"{BASE}/jobs"))
        if jobs.get(jid, {}).get("status") not in ("processing", None):
            return jobs[jid]
        time.sleep(2)
    return jobs.get(jid, {})

RESULTS = []
def ck(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("" if detail == "" else f"  {detail}"))

async def main():
    os.makedirs(OUT, exist_ok=True)
    jid = upload(P8, "P8 TCG hatch QA", "QA-P8", "Knauf")
    job = wait_done(jid)
    print("p8 ->", job.get("measurement_state"), job.get("area_m2"))

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1600, "height": 950})
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(f"{BASE}/portal?job={jid}", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(6000)
        await page.screenshot(path=f"{OUT}/01_p8_canvas.png")

        # ── multi-region rendering ──────────────────────────────────────────────────────
        regions = await page.evaluate("aiRegions.map(r => ({cat:r.category, pts:r.points.length, holes:r.holes.length, area:r.area_m2}))")
        ck("canvas carries every measured part, not one polygon", len(regions) >= 5, json.dumps(regions))
        ck("the ring-shaped road part carries its hole", any(r["holes"] >= 1 for r in regions),
           json.dumps([r["holes"] for r in regions]))
        ck("both surfaces are represented", {r["cat"] for r in regions} >= {"external_yard", "unclassified"},
           json.dumps(sorted({r["cat"] for r in regions})))

        # ── the headline area covers every measured zone, not just the primary ──────────
        headline = await page.evaluate("""(() => ({
          area: document.getElementById('areaDisplay').textContent.trim(),
          source: document.getElementById('areaSource').textContent.trim(),
          summary: (document.getElementById('summaryLine') || {textContent:''}).textContent.trim(),
          zones: (currentJob.result.zones || []).map(z => [z.category, z.area_m2])
        }))()""")
        total = sum(a for _, a in headline["zones"])
        ck("headline area is the sum of the measured zones, not the primary one only",
           headline["area"].startswith(f"{round(total):,}"), f"{headline['area']} vs {total:,.1f}")
        ck("...and it shows the split, so nobody has to guess what it is made of",
           "yard" in headline["source"] and "unclassified" in headline["source"], headline["source"])

        # ── the hole is actually CUT on the canvas, not just present in the data ────────
        # I told Aryan the canvas could not draw holes. Prove it either way by sampling the
        # rendered pixels: a point inside the ring's hole must not carry the surface tint that
        # a point inside the ring band does.
        hole_probe = await page.evaluate("""(() => {
          const ring = aiRegions.find(r => r.holes && r.holes.length);
          if (!ring) return {ok:false, why:'no ring region'};
          const hole = ring.holes[0];
          const cx = hole.reduce((s,p)=>s+p[0],0)/hole.length;
          const cy = hole.reduce((s,p)=>s+p[1],0)/hole.length;
          // a point on the ring band itself: midway between the outer ring and its hole
          const ox = ring.points.reduce((s,p)=>s+p[0],0)/ring.points.length;
          const oy = ring.points.reduce((s,p)=>s+p[1],0)/ring.points.length;
          const bx = Math.round(cx + (ring.points[0][0]-cx)*0.85);
          const by = Math.round(cy + (ring.points[0][1]-cy)*0.85);
          const px = (x,y) => Array.from(ctx.getImageData(Math.round(x), Math.round(y), 1, 1).data);
          return {ok:true, inHole: px(cx,cy), onBand: px(bx,by), cx, cy, bx, by};
        })()""")
        if hole_probe.get("ok"):
            in_hole, on_band = hole_probe["inHole"], hole_probe["onBand"]
            ck("the ring's hole is CUT on the canvas — the tint stops at the hole",
               in_hole != on_band, f"inside hole {in_hole} vs on band {on_band}")
        else:
            ck("the ring's hole is CUT on the canvas — the tint stops at the hole",
               False, hole_probe.get("why"))

        # ── zoom-aware hit radii ────────────────────────────────────────────────────────
        r1 = await page.evaluate("(() => { zoom = 1; return hitRadius(10); })()")
        r8 = await page.evaluate("(() => { zoom = 8; return hitRadius(10); })()")
        await page.evaluate("zoom = 1; applyCanvasTransform();")
        ck("vertex grab radius is constant on SCREEN, not in canvas pixels",
           abs(r1 - 10) < 1e-9 and abs(r8 - 1.25) < 1e-9, f"zoom1={r1} zoom8={r8}")

        # ── undo during cut-out drawing (the reported defect) ───────────────────────────
        state = await page.evaluate("""(() => {
          mode = 'cutout'; cutoutPolygons = []; undoStack = [];
          pushHistory('start cut-out'); cutoutPolygons.push({points:[{x:100,y:100}], closed:false});
          pushHistory('cut-out point');  cutoutPolygons[0].points.push({x:200,y:100});
          pushHistory('cut-out point');  cutoutPolygons[0].points.push({x:200,y:200});
          const before = cutoutPolygons[0].points.length;
          undoLast();
          const after = cutoutPolygons.length ? cutoutPolygons[0].points.length : 0;
          undoLast(); undoLast();
          return {before, after, finally_: cutoutPolygons.length};
        })()""")
        ck("Undo takes back a cut-out point (it used to do nothing in cut-out mode)",
           state["before"] == 3 and state["after"] == 2, json.dumps(state))
        ck("Undo all the way back removes the cut-out entirely", state["finally_"] == 0, json.dumps(state))

        # ── undo restores a deleted separate area (Inderjit's lost markup) ──────────────
        restored = await page.evaluate("""(() => {
          areaElements = []; activeAreaElement = null; undoStack = [];
          areaElements.push({elementId:'qa-1', name:'Footpath', category:'external_yard',
                             saved:true, points:[{x:10,y:10},{x:90,y:10},{x:90,y:90}]});
          removeAreaElement('qa-1');
          const afterDelete = areaElements.length;
          undoLast();
          return {afterDelete, afterUndo: areaElements.length, name: (areaElements[0]||{}).name};
        })()""")
        ck("a deleted separate area comes back with Undo", restored["afterDelete"] == 0
           and restored["afterUndo"] == 1 and restored["name"] == "Footpath", json.dumps(restored))

        # ── delete affordance: destructive control is not in Save's slot ────────────────
        markup = await page.evaluate("""(() => {
          areaElements = [{elementId:'qa-2', name:'Duct slab', category:'dock', saved:true,
                           points:[{x:10,y:10},{x:90,y:10},{x:90,y:90}]}];
          renderAreaElementsEditor();
          const row = document.querySelector('#areaElementsEditor .area-element-row');
          const buttons = Array.from(row.querySelectorAll('button')).map(b => b.textContent.trim());
          return {buttons, firstIsDelete: /Delete/.test(buttons[0] || ''), lastIsDelete: /Delete/.test(buttons[buttons.length-1] || '')};
        })()""")
        ck("the destructive control reads 'Delete' and sits first, not where Save sat",
           markup["firstIsDelete"] and not markup["lastIsDelete"], json.dumps(markup))
        await page.screenshot(path=f"{OUT}/02_area_element_row.png")

        # ── area-elements list scrolls horizontally (Aryan's 2 Sep fix — verify only) ───
        scrollable = await page.evaluate("""(() => {
          areaElements = ['a','b','c','d','e'].map((n,i) => ({elementId:'qa-s'+i, name:'Separate area '+n,
            category:'external_yard', saved:true, points:[{x:10,y:10},{x:90,y:10},{x:90,y:90}]}));
          renderAreaElementsEditor();
          const el = document.getElementById('areaElementsEditor');
          const style = getComputedStyle(el);
          return {overflowX: style.overflowX, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth};
        })()""")
        ck("the separate-areas list can scroll left/right", scrollable["overflowX"] in ("auto", "scroll"),
           json.dumps(scrollable))

        # ── submit pre-validation names the real limit ──────────────────────────────────
        msg = await page.evaluate("""(() => {
          const seen = [];
          const realToast = window.toast;
          window.toast = (m, kind) => seen.push(String(m));
          areaElements = []; activeAreaElement = null; traceRegions = [];
          poly = Array.from({length: 501}, (_, i) => ({x: i, y: i}));
          cutoutPolygons = []; userChannels = [];
          try { submitDecision("adjust"); } catch (e) {}
          window.toast = realToast;
          return seen;
        })()""")
        await page.wait_for_timeout(400)
        await page.screenshot(path=f"{OUT}/03_after_ui_checks.png")
        ck("the 500-vertex limit is named, not reported as a fifty-polygon error",
           any("500" in m and "points" in m for m in msg), json.dumps(msg[-2:]))
        ck("no uncaught page errors during the whole pass", not errors, "; ".join(errors[:3]))

        # ── Aryan's own test, on the sheet he tested (5 Sep) ────────────────────────────
        # Indurent Park has three unit yards in one tint. The pipeline hands each its own
        # polygon in yard_regions, but the zone carries no geometry, so the canvas fell back
        # to the single top-level polygon: ONE 2,520 m2 strip under a 6,510 m2 headline.
        # That is what "the patterns are not getting recognised for the area that needs to be
        # calculated" looks like from the assessor's chair. Drive it in the browser.
        jid2 = upload(INDURENT, "Indurent Park QA", "QA-IND", "Indurent")
        job2 = wait_done(jid2)
        print("indurent ->", job2.get("measurement_state"), job2.get("area_m2"))
        page2 = await ctx.new_page()
        errors2 = []
        page2.on("pageerror", lambda e: errors2.append(str(e)))
        page2.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page2.goto(f"{BASE}/portal?job={jid2}", wait_until="networkidle", timeout=60000)
        await page2.wait_for_timeout(6000)
        await page2.screenshot(path=f"{OUT}/04_indurent_canvas.png")

        # Aryan's sheet, on the screen he would look at. It reported 6,510 m2 across three
        # regions that were the P2 CAR PARKING; it then refused; it is now measured from the
        # engineer's own CAD layer. Three yards must be drawn as three yards, and the two
        # things the assessor cannot see for himself — how far the outline bridged, and that
        # the figure is a floor — must be on the screen, not only in the JSON.
        ind_ui = await page2.evaluate("""(() => ({
          regions: aiRegions.map(r => ({pts:r.points.length, area:r.area_m2})),
          headline: document.getElementById('areaDisplay').textContent.trim(),
          state: currentJob.measurement_state || (currentJob.result||{}).measurement_state,
          body: document.body.innerText
        }))()""")
        ck("indurent: three service yards are drawn as three outlines, not one",
           len(ind_ui["regions"]) == 3, f"{len(ind_ui['regions'])} outlines")
        ck("indurent: the outlines carry the total the headline is made of",
           abs(sum(r["area"] or 0 for r in ind_ui["regions"]) - (job2.get("area_m2") or 0)) < 1.0,
           f"{sum(r['area'] or 0 for r in ind_ui['regions']):,.1f} vs job {job2.get('area_m2')}")
        # Not "the words exist somewhere on the page" — the flags panel sits ~1,200 px down a
        # 950 px screen, which is exactly where the surface-refusal reason used to hide. The
        # caveat has to be in the viewport, next to the number it qualifies.
        caveat = await page2.evaluate("""(() => {
          const el = document.getElementById('refusalBanner');
          if (!el || el.hidden) return {shown:false};
          const r = el.getBoundingClientRect();
          return {shown:true, text: el.innerText.trim(),
                  inViewport: r.top >= 0 && r.top < window.innerHeight};
        })()""")
        ck("indurent: the minimum-area caveat is ON SCREEN, beside the headline",
           caveat.get("shown") and caveat.get("inViewport"), json.dumps(caveat)[:300])
        ck("indurent: it says the area is a MINIMUM and to treat it as a floor",
           "MINIMUM" in (caveat.get("text") or "")
           and "floor" in (caveat.get("text") or ""),
           (caveat.get("text") or "")[:200])
        ck("indurent: it says how wide a GAP the outline was closed across, in metres",
           re.search(r"closed across gaps of up to [\d.]+ m of blank paper",
                     caveat.get("text") or "") is not None,
           (caveat.get("text") or "")[:200])
        ck("indurent: it names the CAD layer rather than implying a colour was matched",
           "CAD layer" in (caveat.get("text") or "")
           and "Service Yard" in (caveat.get("text") or ""),
           (caveat.get("text") or "")[:200])
        ck("indurent: measured but not approvable — no scale bar on the sheet",
           ind_ui["state"] == "MEASURED_UNVERIFIED", str(ind_ui["state"]))

        # THE CHECK THAT WAS MISSING. The marked-PDF export was made available before
        # approval, the route returned 200 and the builder was tested — and the link stayed
        # invisible in the browser, because renderDecision() returns early for a job with no
        # decision and hid it on the way out. Server-side tests all passed; Aryan opened the
        # portal and correctly reported that nothing had changed. Assert the LINK, in the DOM,
        # on an undecided job, and that it actually serves a PDF.
        mk = await page2.evaluate("""(() => {
          const box = document.getElementById('markedPdfLinks');
          const a = document.getElementById('linkMarkedPdf');
          const vis = box && getComputedStyle(box).display !== 'none';
          return {shown: !!vis, href: a ? a.getAttribute('href') : null,
                  text: a ? a.textContent.trim() : null,
                  decision: currentJob.decision || null};
        })()""")
        ck("indurent: the marked-up drawing is offered BEFORE any decision, in the browser",
           mk.get("shown") and not mk.get("decision"), json.dumps(mk)[:220])
        ck("...and it is labelled as a check, not as an approved issue document",
           "Check the measurement" in (mk.get("text") or "")
           and "not approved" in (mk.get("text") or "").lower(),
           str(mk.get("text")))
        _mk_resp = urllib.request.urlopen(f"{BASE}/marked-pdf/{jid2}.pdf")
        _mk_bytes = _mk_resp.read()
        ck("...and that link really returns the stamped PDF, not a 409",
           _mk_resp.status == 200 and _mk_bytes[:4] == b"%PDF"
           and "AI_CHECK_UNAPPROVED" in (
               _mk_resp.headers.get("Content-Disposition") or ""),
           f"{_mk_resp.status} {len(_mk_bytes)}B {_mk_resp.headers.get('Content-Disposition')}")

        # These checks used to run on Indurent when it reported the car park. The BEHAVIOUR
        # they prove still matters, so they run on project 8, which is genuinely multi-part
        # and carries a ring. Expectations are derived from the job, never typed in.
        ind = await page.evaluate("""(() => ({
          regions: aiRegions.map(r => ({cat:r.category, pts:r.points.length, area:r.area_m2,
                                        holes:r.holes.length})),
          headline: document.getElementById('areaDisplay').textContent.trim(),
          zones: (currentJob.result.zones || []).map(z => [z.category, z.area_m2])
        }))()""")
        ck("every measured part is outlined on the canvas, not just the primary one",
           len(ind["regions"]) >= 5, f"{len(ind['regions'])} outlines")
        ck("the outlines carry the same total the headline is made of",
           abs(sum(r["area"] or 0 for r in ind["regions"])
               - sum(a for _, a in ind["zones"])) < 1.0,
           f"{sum(r['area'] or 0 for r in ind['regions']):,.1f} vs headline {ind['headline']}")

        # Pixel proof: the tint over a NON-PRIMARY part is put there by that part and nothing
        # else. Sample it, redraw with only the primary, sample again — the pixel must change.
        probe = await page.evaluate("""(() => {
          if (aiRegions.length < 2) return {ok:false, why:"fewer than two regions"};
          const r = aiRegions[1];
          const path = new Path2D();
          r.points.forEach((p,i) => i ? path.lineTo(p[0],p[1]) : path.moveTo(p[0],p[1]));
          path.closePath();
          const xs = r.points.map(p=>p[0]), ys = r.points.map(p=>p[1]);
          let hit = null;
          for (let gx = 0; gx < 40 && !hit; gx++) for (let gy = 0; gy < 40 && !hit; gy++) {
            const x = Math.min(...xs) + (Math.max(...xs)-Math.min(...xs)) * (gx+0.5)/40;
            const y = Math.min(...ys) + (Math.max(...ys)-Math.min(...ys)) * (gy+0.5)/40;
            if (ctx.isPointInPath(path, x, y)) hit = [Math.round(x), Math.round(y)];
          }
          if (!hit) return {ok:false, why:"no interior point found"};
          const px = () => Array.from(ctx.getImageData(hit[0], hit[1], 1, 1).data);
          const withAll = px();
          const keep = aiRegions;
          aiRegions = [keep[0]]; draw();
          const primaryOnly = px();
          aiRegions = keep; draw();
          return {ok:true, hit, withAll, primaryOnly};
        })()""")
        ck("a non-primary measured part is actually painted on the canvas (pixel proof)",
           probe.get("ok") and probe["withAll"] != probe["primaryOnly"], json.dumps(probe))

        # The unedited-submit guard: loading the AI outlines and submitting them untouched must be
        # refused when that would move the number. Its only NATURAL subject was Indurent's 30%
        # loose outline (7,401 enclosed vs 6,510 measured), which is gone with the sheet — on
        # project 8 the loaded outlines sit 0.5% from their measurement, inside the tolerance, so
        # nothing fires. Drive the guard directly instead, and say plainly that this is synthetic:
        # the loose-outline DEFECT is no longer covered by any check, only the guard against it.
        guard = await page.evaluate("""(() => {
          const seen = [];
          const realToast = window.toast;
          window.toast = (m, kind) => seen.push(String(m));
          document.getElementById('btnLoad').click();
          aiLoadTotalM2 = calcArea() * 0.5;      // pretend the outlines enclose twice the measurement
          try { submitDecision('adjust'); } catch (e) { seen.push('THREW ' + e); }
          window.toast = realToast;
          return seen;
        })()""")
        ck("an unedited submit that would move the number is refused, naming both figures "
           "(synthetic: the real loose outline left with the Indurent sheet)",
           any("unedited" in m and "against its" in m for m in guard), json.dumps(guard[-1:]))

        # ── the three sheets we refuse: is the REASON on the screen? ────────────────────
        # All three of Inderjit's other sheets end UNMEASURED, which is the right answer for
        # them. But the reason lived only in the flag list at y~1200-1330 on a 950 px screen —
        # below the fold. What he saw was an empty canvas, "No polygon traced yet", and no
        # explanation: "I haven't got any response at all". The contract says a refusal is
        # visible; visible means in the viewport.
        for tag, path in REFUSED_SHEETS:
            jid3 = upload(path, f"Inderjit {tag} QA", "QA-091", "Indurent")
            job3 = wait_done(jid3)
            pg = await ctx.new_page()
            errs3 = []
            pg.on("pageerror", lambda e: errs3.append(str(e)))
            await pg.goto(f"{BASE}/portal?job={jid3}", wait_until="networkidle", timeout=90000)
            await pg.wait_for_timeout(5000)
            await pg.screenshot(path=f"{OUT}/05_refused_{tag}.png")
            seen = await pg.evaluate("""(() => {
              const el = document.getElementById('refusalBanner');
              if (!el || el.hidden) return {shown:false};
              const r = el.getBoundingClientRect();
              return {shown:true, text: el.innerText.trim(),
                      inViewport: r.top >= 0 && r.top < window.innerHeight,
                      state: (currentJob.measurement_state || (currentJob.result||{}).measurement_state)};
            })()""")
            ck(f"{tag}: the reason we did not measure is ON SCREEN, not below the fold",
               seen.get("shown") and seen.get("inViewport"), json.dumps(seen)[:400])
            # Whatever the reason, the banner must SAY it. A refusal flag once shipped
            # without an entry in the portal's REFUSAL_REASONS table, so the banner fell
            # through to "see the flags below" and the reason rendered off-screen — the
            # very defect the banner exists to prevent.
            ck(f"{tag}: the banner NAMES the reason rather than pointing below the fold",
               "see the flags below" not in (seen.get("text") or "").lower()
               and len((seen.get("text") or "")) > 80,
               (seen.get("text") or "")[:180])
            ck(f"{tag}: it says what to do next, in the assessor's words",
               "Calibrate" in (seen.get("text") or "") and "Trace" in (seen.get("text") or ""),
               (seen.get("text") or "")[:200])
            ck(f"{tag}: still UNMEASURED — the banner explains the refusal, it does not undo it",
               seen.get("state") == "UNMEASURED", str(seen.get("state")))
            ck(f"{tag}: no uncaught page errors", not errs3, "; ".join(errs3[:2]))
            refused_jid = jid3
            await pg.close()

        # ── 2105: candidates must be VISIBLE and OUT of the total ───────────────────────
        # The client's design, 9 Sep 2026: extra stipple areas "appear separately as
        # candidate areas... don't automatically include them in the final total, but also
        # don't reject the whole sheet because of them." Both halves are checked HERE, in a
        # real browser, because the last portal feature I shipped was correct server-side and
        # invisible on screen, and Aryan was the one who found that.
        jid2105 = upload(SPEC2105, "Inderjit 2105 QA", "QA-091", "Indurent")
        job2105 = wait_done(jid2105)
        pg5 = await ctx.new_page()
        errs5 = []
        pg5.on("pageerror", lambda e: errs5.append(str(e)))
        await pg5.goto(f"{BASE}/portal?job={jid2105}", wait_until="networkidle", timeout=90000)
        await pg5.wait_for_timeout(6000)
        await pg5.screenshot(path=f"{OUT}/09_2105_candidates.png", full_page=True)
        st = await pg5.evaluate("""(() => {
          const res = (currentJob && currentJob.result) || currentJob || {};
          const regions = (currentJob.yard_regions || res.yard_regions || []);
          const drawn = (typeof aiRegions !== 'undefined' ? aiRegions : []).map(r => ({
            label: r.label, cand: !!r.candidate, area: r.area_m2}));
          const toggles = Array.from(document.querySelectorAll('.yard-region-toggle'))
            .map(t => ({id: t.dataset.regionId, checked: t.checked}));
          const bodyText = document.body.innerText;
          return {
            state: res.measurement_state, area: res.area_m2,
            regions: regions.map(r => ({id: r.region_id, a: r.area_m2,
                                        inc: r.included, cand: !!r.candidate})),
            drawn, toggles,
            saysCandidate: /CANDIDATE/i.test(bodyText),
            saysNotInTotal: /NOT IN TOTAL/i.test(bodyText),
          };
        })()""")
        cands = [r for r in st.get("regions", []) if r.get("cand")]
        mains = [r for r in st.get("regions", []) if not r.get("cand")]
        ck("2105: it measures now, from the one stipple in its own legend",
           st.get("state") == "MEASURED_UNVERIFIED" and (st.get("area") or 0) > 0,
           json.dumps({k: st.get(k) for k in ("state", "area")}))
        ck("2105: the headline is the MAIN regions only — no candidate is in the number",
           abs((st.get("area") or 0) - sum(r["a"] for r in mains)) < 1.0,
           f"headline {st.get('area')} vs mains {sum(r['a'] for r in mains) if mains else None}")
        ck("2105: the candidate area is NOT added to the total",
           all(abs((st.get("area") or 0) - (sum(r["a"] for r in mains) + c["a"])) > 1.0
               for c in cands) if cands else False,
           json.dumps(st.get("regions"))[:220])
        # The whole point of a candidate is that a human can look at it. An excluded region
        # is not drawn; a candidate MUST be, or there is nothing to review.
        ck("2105: the candidate is DRAWN on the canvas, not hidden like an excluded region",
           any(d.get("cand") for d in st.get("drawn", [])),
           json.dumps(st.get("drawn"))[:220])
        ck("2105: ...and the screen says CANDIDATE and NOT IN TOTAL in so many words",
           st.get("saysCandidate") and st.get("saysNotInTotal"),
           json.dumps({k: st.get(k) for k in ("saysCandidate", "saysNotInTotal")}))
        ck("2105: the candidate's checkbox starts UNCHECKED — opt in, never opt out",
           bool(st.get("toggles")) and any(not t["checked"] for t in st.get("toggles", [])),
           json.dumps(st.get("toggles")))
        ck("2105: approval is blocked until every offered area has been ruled on",
           bool(job2105.get("yard_region_review_required")
                or (job2105.get("result") or {}).get("yard_region_review_required")),
           str(job2105.get("yard_region_review_required")))
        ck("2105: no uncaught page errors", not errs5, "; ".join(errs5[:2]))

        # Now walk the path Aryan will walk FIRST: tick the offered area, save, and look at
        # what the screen says afterwards. Everything above only proved the opening state.
        # An included candidate is measured ground -- if the outline and the row still said
        # "NOT IN TOTAL" while the headline had risen, the label would contradict the number.
        _before = st.get("area") or 0
        _cand_ids = [r["id"] for r in st.get("regions", []) if r.get("cand")]
        for _tid in _cand_ids:
            await pg5.click(f'.yard-region-toggle[data-region-id="{_tid}"]')
        await pg5.evaluate("saveYardRegionReview()")
        await pg5.wait_for_timeout(6000)
        await pg5.screenshot(path=f"{OUT}/10_2105_candidate_included.png", full_page=True)
        st2 = await pg5.evaluate("""(() => {
          const res = (currentJob && currentJob.result) || currentJob || {};
          const regions = (currentJob.yard_regions || res.yard_regions || []);
          const drawn = (typeof aiRegions !== 'undefined' ? aiRegions : []).map(r => ({
            label: r.label, cand: !!r.candidate, area: r.area_m2}));
          return {
            area: res.area_m2, drawn,
            regions: regions.map(r => ({id: r.region_id, a: r.area_m2,
                                        inc: r.included, cand: !!r.candidate})),
            reviewRequired: !!(currentJob.yard_region_review_required
                               || res.yard_region_review_required),
            saysNotInTotal: /NOT IN TOTAL/i.test(document.body.innerText),
          };
        })()""")
        _cand_area = sum(r["a"] for r in st.get("regions", []) if r.get("cand"))
        ck("2105: ticking the offered area adds exactly its own m² to the headline",
           abs((st2.get("area") or 0) - (_before + _cand_area)) < 1.0,
           f"{_before} + {_cand_area} -> {st2.get('area')}")
        ck("2105: ...and the endpoint keeps `candidate` on it, so its provenance survives",
           all(r["cand"] and r["inc"] for r in st2.get("regions", []) if r["id"] in _cand_ids),
           json.dumps(st2.get("regions"))[:220])
        ck("2105: ...the outline stops being drawn as an offer once it is IN the total",
           bool(st2.get("drawn")) and not any(d.get("cand") for d in st2.get("drawn", [])),
           json.dumps(st2.get("drawn"))[:220])
        ck("2105: ...no label anywhere still says NOT IN TOTAL while the total includes it",
           not st2.get("saysNotInTotal"), str(st2.get("saysNotInTotal")))
        # It clears the REGION-REVIEW block and nothing else: the sheet is still
        # MEASURED_UNVERIFIED, so Approve stays blocked behind Confirm scale + extent, the
        # same route as Indurent and South Mimms. Saying "Save unblocks Approve" would have
        # been a sentence the assessor could disprove with one click.
        ck("2105: ...saving the review clears the region-review block (scale+extent still required)",
           st2.get("reviewRequired") is False, str(st2.get("reviewRequired")))
        ck("2105: no uncaught page errors through the include round trip",
           not errs5, "; ".join(errs5[:2]))
        await pg5.close()

        # ── LDSS2: a REFUSAL that still hands over exact geometry ───────────────────────
        import os as _os_ldss
        if not _os_ldss.path.exists(LDSS2):
            print("  [SKIP] LDSS2 not present")
        else:
            jidL = upload(LDSS2, "Skanska Equinix QA", "QA-LDSS2", "Skanska")
            jobL = wait_done(jidL)
            pgL = await ctx.new_page()
            errsL = []
            pgL.on("pageerror", lambda e: errsL.append(str(e)))
            await pgL.goto(f"{BASE}/portal?job={jidL}", wait_until="networkidle", timeout=90000)
            await pgL.wait_for_timeout(6000)
            await pgL.screenshot(path=f"{OUT}/11_ldss2_boundaries.png", full_page=True)
            stL = await pgL.evaluate("""(() => {
              const res = (currentJob && currentJob.result) || currentJob || {};
              const regions = (currentJob.yard_regions || res.yard_regions || []);
              const drawn = (typeof aiRegions !== 'undefined' ? aiRegions : []).map(r => ({
                label: r.label, cand: !!r.candidate, area: r.area_m2}));
              const toggles = Array.from(document.querySelectorAll('.yard-region-toggle'))
                .map(t => ({id: t.dataset.regionId, checked: t.checked}));
              const body = document.body.innerText;
              return {state: res.measurement_state, area: res.area_m2,
                      regions: regions.map(r => ({id: r.region_id, a: r.area_m2,
                                                  inc: r.included, cand: !!r.candidate})),
                      drawn, toggles,
                      saysOffered: /EXACT BOUNDARIES OFFERED/i.test(body),
                      namesLayer: /Onsite_Dock_\(Rigid\)/i.test(body)};
            })()""")
            ck("LDSS2: still refuses — no number is invented from a boundary nobody chose",
               stL.get("state") == "UNMEASURED" and not stL.get("area"),
               f"state={stL.get('state')} area={stL.get('area')}")
            ck("LDSS2: ...but the four exact boundaries reach the SCREEN, not just the flags",
               len(stL.get("regions") or []) >= 3,
               json.dumps(stL.get("regions"))[:200])
            ck("LDSS2: ...every one of them is OUT of the total until a human includes it",
               bool(stL.get("regions"))
               and all(r["inc"] is False and r["cand"] for r in stL["regions"]),
               json.dumps(stL.get("regions"))[:200])
            ck("LDSS2: ...they are DRAWN on the canvas, so the assessor can see the shape",
               any(d.get("cand") for d in stL.get("drawn", [])),
               json.dumps(stL.get("drawn"))[:200])
            ck("LDSS2: ...and each is named by the layer it came from",
               stL.get("namesLayer"), str(stL.get("namesLayer")))
            ck("LDSS2: ...with every checkbox starting UNCHECKED",
               bool(stL.get("toggles"))
               and all(not t["checked"] for t in stL.get("toggles", [])),
               json.dumps(stL.get("toggles")))
            # "Correct but below the fold" is the defect the refusal banner was built to fix.
            # On a sheet whose ONLY content is four offered outlines, the list of them is the
            # primary thing on the page -- if the assessor has to scroll to discover it exists,
            # the offer may as well not have been made.
            posL = await pgL.evaluate("""(() => {
              const t = document.querySelector('.yard-region-toggle');
              const box = t ? t.closest('div[style*="border"]') : null;
              const el = box || t;
              if (!el) return {found: false};
              const r = el.getBoundingClientRect();
              return {found: true, top: Math.round(r.top + window.scrollY),
                      viewport: window.innerHeight,
                      inView: (r.top + window.scrollY) < window.innerHeight,
                      bannerSaysWhere: /MEASURED REGION REVIEW/i.test(
                        (document.getElementById('refusalBanner') || {}).innerText || ''),
                      bannerListsOffers: /On offer, none counted/i.test(
                        (document.getElementById('refusalBanner') || {}).innerText || '')};
            })()""")
            # The first version of this check passed on `inView OR the banner names the box`,
            # and the data showed top=1124 on a 950 px viewport -- it was a test written so the
            # defect could pass. What matters is that the assessor SEES WHAT IS ON OFFER without
            # scrolling, not that something points down the page at it.
            ck("LDSS2: ...and what is on offer is visible without scrolling, areas and all",
               posL.get("found") and (posL.get("inView") or posL.get("bannerListsOffers")),
               json.dumps(posL))
            ck("LDSS2: ...the banner tells the assessor to TICK one, not to trace what we just gave them",
               posL.get("bannerSaysWhere"), json.dumps(posL))
            # The path he will walk FIRST: tick the one that is his, save. This starts from
            # UNMEASURED with NO scale_k, which the review endpoint was never built for --
            # it recomputes an area from the kept region and says nothing about the state.
            await pgL.click('.yard-region-toggle[data-region-id="boundary-2"]')
            await pgL.evaluate("saveYardRegionReview()")
            await pgL.wait_for_timeout(6000)
            await pgL.screenshot(path=f"{OUT}/12_ldss2_included.png", full_page=True)
            st2L = await pgL.evaluate("""(() => {
              const res = (currentJob && currentJob.result) || currentJob || {};
              const el = document.getElementById('refusalBanner');
              const body = document.body.innerText;
              return {area: res.area_m2, state: res.measurement_state,
                      bannerHidden: !el || el.hidden,
                      bannerText: (el && el.innerText || '').slice(0, 120),
                      saysNoMeasurement: /No AI measurement on this sheet/i.test(body),
                      costing: !!(res.costing || currentJob.costing),
                      price: res.price_gbp || currentJob.price_gbp || null,
                      readout: (document.querySelector('.readout') || {}).innerText || ''};
            })()""")
            ck("LDSS2 include: ticking his outline gives exactly its own area",
               abs((st2L.get("area") or 0) - 1114.0) < 1.0, json.dumps(st2L)[:200])
            # A headline number under a banner reading "No AI measurement on this sheet" is
            # the dropped-NOT contradiction again: the screen asserting the opposite of the
            # figure beside it, on the first click the assessor makes.
            ck("LDSS2 include: ...and the screen STOPS saying there is no measurement",
               not st2L.get("saysNoMeasurement"),
               f"state={st2L.get('state')} area={st2L.get('area')} banner={st2L.get('bannerText')!r}")
            ck("LDSS2 include: ...the state is no longer UNMEASURED once a human identified it",
               st2L.get("state") != "UNMEASURED", str(st2L.get("state")))
            # My first version of this check probed `price_gbp`, a field that exists NOWHERE
            # in the codebase, so it could only ever pass -- the same self-congratulating test
            # as the below-the-fold one. The contract does not forbid a price here; it forbids
            # an ungated one. Assert the GATE, and assert it server-side rather than by
            # reading a colour off the screen.
            blockL = await pgL.evaluate("""(async (jid) => {
              const r = await fetch('/approve/' + encodeURIComponent(jid),
                                    {method: 'POST'});
              let body = null; try { body = await r.json(); } catch (e) { body = null; }
              return {status: r.status, error: (body && (body.error || body.reason)) || ''};
            })""", jidL)
            ck("LDSS2 include: ...and approval is still BLOCKED, because scale is unverified",
               blockL.get("status") != 200 and re.search(
                   r"scale", str(blockL.get("error") or ""), re.I) is not None,
               json.dumps(blockL)[:220])
            ck("LDSS2 include: ...and the block names MEASURED_UNVERIFIED, not UNMEASURED",
               "MEASURED_UNVERIFIED" in str(blockL.get("error") or ""),
               json.dumps(blockL)[:220])
            # ...and now WALK the only route out. A MEASURED_UNVERIFIED job whose Confirm
            # button is hidden is a dead end: approve 409s by design and nothing on screen
            # clears it. The button is gated on `existingScale > 0`, and this sheet reached
            # the portal with scale_k=None until the pipeline was taught to carry it.
            await pgL.reload(); await pgL.wait_for_timeout(2500)
            await pgL.evaluate("selectJob(%r)" % jidL); await pgL.wait_for_timeout(2500)
            confVis = await pgL.evaluate("""(() => {
              const b = document.getElementById('btnConfirmExisting');
              const res = (currentJob && currentJob.result) || currentJob || {};
              return {shown: !!b && b.style.display !== 'none' && b.offsetParent !== null,
                      scale_k: res.scale_k || currentJob.scale_k || null,
                      state: res.measurement_state || currentJob.measurement_state};
            })()""")
            ck("LDSS2 confirm: the way out of the block is VISIBLE, not a dead end",
               confVis.get("shown"), json.dumps(confVis)[:200])
            import urllib.request as _u0, fitz as _fitz0
            _chk = _fitz0.open(stream=_u0.urlopen(f"{BASE}/marked-pdf/{jidL}.pdf").read(),
                               filetype="pdf")
            _chk_txt = "\n".join(pg.get_text() for pg in _chk)
            ck("LDSS2 export: the pre-decision CHECK sheet DOES show what was declined",
               "CANDIDATE" in _chk_txt.upper(),
               str("CANDIDATE" in _chk_txt.upper()))
            _chk.close()
            await pgL.evaluate("confirmExistingMeasurement()")
            await pgL.wait_for_timeout(4000)
            await pgL.screenshot(path=f"{OUT}/13_ldss2_confirmed.png", full_page=True)
            afterL = await pgL.evaluate("""(async (jid) => {
              const j = await (await fetch('/jobs')).json();
              const job = j[jid] || {};
              const r = await fetch('/approve/' + encodeURIComponent(jid), {method: 'POST'});
              let b = null; try { b = await r.json(); } catch (e) {}
              return {confirmed: !!job.scale_confirmed, area: job.area_m2,
                      state: job.measurement_state,
                      approve: r.status, err: (b && b.error) || ''};
            })""", jidL)
            ck("LDSS2 confirm: confirming scale+extent actually records it",
               afterL.get("confirmed") is True, json.dumps(afterL)[:220])
            ck("LDSS2 confirm: ...and the job can THEN be approved — the path completes",
               afterL.get("approve") == 200, json.dumps(afterL)[:220])
            ck("LDSS2 confirm: ...with his 1,114 m² intact through the whole round trip",
               abs((afterL.get("area") or 0) - 1114.0) < 1.0, str(afterL.get("area")))
            # The document he actually downloads. Before approval it is a CHECK sheet and
            # must show every offer; after approval it is the ISSUED markup and must show
            # only what the quantity contains. Same builder, two jobs -- which is how the
            # declined outlines ended up on his approved LDSS2 drawing.
            import urllib.request as _u, fitz as _fitz
            _pdf_url = f"{BASE}/marked-pdf/{jidL}.pdf"
            _issued_bytes = _u.urlopen(_pdf_url).read()
            _doc = _fitz.open(stream=_issued_bytes, filetype="pdf")
            _txt = "\n".join(pg.get_text() for pg in _doc)
            ck("LDSS2 export: the APPROVED drawing carries no declined candidate",
               "CANDIDATE" not in _txt.upper(),
               [ln for ln in _txt.splitlines() if "CANDIDATE" in ln.upper()][:2])
            ck("LDSS2 export: ...and it does carry the 1,114 m² he approved",
               "1,114" in _txt or "1114" in _txt.replace(",", ""),
               _txt[:120].replace("\n", " "))
            ck("LDSS2 export: ...and is no longer stamped NOT APPROVED",
               "NOT APPROVED" not in _txt.upper(),
               [ln for ln in _txt.splitlines() if "APPROVED" in ln.upper()][:2])
            _doc.close()
            ck("LDSS2: no uncaught page errors", not errsL, "; ".join(errsL[:2]))
            await pgL.close()

        # A measured job must NOT carry the banner: it would tell the assessor there is no
        # measurement while the headline shows one. Checked on project 8 — Indurent is a
        # refused sheet now and would trivially pass the wrong way round.
        await page.reload(wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        hidden = await page.evaluate("(() => { const el = document.getElementById('refusalBanner'); return !el || el.hidden; })()")
        ck("a measured sheet shows no refusal banner", hidden, str(hidden))

        # The Indurent Load-AI checks lived here: three yards loaded, the 30%-loose outline named,
        # an unedited submit refused at 7,401 vs 6,510. All three had the same subject — a sheet
        # whose "three yards" were the P2 car parking. It refuses now, so the checks had no
        # subject and were removed rather than weakened. What survives them: the ring test below
        # (Load AI on a multi-part sheet), the pixel proof above, and a synthetic drive of the
        # unedited-submit guard. What does NOT survive: any check that a tint-path outline
        # actually traces its own quantity tightly. That defect is real and is now uncovered.

        # A RING-shaped surface is REFUSED by Load AI polygon, and says so. The editor holds one
        # closed outline per region with no hole, and calcArea SUMS regions rather than unioning
        # them, so there is no honest way to load a ring: loading its hole as a cut-out made the
        # road cancel the yard inside it (38,527 m2 for a 58,411 m2 sheet), and loading the ring
        # without the hole double-counted that yard (90,236 m2). Both measured on project 8.
        ring = await page.evaluate("""(() => {
          const seen = [];
          const realToast = window.toast;
          window.toast = (m, kind) => seen.push(String(m));
          document.getElementById('btnLoad').click();
          window.toast = realToast;
          return {regions: traceRegionEntries().length, area: calcArea(), toasts: seen,
                  aiRegions: aiRegions.length,
                  rings: aiRegions.filter(r => r.holes.length).length,
                  loadedStated: aiRegions.filter(r => !r.holes.length)
                                         .reduce((sum, r) => sum + (r.area_m2 || 0), 0),
                  cutouts: cutoutPolygons.filter(c => c.fromAiRegion).length};
        })()""")
        ck("the ring-shaped road is NOT loaded into an editor that cannot hold its hole",
           ring["rings"] >= 1 and ring["regions"] == ring["aiRegions"] - ring["rings"]
           and ring["cutouts"] == 0, json.dumps({k: ring[k] for k in ("regions","aiRegions","rings","cutouts")}))
        ck("...and it names the surface it refused and where the area still is",
           any("ring-shaped" in t and "road" in t and "Confirm scale + extent" in t for t in ring["toasts"]),
           json.dumps(ring["toasts"]))
        # The reference is the sum of the parts that WERE loaded (three yard parts plus the
        # road's second, hole-free component) - NOT the yard zone total, and not the whole
        # sheet. If this drifts, either a ring leaked in or a surface is being double-counted.
        ck("what IS loaded adds up to the surfaces it came from, with nothing double-counted",
           ring["area"] is not None and ring["loadedStated"] > 0
           and abs(ring["area"] - ring["loadedStated"]) / ring["loadedStated"] < 0.03,
           f"{ring['area']:.1f} enclosed vs {ring['loadedStated']:.1f} stated across the loaded parts")

        # ── a job still being measured must not wear the last job's numbers ────────────
        jid4 = upload(INDURENT, "Processing QA", "QA-PROC", "Indurent")
        pg = await ctx.new_page()
        pg.on("pageerror", lambda e: errors2.append(str(e)))
        await pg.goto(f"{BASE}/portal?job={jid4}", wait_until="networkidle", timeout=60000)
        await pg.wait_for_timeout(3000)
        proc = await pg.evaluate("""(() => ({
          status: currentJob.status,
          area: document.getElementById('areaDisplay').textContent.trim(),
          readout: document.getElementById('readout').textContent.trim(),
          perimeter: document.getElementById('perimeterDisplay').textContent.trim(),
          scale: document.getElementById('scaleDisplay').textContent.trim(),
          ratio: document.getElementById('scaleRatioDisplay').textContent.trim(),
          costing: document.getElementById('costingBlock').innerText.trim(),
          zoomControls: !!document.getElementById('zoomControls')
        }))()""")
        if proc["status"] == "processing":
            ck("a job still being measured shows no area at all, not the last job's",
               proc["area"] == "\u2014" and "\u2014" in proc["readout"], json.dumps(proc))
            # Then open a measured job: the zoom controls must have survived. innerHTML='' on
            # canvasWrap used to delete them for the rest of the session.
            await pg.goto(f"{BASE}/portal?job={jid}", wait_until="networkidle", timeout=60000)
            await pg.wait_for_timeout(5000)
            zoom = await pg.evaluate("""(() => ({
              controls: !!document.getElementById('zoomControls'),
              zoomIn: !!document.getElementById('btnZoomIn'),
              empty: (document.getElementById('emptyState')||{}).innerText || ''
            }))()""")
            ck("opening a processing job does not delete the zoom controls for the session",
               zoom["controls"] and zoom["zoomIn"], json.dumps(zoom))
            ck("a processing job shows no scale, perimeter or price from the last job either",
               all(v == "\u2014" for v in (proc.get("perimeter"), proc.get("scale"), proc.get("ratio")))
               and "£" not in (proc.get("costing") or ""), json.dumps(proc))
            ck("...and the empty state gets its own words back",
               "Takeoff running" not in zoom["empty"], zoom["empty"][:80])
        else:
            ck("a job still being measured shows no area at all, not the last job's",
               False, f"could not observe a processing job (status={proc['status']})")
        await pg.close()

        # ── a REJECTED sheet must not be told to go and measure itself ─────────────────
        pg = await ctx.new_page()
        pg.on("pageerror", lambda e: errors2.append(str(e)))
        await pg.goto(f"{BASE}/portal?job={refused_jid}", wait_until="networkidle", timeout=60000)
        await pg.wait_for_timeout(4000)
        await pg.click("#btnReject")
        # The banner is hidden while /snapshot re-renders after the decision - measured at up
        # to 10s on one sheet. Assert the steady state the assessor ends up looking at.
        for _ in range(20):
            await pg.wait_for_timeout(1000)
            settled = await pg.evaluate("(() => { const el = document.getElementById('refusalBanner'); return !!(currentJob && currentJob.decision === 'rejected' && el && !el.hidden); })()")
            if settled:
                break
        rej = await pg.evaluate("""(() => {
          const el = document.getElementById('refusalBanner');
          return {hidden: !el || el.hidden, text: el ? el.innerText.trim() : null,
                  decision: currentJob.decision};
        })()""")
        await pg.screenshot(path=f"{OUT}/07_rejected_banner.png")
        if rej.get("decision") == "rejected":
            ck("a rejected sheet is not told to calibrate and trace itself",
               (rej["text"] or "") and "rejected" in rej["text"].lower()
               and "Calibrate" not in (rej["text"] or ""), json.dumps(rej)[:300])
        else:
            ck("a rejected sheet is not told to calibrate and trace itself", False,
               json.dumps(rej)[:300])
        await pg.close()
        ck("no uncaught page errors across the whole second half", not errors2,
           "; ".join(errors2[:3]))
        await browser.close()

    print(f"\n==== {sum(1 for _,ok,_ in RESULTS if ok)}/{len(RESULTS)} PASS ====")
    return 0 if all(ok for _,ok,_ in RESULTS) else 1

sys.exit(asyncio.run(main()))
