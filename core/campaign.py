"""Campaign posts (Black Week + Christmas 2026): manifest, time windows, drafts, bio-link schedule.

A campaign post is an ordinary ledger entry keyed by a numeric post id (the `id` in
campaigns/peak-2026.yml), so the normal draft -> email -> approval issue -> publish flow
applies unchanged. What is campaign-specific lives here:

  * window_state(): approval before publish_time parks the draft as `approved` (a scheduled
    run publishes it at publish_time), approval later the same day publishes at once, and
    anything after 23:59 of the post's date is refused (`expired`).
  * build_drafts(): the exact captions from the manifest; Facebook gets a direct UTM link
    instead of "Link in der Bio."; Stories have no caption.
  * bio_decision(): which URL /pages/latest-post must point to on a given Berlin date.
"""

import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "campaigns" / "peak-2026.yml"  # independent of the cwd
BIO_CTA = "Link in der Bio."
BIO_RESET_DAYS = 3   # after reset_on + this many days the bio sync does nothing any more


def load_manifest(path=MANIFEST_PATH):
    import yaml  # lazy: only campaign code paths need PyYAML
    m = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    ids = [str(p["id"]) for p in m["posts"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate post ids in the campaign manifest")
    for p in m["posts"]:
        if not re.fullmatch(r"[0-9]+", str(p["id"])):
            raise ValueError(f"Post id must be numeric: {p['id']!r}")
        if p["image"] not in m["images"]:
            raise ValueError(f"Post {p['id']}: unknown image {p['image']!r}")
    return m


def tz(m):
    return ZoneInfo(m.get("timezone", "Europe/Berlin"))


def now_local(m):
    return datetime.now(tz(m))


def find_post(m, post_id):
    return next((p for p in m["posts"] if str(p["id"]) == str(post_id)), None)


def _hm(text):
    h, mi = text.split(":")
    return time(int(h), int(mi))


def draft_due(m, now):
    return datetime.combine(now.date(), _hm(m["draft_time"]), tzinfo=tz(m))


def publish_at(post, m):
    return datetime.combine(post["date"], _hm(m["publish_time"]), tzinfo=tz(m))


def window_state(post, m, now):
    """too_early | before_publish_time | open | expired  (all in the campaign time zone)."""
    today = now.astimezone(tz(m)).date()
    if today < post["date"]:
        return "too_early"
    if today > post["date"]:
        return "expired"
    return "open" if now >= publish_at(post, m) else "before_publish_time"


def label(post):
    kind = "Story" if post["type"] == "story" else "Feed post"
    return f"Peak 2026 · {kind} {post['date'].strftime('%d.%m.')}"


# --- drafts -----------------------------------------------------------------

def image_url(post, m):
    return m["images"][post["image"]]["url"]


def facebook_message(caption, link):
    if caption.count(BIO_CTA) != 1:
        raise ValueError(f"Caption must contain {BIO_CTA!r} exactly once to be replaced for Facebook")
    return caption.replace(BIO_CTA, f"Zum Angebot: {link}")


def bio_path_for(post, m):
    """Collection the bio link points at on the post's date (shown in the email as the link)."""
    for r in m["bio_link"]["ranges"]:
        if r["from"] <= post["date"] <= r["to"]:
            return r["path"]
    return m["bio_link"]["ranges"][-1]["path"]


def build_drafts(post, m):
    """{channel: draft dict} for one post. The image URL is the Shopify CDN URL (stored: it does not
    depend on the repo name)."""
    pid, name = str(post["id"]), m["campaign"]
    base = {"article_id": pid, "handle": f"{name}-{pid}", "title": label(post), "campaign": name,
            "published_at": None, "image_url": image_url(post, m)}
    link = f"https://{os.environ.get('STOREFRONT_DOMAIN', 'www.maisondecocon.com')}{bio_path_for(post, m)}"
    drafts = {}
    for ch in post["channels"]:
        if ch == "ig":
            caption = post["caption"].rstrip("\n")
            drafts[ch] = {**base, "channel": "ig", "full_caption": caption, "caption": caption, "hashtags": [],
                          "article_url": link, "target": "Instagram feed (@maisondecocon)"}
        elif ch == "fb":
            fb_link = post["fb_link"]
            drafts[ch] = {**base, "channel": "fb", "message": facebook_message(post["caption"].rstrip("\n"), fb_link),
                          "article_url": fb_link, "target": "Facebook Page (Maison de Cocon)"}
        elif ch == "story":
            drafts[ch] = {**base, "channel": "story", "article_url": link,
                          "target": "Instagram Story (@maisondecocon), no caption"}
        else:
            raise ValueError(f"Unknown channel {ch!r} in post {pid}")
    return drafts


# --- one-off test Story -----------------------------------------------------

def test_story_id(m):
    t = m.get("test_story")
    return str(t["id"]) if t else None


def build_test_story_draft(m):
    """Story draft for the one-off live test (not a scheduled post; no date, no caption)."""
    t = m["test_story"]
    pid = str(t["id"])
    return {"article_id": pid, "handle": f"{m['campaign']}-teststory", "title": "TEST Story (neutral image, delete after)",
            "campaign": m["campaign"], "published_at": None, "image_url": t["image_url"], "channel": "story",
            "article_url": t["image_url"], "target": "Instagram Story (@maisondecocon), no caption"}


# --- bio link ---------------------------------------------------------------

def override_active(m, today):
    """True from the first range's start through the last range's end: blog publishes must not
    touch /pages/latest-post in this window."""
    ranges = m["bio_link"]["ranges"]
    return ranges[0]["from"] <= today <= ranges[-1]["to"]


def bio_decision(m, today):
    """('campaign', path) | ('reset', None) | ('none', None).

    reset = the normal behaviour: the newest published blog post (ledger pointer). It is applied
    from reset_on; BIO_RESET_DAYS later the sync stops touching anything."""
    ranges = m["bio_link"]["ranges"]
    for r in ranges:
        if r["from"] <= today <= r["to"]:
            return "campaign", r["path"]
    reset_on = m["bio_link"]["reset_on"]
    if today >= reset_on and today <= reset_on + timedelta(days=BIO_RESET_DAYS):
        return "reset", None
    return "none", None
