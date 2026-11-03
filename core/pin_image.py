"""Pinterest pin image: 1000x1500 (2:3) from the article image with the title overlaid.

Brand: Paper #FAF9F7 card, title in Cormorant Garamond, small wordmark in Jost
(both SIL OFL, files in assets/fonts/). Deterministic and offline: Pillow only.
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1000, 1500
PAPER = (250, 249, 247)        # #FAF9F7
INK = (43, 39, 36)
MUTED = (138, 129, 120)
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
CARD_MARGIN = 64               # card inset from the image edge
CARD_PAD_X, CARD_PAD_TOP, CARD_PAD_BOTTOM = 56, 60, 44
MAX_TITLE_LINES = 4
BRAND = "MAISON DE COCON"


def _font(name, size, weight):
    font = ImageFont.truetype(str(FONT_DIR / name), size)
    try:
        font.set_variation_by_axes([weight])
    except Exception:  # static font or FreeType without variation support: keep the default instance
        pass
    return font


def title_font(size):
    return _font("CormorantGaramond-Variable.ttf", size, 500)


def brand_font(size):
    return _font("Jost-Variable.ttf", size, 400)


def cover_crop(img, width=WIDTH, height=HEIGHT):
    """Scale to cover the target box, then centre-crop."""
    scale = max(width / img.width, height / img.height)
    resized = img.resize((max(width, round(img.width * scale)), max(height, round(img.height * scale))), Image.LANCZOS)
    left, top = (resized.width - width) // 2, (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def wrap(text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.getlength(trial) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_title(text, max_width, max_lines=MAX_TITLE_LINES, start=68, floor=38):
    """Largest font size (step 2) at which the title fits in max_lines."""
    for size in range(start, floor - 1, -2):
        font = title_font(size)
        lines = wrap(text, font, max_width)
        if len(lines) <= max_lines and all(font.getlength(l) <= max_width for l in lines):
            return font, lines, size
    font = title_font(floor)
    lines = wrap(text, font, max_width)
    return font, lines[:max_lines], floor  # last resort: truncated (titles are <=100 chars, so rare)


def _spaced(draw, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + spacing


def _spaced_width(text, font, spacing):
    return sum(font.getlength(c) + spacing for c in text) - spacing


def render(source_image, title):
    """source_image: PIL image. Returns a 1000x1500 RGB PIL image."""
    canvas = cover_crop(source_image.convert("RGB")).copy()
    draw = ImageDraw.Draw(canvas)

    inner = WIDTH - 2 * CARD_MARGIN - 2 * CARD_PAD_X
    font, lines, size = fit_title(title.strip(), inner)
    line_h = round(size * 1.16)
    small = brand_font(22)
    block_h = line_h * len(lines)
    rule_gap, brand_gap = 34, 22
    card_h = CARD_PAD_TOP + block_h + rule_gap + 1 + brand_gap + 22 + CARD_PAD_BOTTOM
    card_bottom = HEIGHT - CARD_MARGIN
    card_top = card_bottom - card_h

    draw.rectangle([CARD_MARGIN, card_top, WIDTH - CARD_MARGIN, card_bottom], fill=PAPER)
    y = card_top + CARD_PAD_TOP
    for line in lines:
        draw.text((CARD_MARGIN + CARD_PAD_X, y - round(size * 0.12)), line, font=font, fill=INK)
        y += line_h
    y += rule_gap
    draw.line([(CARD_MARGIN + CARD_PAD_X, y), (CARD_MARGIN + CARD_PAD_X + 64, y)], fill=MUTED, width=1)
    _spaced(draw, (CARD_MARGIN + CARD_PAD_X, y + brand_gap), BRAND, small, MUTED, 6)
    return canvas


def render_bytes(source_bytes, title, quality=90):
    img = Image.open(io.BytesIO(source_bytes))
    out = io.BytesIO()
    render(img, title).save(out, "JPEG", quality=quality)
    return out.getvalue()


def save(source_image, title, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render(source_image, title).save(path, "JPEG", quality=90)
    return path
