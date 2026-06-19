#!/usr/bin/env python3
"""
Minimal Anthropic API client over raw HTTP (urllib) — bypasses the SDK
'SyncHttpxClientWrapper has no attribute _state' bug seen earlier, and needs no pip install.
Reads ANTHROPIC_API_KEY from the environment.

Verified working models on this account (Jun 2026): claude-sonnet-4-6, claude-opus-4-8,
claude-haiku-4-5-20251001.  (Legacy 3.5/3.7 names 404 on this account.)

NOTE: the call must run where the network reaches api.anthropic.com directly. A restrictive
SOCKS proxy (e.g. some sandboxes) can strip the x-api-key header and yield a spurious 401 —
that is an environment issue, not a bad key. Run from an open-network host in that case.
"""
import os, json, base64, urllib.request, urllib.error

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"     # good vision + cheaper than opus
VISION_MODEL  = "claude-sonnet-4-6"


def have_key():
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def call(messages, model=DEFAULT_MODEL, max_tokens=1024, system=None):
    """Low-level messages call. Returns assistant text (or raises)."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        payload["system"] = system
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        d = json.load(r)
        return d["content"][0]["text"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API HTTP {e.code}: {e.read().decode()[:300]}")


def _img_block(png_path):
    b64 = base64.b64encode(open(png_path, "rb").read()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def read_legend(png_path, model=VISION_MODEL):
    """VISION: read the hard-landscaping legend; return the priced concrete area's label + a
    description of its hatch/colour, so colour-segmentation can be anchored. Returns dict."""
    prompt = (
        "This is a construction/landscaping drawing legend + plan. Identify the legend entry for the "
        "PRICED CONCRETE area a groundworks subcontractor would measure — typically 'Concrete Service "
        "Yard construction' (may also be 'external yard', 'Type C', 'GV areas'). Reply ONLY JSON: "
        '{\"label\": \"...\", \"swatch\": \"grey|other\", \"approx_rgb\": [r,g,b], '
        '\"distinct_from\": [\"roads/bituminous\",\"car park\",\"landscaping\"]}')
    txt = call([{"role": "user", "content": [_img_block(png_path), {"type": "text", "text": prompt}]}],
               model=model, max_tokens=400)
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


def confirm_region(overlay_png, area_m2, model=VISION_MODEL):
    """VISION: show the segmented region (red overlay) + the proposed area; ask Claude to confirm it is
    the concrete service yard and nothing obviously wrong. Returns dict {ok, reason}."""
    prompt = (
        f"The RED region is what an AI measured as the concrete service yard ({area_m2:,.0f} m2). "
        "Does the red cover the concrete yard hatch only (NOT the building slab, roads, car park or "
        'landscaping)? Reply ONLY JSON: {\"ok\": true/false, \"reason\": \"short\"}.')
    txt = call([{"role": "user", "content": [_img_block(overlay_png), {"type": "text", "text": prompt}]}],
               model=model, max_tokens=200)
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


if __name__ == "__main__":
    print("key present:", have_key())
    if have_key():
        print(call([{"role": "user", "content": "reply OK"}], max_tokens=6))
