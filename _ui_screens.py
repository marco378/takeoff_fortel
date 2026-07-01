"""Capture UI screenshots of estimation on REAL Fortel drawings (headless)."""
import asyncio, os, json, time, uuid, urllib.request

BASE = "http://127.0.0.1:5098"
OUT  = "test_screenshots/estimation_flow"

UPLOADS = [
    # (path, project_name, ref, client)
    ("drawings/winvic/Yard_Area_Proposed_Site_Plan.pdf",
     "California Drive External Yard", "WCL-1830", "Winvic Construction"),
    ("drawings/tender_pack/2-Enquiry/01-Tender/Drawings/Proposed_Site_Plan.pdf",
     "California Drive - unmarked site plan", "WCL-1830-U", "Winvic Construction"),
]

def upload(path, name, ref, client):
    boundary = "----ui" + uuid.uuid4().hex
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
    for _ in range(45):
        jobs = json.load(urllib.request.urlopen(f"{BASE}/jobs"))
        if jobs.get(jid, {}).get("status") not in ("processing", None):
            return jobs[jid]
        time.sleep(2)
    return jobs.get(jid, {})

async def main():
    from playwright.async_api import async_playwright
    os.makedirs(OUT, exist_ok=True)

    jids = []
    for path, name, ref, client in UPLOADS:
        jid = upload(path, name, ref, client)
        j = wait_done(jid)
        print(os.path.basename(path), "->", j.get("measurement_state"), j.get("area_m2"))
        jids.append(jid)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

        await page.goto(f"{BASE}/portal", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=f"{OUT}/01_job_list_real.png")
        print("01 job list")

        # real marked Winvic yard (verified, approvable)
        await page.goto(f"{BASE}/portal?job={jids[0]}", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(4500)
        await page.screenshot(path=f"{OUT}/02_winvic_yard_verified.png")
        print("02 winvic yard")

        # real unmarked engineer sheet (UNMEASURED -> assessor trace on real drawing)
        await page.goto(f"{BASE}/portal?job={jids[1]}", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(4500)
        await page.screenshot(path=f"{OUT}/03_unmarked_siteplan_assessor.png")
        print("03 unmarked site plan")

        # approve gate check on the unmeasured job
        btn = page.get_by_text("Approve", exact=False)
        if await btn.count():
            await btn.first.click()
            await page.wait_for_timeout(2000)
        await page.screenshot(path=f"{OUT}/04_approve_gate_real.png")
        print("04 approve gate")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
