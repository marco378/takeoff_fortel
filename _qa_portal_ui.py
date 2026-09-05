"""QA the portal UI fixes on a REAL Fortel drawing (project-8 hatch sheet), headless.

Isolated jobs file so nothing lands in approval_jobs.json.
"""
import asyncio, os, json, time, uuid, urllib.request, sys

PORT = os.environ.get("QA_PORT", "5111")
BASE = f"http://127.0.0.1:{PORT}"
OUT  = os.environ["QA_OUT"]
P8   = "drawings/inderjit_p8/8_14173-TCG-XX_XX-XX-SK-C-0003_CONCRETE_SLAB_MSA.pdf"
INDURENT = ("drawings/inderjit_p9p10/11_Indurent_Park_Newport_22513-RLL-25-00-DR-C-3151"
            "_P02_Proposed_Pavement_Construction.pdf")
# The other three sheets Inderjit sent on 4 Sep. All three are correctly UNMEASURED; what was
# wrong was that the portal never said so where he could see it.
REFUSED_SHEETS = [
    ("mimms",  "drawings/inderjit_p9p10/12_South_Mimms.pdf"),
    ("roscoe", "drawings/inderjit_p9p10/10_26051-ROS-00-XX-DR-C-05101.pdf"),
    ("spec2105", "drawings/inderjit_p9p10/9_25010-RLL-26-XX-DR-C-2105"
                 "_P01_External_Construction_Specification.pdf"),
]

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

        ind = await page2.evaluate("""(() => ({
          regions: aiRegions.map(r => ({cat:r.category, pts:r.points.length, area:r.area_m2})),
          headline: document.getElementById('areaDisplay').textContent.trim(),
          yard: (currentJob.result.yard_regions || []).map(r => [r.region_id, r.area_m2, r.included])
        }))()""")
        included = [r for r in ind["yard"] if r[2]]
        ck("every included yard region is outlined on the canvas, not just the primary one",
           len(ind["regions"]) == len(included) and len(included) == 3,
           f"{len(ind['regions'])} outlines vs {len(included)} included regions")
        ck("the outlines carry the same areas the headline is made of",
           abs(sum(r["area"] or 0 for r in ind["regions"]) - sum(r[1] for r in included)) < 1.0,
           f"{sum(r['area'] or 0 for r in ind['regions']):,.1f} vs headline {ind['headline']}")
        ck("the region the colour gate excluded is NOT drawn as measured",
           len(ind["yard"]) == 4 and len(ind["regions"]) == 3, json.dumps(ind["yard"]))

        # Pixel proof: the tint over the SECOND yard is put there by that region and nothing
        # else. Sample it, redraw with only the primary, sample again — the pixel must change.
        probe = await page2.evaluate("""(() => {
          if (aiRegions.length < 2) return {ok:false, why:'fewer than two regions'};
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
          if (!hit) return {ok:false, why:'no interior point found'};
          const px = () => Array.from(ctx.getImageData(hit[0], hit[1], 1, 1).data);
          const withAll = px();
          const keep = aiRegions;
          aiRegions = [keep[0]]; draw();
          const primaryOnly = px();
          aiRegions = keep; draw();
          return {ok:true, hit, withAll, primaryOnly};
        })()""")
        ck("the second yard is actually painted on the canvas (pixel proof)",
           probe.get("ok") and probe["withAll"] != probe["primaryOnly"], json.dumps(probe))
        ck("no uncaught page errors on the Indurent sheet", not errors2, "; ".join(errors2[:3]))

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
            ck(f"{tag}: it says what to do next, in the assessor's words",
               "Calibrate" in (seen.get("text") or "") and "Trace" in (seen.get("text") or ""),
               (seen.get("text") or "")[:200])
            ck(f"{tag}: still UNMEASURED — the banner explains the refusal, it does not undo it",
               seen.get("state") == "UNMEASURED", str(seen.get("state")))
            ck(f"{tag}: no uncaught page errors", not errs3, "; ".join(errs3[:2]))
            await pg.close()

        # A measured job must NOT carry the banner: it would tell the assessor there is no
        # measurement while the headline shows one.
        await page2.reload(wait_until="networkidle", timeout=60000)
        await page2.wait_for_timeout(4000)
        hidden = await page2.evaluate("(() => { const el = document.getElementById('refusalBanner'); return !el || el.hidden; })()")
        ck("a measured sheet shows no refusal banner", hidden, str(hidden))
        await browser.close()

    print(f"\n==== {sum(1 for _,ok,_ in RESULTS if ok)}/{len(RESULTS)} PASS ====")
    return 0 if all(ok for _,ok,_ in RESULTS) else 1

sys.exit(asyncio.run(main()))
