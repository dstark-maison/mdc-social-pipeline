"""Instagram Story channel (Graph API, media_type=STORIES, image only, no caption).

Same container flow as the feed channel: the container is created at PUBLISH time, `publishing` is
persisted before media_publish, and the post is read back by id before the ledger says `published`.
"""

import os
import time

from core import http
from channels import instagram

CODE = "story"
GRAPH_BASE = instagram.GRAPH_BASE


def create_container(draft):
    resp = http.request(
        "POST", f"{GRAPH_BASE}/{os.environ['IG_USER_ID']}/media", "story-create-container",
        headers=instagram._auth(),
        data={"image_url": draft["image_url"], "media_type": "STORIES"},
    )
    return resp.json()["id"]


def verify(post_id, attempts=5, interval_s=3):
    """Read the story back by id: it needs a permalink, and if Instagram reports the product type
    it must be STORY (a feed post by mistake would be caught here)."""
    last = None
    for _ in range(attempts):
        try:
            info = http.request(
                "GET", f"{GRAPH_BASE}/{post_id}", "story-readback", headers=instagram._auth(),
                params={"fields": "permalink,timestamp,media_product_type"},
            ).json()
            if not info.get("permalink"):
                raise RuntimeError(f"Read-back has no permalink: {info}")
            product = info.get("media_product_type")
            if product and product != "STORY":
                raise RuntimeError(f"Read-back is not a story (media_product_type={product})")
            return info
        except http.ApiError as exc:
            last = exc
            time.sleep(interval_s)
    raise last


def publish(draft, mark_publishing, revert_publishing):
    """Returns {post_id, url, timestamp}. Raises on any failure (see instagram.publish)."""
    creation_id = create_container(draft)
    instagram.wait_until_ready(creation_id)
    mark_publishing(creation_id)
    try:
        resp = http.request(
            "POST", f"{GRAPH_BASE}/{os.environ['IG_USER_ID']}/media_publish", "story-media-publish",
            headers=instagram._auth(), data={"creation_id": creation_id},
        )
    except http.ApiError:
        revert_publishing()
        raise
    post_id = resp.json()["id"]
    info = verify(post_id)
    return {"post_id": post_id, "url": info["permalink"], "timestamp": info.get("timestamp")}


def after_publish(draft, result):
    return None  # stories never touch the bio link
