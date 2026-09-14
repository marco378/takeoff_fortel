"""A synthetic Surface Finishes Plan, authored in DISPLAY coordinates.

The point of the 270-degree variant is the trap that has cost this project twice: page.get_*
returns UNROTATED coordinates while the raster and the reader both work in the rotated frame.
A fixture drawn only at rotation 0 cannot catch a derotation mistake, so the same sheet is
built at both and the two must agree.
"""
import sys
import fitz

DISPLAY_W, DISPLAY_H = 1190, 842


def build(path, rotation=0):
    doc = fitz.open()
    if rotation in (90, 270):
        page = doc.new_page(width=DISPLAY_H, height=DISPLAY_W)
        # display (dx, dy) -> page (x, y) for a 270-degree rotation
        to_page = lambda dx, dy: (DISPLAY_H - dy, dx)
        text_angle = 270
    else:
        page = doc.new_page(width=DISPLAY_W, height=DISPLAY_H)
        to_page = lambda dx, dy: (dx, dy)
        text_angle = 0

    def line(a, b, colour, width=0.7):
        page.draw_line(fitz.Point(*to_page(*a)), fitz.Point(*to_page(*b)),
                       color=colour, width=width)

    def text(at, body, size):
        page.insert_text(fitz.Point(*to_page(*at)), body, fontsize=size, rotate=text_angle)

    text((60, 40), "WP5 SYNTHETIC TERMINAL", 14)
    text((60, 60), "SURFACE FINISHES PLAN SHEET 1 OF 1", 12)
    text((60, 80), "Scale 1:500", 9)

    rows = [("Synthetic HGV Slab Construction 200mm thick Refer to DD-C-1010", (0, 1, 1)),
            ("Synthetic Container Slab Construction 375mm thick Refer to DD-C-1020", (1, 0.75, 0))]
    text((900, 120), "Legend", 11)
    for index, (body, colour) in enumerate(rows):
        y = 150 + index * 40
        for step in range(8):                 # swatch: deliberately denser than the plan,
            x = 900 + step * 4                # exactly as the real BWB sheets draw it
            line((x, y - 6), (x + 6, y + 6), colour)
        text((960, y + 3), body, 6)

    # Two constructions, well clear of the legend column so nothing reads as a swatch.
    for (x0, y0, x1, y1), colour, period in (((100, 200, 380, 620), (0, 1, 1), 16),
                                             ((420, 200, 700, 620), (1, 0.75, 0), 16)):
        offset = x0 - (y1 - y0)
        while offset < x1:
            ax = max(offset, x0)
            bx = min(offset + (y1 - y0), x1)
            if bx > ax:
                line((ax, y0 + (ax - offset)), (bx, y0 + (bx - offset)), colour)
            offset += period

    if rotation:
        page.set_rotation(rotation)
    doc.save(path)
    doc.close()


if __name__ == "__main__":
    build(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)
