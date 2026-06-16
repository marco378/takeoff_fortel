#!/usr/bin/env python3
"""ONE live Claude-vision call (raw HTTP, no SDK): identify the unmarked yard slab, measure, score.
Key from ANTHROPIC_API_KEY env only (never stored)."""
import base64, json, os, urllib.request
import fitz
from shapely.geometry import Polygon

PNG = "grid_Yard.png"
PROMPT = ("You are a concrete-takeoff estimator. This site plan has a red coordinate grid "
          "labelled in PDF points (blue numbers, every 250). Identify the external concrete yard "
          "slab we price: the paved hardstanding around and including the warehouse, the parking "
          "and the loading aprons, bounded by the site boundary/kerbs, EXCLUDING the roundabout and "
          "soft landscaping at the bottom and the landscaping strip on the right. "
          'Reply with ONLY JSON {"vertices":[[x,y],...]} of 15-25 PDF-point coordinates, clockwise.')

b64 = base64.b64encode(open(PNG, "rb").read()).decode()
body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 1200,
    "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
        {"type": "text", "text": PROMPT}]}]}).encode()
req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
    "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
    "content-type": "application/json"})
try:
    resp = json.load(urllib.request.urlopen(req, timeout=60))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read()[:100].decode("utf-8", "ignore")); raise SystemExit

txt = resp["content"][0]["text"]
verts = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])["vertices"]
k = 0.108
poly = Polygon(verts).buffer(0)
area = poly.area * k * k
md = fitz.open("drawings/Yard Area Proposed_Site_Plan.pdf")[0]
tv = [(pt[0], pt[1]) for pt in next(a.vertices for a in md.annots()
      if a.type[1] == "Polygon" and "sq m" in (a.info.get("content", "") or ""))]
true = Polygon(tv).buffer(0)
iou = poly.intersection(true).area / poly.union(true).area
print(f"LIVE Claude vision (sonnet-4-6): {len(verts)} vertices")
print(f"  area={area:,.0f} m2  gold 26,080  err {abs(area-26080)/26080*100:.1f}%  IoU {iou:.2f}")
print(f"  tokens in/out: {resp['usage']['input_tokens']}/{resp['usage']['output_tokens']}")
