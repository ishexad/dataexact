"""Generate the Open Graph link-preview images, one per page.

1200x630 is the size Facebook, LinkedIn, Slack and X all crop from. Built in
the site's own palette so a shared link looks like the site it points at.
Orbitron and Share Tech Mono aren't installed locally, so this uses Arial
Bold (letter-spaced, which is what gives Orbitron its wide geometric feel)
and Consolas for the mono lines. Those are Windows font paths — adjust F if
you run this elsewhere.

    python make_og_images.py

Writes static/marketing/og-<page>.png, which the marketing mount serves from
/og-<page>.png. Rerun it whenever a page title or strapline changes, then
commit the regenerated files — nothing produces them at runtime.

Needs Pillow, deliberately not in requirements.txt: this is a build-time
script the app never imports.
"""
import os

from PIL import Image, ImageDraw, ImageFont

# Run from anywhere: the paths below are relative to the repo root.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

W, H = 1200, 630
BG = (247, 245, 238)
PANEL = (255, 255, 255)
LINE = (221, 217, 203)
AMBER = (201, 122, 25)
CYAN = (31, 143, 148)
TEXT = (34, 38, 31)
MUTED = (118, 116, 105)

F = "C:/Windows/Fonts/"
font_mark = ImageFont.truetype(F + "arialbd.ttf", 30)
font_title = ImageFont.truetype(F + "arialbd.ttf", 74)
font_title_sm = ImageFont.truetype(F + "arialbd.ttf", 58)
font_sub = ImageFont.truetype(F + "consola.ttf", 28)
font_url = ImageFont.truetype(F + "consola.ttf", 26)

PAGES = {
    "home": ("PRECISION DATA TOOLS",
             "Excel and Word tools that each do one job properly"),
    "app": ("EXCEL TO WORD",
            "Turn a spreadsheet into an organized Word document"),
    "compare": ("COMPARE EXCEL FILES",
                "See exactly what changed between two versions"),
    "format": ("FORMAT REPORT",
               "Give a plain Word document a professional look"),
    "anomaly": ("DETECT ANOMALIES",
                "Find the errors in a spreadsheet before they cost you"),
    "codebook": ("QUALITATIVE CODING",
                 "Code a document corpus against your own codebook"),
}


def draw_tracked(draw, xy, text, font, fill, tracking=0):
    """Pillow has no letter-spacing, so step through the string by hand."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking
    return x


def wrap(text, font, max_width):
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build(slug, title, subtitle):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Faint grid, echoing the hero canvas on the home page.
    for x in range(0, W, 60):
        d.line([(x, 0), (x, H)], fill=(238, 235, 225), width=1)
    for y in range(0, H, 60):
        d.line([(0, y), (W, y)], fill=(238, 235, 225), width=1)

    # White panel, the site's recurring container.
    m = 44
    d.rectangle([m, m, W - m, H - m], fill=PANEL, outline=LINE, width=1)

    pad = m + 56
    y = m + 54

    # Wordmark: [ DATAEXACT ] with the brackets in amber, plus the cyan blip.
    x = pad
    x = draw_tracked(d, (x, y), "[", font_mark, AMBER, 6)
    x += 8
    x = draw_tracked(d, (x, y), "DATAEXACT", font_mark, TEXT, 6)
    x += 4
    x = draw_tracked(d, (x, y), "]", font_mark, AMBER, 6)
    d.rectangle([x + 14, y + 12, x + 24, y + 22], fill=CYAN)

    # Title, tracked out to approximate Orbitron's width.
    max_w = W - pad * 2
    font_t = font_title
    lines = wrap(title, font_t, max_w - 120)
    if len(lines) > 2 or font_t.getlength(title) > max_w - 60:
        font_t = font_title_sm
        lines = wrap(title, font_t, max_w - 60)

    y = 236 if len(lines) == 1 else 196
    for line in lines:
        draw_tracked(d, (pad, y), line, font_t, TEXT, 3)
        y += font_t.size + 16

    # Amber rule under the title.
    y += 12
    d.rectangle([pad, y, pad + 132, y + 5], fill=AMBER)

    # Subtitle.
    y += 40
    for line in wrap(subtitle, font_sub, max_w - 40):
        d.text((pad, y), line, font=font_sub, fill=MUTED)
        y += font_sub.size + 10

    # Footer: domain left, free-tier note right.
    fy = H - m - 76
    d.line([(pad, fy - 18), (W - pad, fy - 18)], fill=LINE, width=1)
    d.text((pad, fy), "dataexact.io", font=font_url, fill=CYAN)
    note = "3 files free \u00b7 no signup"
    d.text((W - pad - font_url.getlength(note), fy), note, font=font_url, fill=MUTED)

    out = f"static/marketing/og-{slug}.png"
    img.save(out, "PNG", optimize=True)
    return out, os.path.getsize(out)


for slug, (title, subtitle) in PAGES.items():
    path, size = build(slug, title, subtitle)
    print(f"{path:<36} {size / 1024:6.1f} KB")
