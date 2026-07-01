#!/usr/bin/env python3
"""
robustness_corpus.py

Deterministic, re-runnable adversarial corpus generator for the Fortel
takeoff pipeline. Derives "hostile" variants from the real Winvic tender
PDFs (drawings/winvic/, drawings/_int_d77.pdf, drawings/tender_pack/) into
drawings/corpus/ (gitignored).

This script does NOT modify pipeline code — it only produces test fixtures.
Re-running it wipes and rebuilds drawings/corpus/ so results are reproducible.

Usage:
    .venv/bin/python robustness_corpus.py
"""
import os
import shutil
import zipfile
import email
import email.policy
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText

import fitz  # PyMuPDF

REPO = os.path.dirname(os.path.abspath(__file__))
DRAWINGS = os.path.join(REPO, "drawings")
CORPUS = os.path.join(DRAWINGS, "corpus")

# Source files used to derive the adversarial corpus. Picked because they
# are real, known-good Winvic tender PDFs already in the repo.
SRC_MARKED = os.path.join(DRAWINGS, "winvic", "Yard_Area_Proposed_Site_Plan.pdf")
SRC_UNMARKED = os.path.join(DRAWINGS, "_int_d77.pdf")
SRC_DOCK = os.path.join(DRAWINGS, "winvic", "Dock_Slab_Area_Proposed_Site_Plan.pdf")

TENDER_PACK_DIR = os.path.join(DRAWINGS, "tender_pack")


def log(msg):
    print(f"[corpus] {msg}")


def reset_corpus_dir():
    if os.path.isdir(CORPUS):
        shutil.rmtree(CORPUS)
    os.makedirs(CORPUS, exist_ok=True)
    log(f"reset {CORPUS}")


def require_sources():
    missing = [p for p in (SRC_MARKED, SRC_UNMARKED, SRC_DOCK) if not os.path.isfile(p)]
    if missing:
        raise SystemExit(
            "robustness_corpus.py: missing required source PDFs: "
            + ", ".join(missing)
            + " — run after drawings/winvic/*.pdf and drawings/_int_d77.pdf exist."
        )


# ── 1. Rotated variants (90 / 180 / 270) ──────────────────────────────────
def make_rotated():
    for deg in (90, 180, 270):
        out = os.path.join(CORPUS, f"yard_rotated_{deg}.pdf")
        d = fitz.open(SRC_MARKED)
        for page in d:
            page.set_rotation(deg)
        d.save(out)
        d.close()
        log(f"wrote {out}")


# ── 2. Multi-page tender pack with target NOT on page 0 ──────────────────
def make_multipage_pack():
    out = os.path.join(CORPUS, "multipage_pack_target_not_page0.pdf")
    merged = fitz.open()

    # Page 0+: junk/filler pages pulled from the tender pack (non-target sheets)
    filler_candidates = []
    if os.path.isdir(TENDER_PACK_DIR):
        for root, _, files in os.walk(TENDER_PACK_DIR):
            for f in files:
                if f.lower().endswith(".pdf"):
                    filler_candidates.append(os.path.join(root, f))
    filler_candidates.sort()  # deterministic order

    n_filler = 0
    for fp in filler_candidates[:3]:
        try:
            src = fitz.open(fp)
            merged.insert_pdf(src, from_page=0, to_page=0)
            src.close()
            n_filler += 1
        except Exception as e:
            log(f"  skip filler {fp}: {e}")

    # Target sheet (the real marked yard plan) goes AFTER the filler pages
    target = fitz.open(SRC_MARKED)
    merged.insert_pdf(target)
    target.close()

    # One more filler page after, to be extra sure page 0 assumption is wrong
    dock = fitz.open(SRC_DOCK)
    merged.insert_pdf(dock)
    dock.close()

    merged.save(out)
    merged.close()
    log(f"wrote {out} ({n_filler} filler + target + dock pages, target NOT on page 0)")


# ── 3. Rasterized-at-150dpi (scan simulation, image-only PDF) ────────────
def make_rasterized():
    out = os.path.join(CORPUS, "yard_rasterized_150dpi.pdf")
    src = fitz.open(SRC_MARKED)
    page = src[0]
    pix = page.get_pixmap(dpi=150)
    img_pdf = fitz.open()
    imgdoc = fitz.open(stream=pix.tobytes("png"), filetype="png")
    rect = imgdoc[0].rect
    new_page = img_pdf.new_page(width=rect.width, height=rect.height)
    new_page.insert_image(rect, stream=pix.tobytes("png"))
    img_pdf.save(out)
    img_pdf.close()
    imgdoc.close()
    src.close()
    log(f"wrote {out} (image-only, no vector/text/annotation data)")


# ── 4. Encrypted PDF (owner password) ─────────────────────────────────────
def make_encrypted():
    out = os.path.join(CORPUS, "yard_encrypted.pdf")
    d = fitz.open(SRC_MARKED)
    perm = int(
        fitz.PDF_PERM_PRINT
        | fitz.PDF_PERM_COPY
        | fitz.PDF_PERM_ANNOTATE
    )
    d.save(
        out,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="fortel-owner-pw-do-not-use-in-prod",
        user_pw="",  # opens without prompt, but restricted/owner-locked
        permissions=perm,
    )
    d.close()
    log(f"wrote {out} (AES-256 owner-password encrypted)")


# ── 5. Truncated / corrupt file (first 60% of bytes) ──────────────────────
def make_truncated():
    out = os.path.join(CORPUS, "yard_truncated_60pct.pdf")
    with open(SRC_MARKED, "rb") as f:
        data = f.read()
    cut = int(len(data) * 0.6)
    with open(out, "wb") as f:
        f.write(data[:cut])
    log(f"wrote {out} ({cut}/{len(data)} bytes)")


# ── 6. Zero-byte .pdf ──────────────────────────────────────────────────────
def make_zero_byte():
    out = os.path.join(CORPUS, "zero_byte.pdf")
    open(out, "wb").close()
    log(f"wrote {out} (0 bytes)")


# ── 7. Text file renamed .pdf ──────────────────────────────────────────────
def make_fake_pdf():
    out = os.path.join(CORPUS, "not_actually_a_pdf.pdf")
    with open(out, "w") as f:
        f.write(
            "This is a plain text file with a .pdf extension.\n"
            "It should be refused cleanly, not crash the pipeline.\n"
            "Area: 26080 m2 (this text should NOT be parsed as a real area)\n"
        )
    log(f"wrote {out} (plain text masquerading as PDF)")


# ── 8. Tiny 10x10pt page PDF ────────────────────────────────────────────────
def make_tiny_page():
    out = os.path.join(CORPUS, "tiny_10x10pt.pdf")
    d = fitz.open()
    d.new_page(width=10, height=10)
    d.save(out)
    d.close()
    log(f"wrote {out} (single 10x10pt page, no content)")


# ── 9. .zip containing a drawing + junk ────────────────────────────────────
def make_zip_bundle():
    out = os.path.join(CORPUS, "bundle_with_junk.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(SRC_MARKED, arcname="Yard_Area_Proposed_Site_Plan.pdf")
        zf.writestr("readme_junk.txt", "This is not a drawing.\n")
        zf.writestr("random_bytes.bin", os.urandom(256))
    log(f"wrote {out} (real PDF + junk files, zipped)")


# ── 10. Minimal .eml with a PDF attachment ─────────────────────────────────
def make_eml_with_attachment():
    out = os.path.join(CORPUS, "enquiry_with_attachment.eml")
    msg = MIMEMultipart()
    msg["Subject"] = "Sub-contract Enquiry - Synthetic Test Site, Unit 9"
    msg["From"] = "estimator@example-contractor.co.uk"
    msg["To"] = "takeoff@fortel.co.uk"
    msg["Date"] = "Mon, 01 Jul 2026 09:00:00 +0000"

    body = MIMEText(
        "Please find attached the proposed site plan for pricing.\n\n"
        "Regards,\nSynthetic Test Contractor\n",
        "plain",
    )
    msg.attach(body)

    with open(SRC_MARKED, "rb") as f:
        pdf_bytes = f.read()
    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header(
        "Content-Disposition", "attachment", filename="Proposed_Site_Plan.pdf"
    )
    msg.attach(attachment)

    with open(out, "wb") as f:
        f.write(msg.as_bytes(policy=email.policy.SMTP))
    log(f"wrote {out} (synthetic .eml with real PDF attachment)")


def main():
    require_sources()
    reset_corpus_dir()
    make_rotated()
    make_multipage_pack()
    make_rasterized()
    make_encrypted()
    make_truncated()
    make_zero_byte()
    make_fake_pdf()
    make_tiny_page()
    make_zip_bundle()
    make_eml_with_attachment()
    n = len(os.listdir(CORPUS))
    log(f"done — {n} files in {CORPUS}")


if __name__ == "__main__":
    main()
