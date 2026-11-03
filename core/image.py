"""Image handling."""

import io

from PIL import Image

from core import http


def download(image_url):
    """Download the article's featured image as an RGB PIL image."""
    resp = http.request("GET", image_url, "image-download")
    content_type = resp.headers.get("Content-Type")
    print(f"  Downloaded featured image: {content_type}, {len(resp.content)} bytes")
    try:
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as exc:
        # Confirmed live: an article's featured image can be an SVG, which
        # Pillow can't rasterize. The real fix is replacing the image in
        # Shopify, not code.
        raise RuntimeError(
            f"Could not open featured image as a raster image "
            f"(Content-Type: {content_type}, {len(resp.content)} bytes). "
            f"If this is an SVG or other vector/unsupported format, replace "
            f"the article's featured image in Shopify with a photo."
        ) from exc


def crop_to_square(image_url, output_path):
    """Download the article's featured image and center-crop to 1:1."""
    img = download(image_url)
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    cropped = img.crop((left, top, left + side, top + side))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path, "JPEG", quality=90)
