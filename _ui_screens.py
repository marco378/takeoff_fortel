"""Capture UI screenshots of estimation being done in the portal (headless)."""
import asyncio, os, json, subprocess, time, urllib.request

BASE = "http://127.0.0.1:5098"
OUT  = "test_screenshots/estimation_flow"

async def run():
    from playwright.async_api import async_playwright
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # 1. upload D77 through the real portal API (estimation happens live)
        import requests_shim  # noqa - not used; keep stdlib
    # (upload done outside browser, below)

async def main():
    from playwright.async_api import async_playwright
    os.makedirs(OUT, exist_ok=True)

    # Upload a fresh D77 job via the API so the portal shows a live estimation
    import mimetypes, uuid, http.client
    boundary = "----ui" + uuid.uuid4().hex
    fields = {"project_name": "Hemington D77 Hard Landscaping", "project_ref": "UI-DEMO-1",
              "client_name": "Winvic Construction"}
    fname = "drawings/_int_d77.pdf"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"_int_d77.pdf\"\r\n"
             f"Content-Type: application/pdf\r\n\r\n").encode()
    body += open(fname, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/upload", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    job = json.load(urllib.request.urlopen(req))
    jid = job["job_id"]
    print("uploaded job", jid, job.get("status"))
    # wait for measurement to finish
    for _ in range(30):
        jobs = json.load(urllib.request.urlopen(f"{BASE}/jobs"))
        st = jobs.get(jid, {}).get("status")
        if st not in ("processing", None):
            break
        time.sleep(2)
    print("job state:", jobs[jid].get("measurement_state"), "area:", jobs[jid].get("area_m2"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        await page.goto(f"{BASE}/portal", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=f"{OUT}/01_job_list_states.png")
        print("01 job list")

        # open the fresh D77 job
        await page.goto(f"{BASE}/portal?job={jid}", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(4000)  # snapshot + polygon draw
        await page.screenshot(path=f"{OUT}/02_d77_measured_detail.png")
        print("02 D77 detail")

        # try to load the AI polygon overlay if the button exists
        for label in ("Load AI polygon", "AI polygon", "Show polygon"):
            btn = page.get_by_text(label, exact=False)
            if await btn.count():
                await btn.first.click()
                await page.wait_for_timeout(2500)
                break
        await page.screenshot(path=f"{OUT}/03_d77_polygon_overlay.png")
        print("03 polygon overlay")

        # click Approve to show the block (unverified) or success dialog
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        for label in ("Approve",):
            btn = page.get_by_text(label, exact=False)
            if await btn.count():
                await btn.first.click()
                await page.wait_for_timeout(2000)
                break
        await page.screenshot(path=f"{OUT}/04_approve_gate.png")
        print("04 approve gate")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
