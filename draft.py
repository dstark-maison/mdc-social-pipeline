"""
Maison de Cocon — social draft stage (Instagram + Facebook).

Two ways in:
  * scheduled (cron): articles PUBLISHED AFTER config.DRAFT_CUTOFF that have no
    ledger entry and are not on config.HOLD_ARTICLE_IDS
  * on request (workflow_dispatch, env ARTICLE_ID = numeric Shopify id): that
    one article, whatever its age. Still refuses held articles, and never
    re-drafts a channel that is already published / mid-publish.

For each article:
  1. generate the German copy with Claude (the Instagram caption)
  2. crop the featured image to 1:1 and push it (public raw URL, no hosting)
  3. derive the Facebook message from the caption (clickable UTM link, fewer hashtags)
  4. save both drafts + both ledger entries (status `draft`) in ONE commit
  5. send ONE approval email: IG + FB previews, Approve both / IG only / FB only / Reject

Nothing is created on any platform at draft time.
"""

import os
import re
import sys
from pathlib import Path

from core import approval, blog, config, copy, image, ledger, meta
from channels import facebook, instagram

REQUIRED_ENV = [
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "SHOPIFY_BLOG_HANDLE", "ANTHROPIC_API_KEY", "ACCESS_TOKEN",
    "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO",
    "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
]
CHANNELS = (instagram.CODE, facebook.CODE)
APPROVABLE_COMBOS = ("ig", "fb", "ig+fb", "story")  # what an approval title accepts


def select_new_articles(articles, ledger_data, cutoff=None, hold=None):
    cutoff = blog.parse_ts(cutoff or config.DRAFT_CUTOFF)
    hold = config.HOLD_ARTICLE_IDS if hold is None else hold
    picked = []
    for a in articles:
        aid = ledger.numeric_id(a["id"])
        if (a["isPublished"] and a.get("publishedAt")
                and blog.parse_ts(a["publishedAt"]) > cutoff
                and aid not in hold
                and not ledger_data.get(aid)):  # no entry for ANY channel
            picked.append(a)
    return picked


def channels_to_draft(article_id, ledger_data):
    """Channels that may be (re-)drafted: not already published or mid-publish."""
    return [c for c in CHANNELS
            if (ledger.get(ledger_data, article_id, c) or {}).get("status")
            not in (ledger.PUBLISHED, ledger.PUBLISHING)]


def check_explicit(article_id, article, ledger_data, hold=None):
    """Raises if an on-request draft must not go ahead; returns the channels to draft."""
    hold = config.HOLD_ARTICLE_IDS if hold is None else hold
    if article_id in hold:
        raise RuntimeError(f"Article {article_id} is on hold: {hold[article_id]}")
    if not article["isPublished"]:
        raise RuntimeError(f"Article {article_id} is not published")
    channels = channels_to_draft(article_id, ledger_data)
    if not channels:
        raise RuntimeError(f"Article {article_id} is already published (or mid-publish) on every channel; "
                           "not drafting again")
    return channels


def build_drafts(article, model, channels):
    """Copy + image once, then one draft per channel. Returns {channel: draft}."""
    blog_handle = os.environ["SHOPIFY_BLOG_HANDLE"]
    handle = article["handle"]  # display + link only; the key is the numeric id
    article_id = ledger.numeric_id(article["id"])
    if not (article.get("image") or {}).get("url"):
        raise RuntimeError("Article has no featured image")

    content = copy.generate_instagram_caption(article, model)

    image_repo_path = f"generated/ig-{article_id}.jpg"
    image.crop_to_square(article["image"]["url"], Path(image_repo_path))
    # Pushed before the email so the preview URL is live.
    ledger.commit_and_push([image_repo_path], f"Add social image for {article_id} ({handle}) [skip ci]")

    drafts = {}
    if instagram.CODE in channels:
        drafts[instagram.CODE] = instagram.build_draft(
            article, blog.build_article_url(blog_handle, handle), content, image_repo_path)
    if facebook.CODE in channels:
        drafts[facebook.CODE] = facebook.build_draft(
            article, facebook.article_url_with_utm(blog_handle, handle), content, image_repo_path)
    return drafts


def preview(channel, d):
    text = d["full_caption"] if channel == instagram.CODE else d["message"]
    return {"label": {"ig": "Instagram", "fb": "Facebook"}[channel],
            "image_url": config.public_raw_url(d["image_path"]),
            "text": text, "target": d["target"], "link": d["article_url"]}


def buttons_for(channels):
    if len(channels) == 2:
        return [("Approve both", "approve", "ig+fb"), ("IG only", "approve", "ig"),
                ("FB only", "approve", "fb"), ("Reject", "reject", "ig+fb")]
    return [("Approve", "approve", channels[0]), ("Reject", "reject", channels[0])]


def process(article, model, channels=CHANNELS):
    """Draft one article. Order matters: persist FIRST, email SECOND, so an email
    can never point at content that was not saved. Re-drafting the same article
    replaces its drafts (same approval links); other articles are untouched."""
    article_id = ledger.numeric_id(article["id"])
    channels = [c for c in CHANNELS if c in channels]
    drafts = build_drafts(article, model, channels)

    def mutate(data):
        for channel in drafts:
            ledger.set_entry(
                data, article_id, channel,
                status=ledger.DRAFT, article_id=article_id, handle=article["handle"],
                post_id=None, url=None, container_id=None, timestamp=ledger.now(),
            )

    files = {ledger.draft_path(article_id, c): ledger.draft_text(d) for c, d in drafts.items()}
    data = ledger.update(mutate, f"Draft {article_id} ({article['handle']}, {'+'.join(drafts)}) [skip ci]", files)

    pending = [
        (aid, "+".join(chs), d.get("title"), d.get("handle"))
        for aid, chs, d in ledger.pending_drafts(data, exclude_article_id=article_id)
        if "+".join(chs) in APPROVABLE_COMBOS
    ]
    approval.send_draft_email(
        {**article, "article_id": article_id},
        previews=[preview(c, d) for c, d in drafts.items()],
        buttons=buttons_for(list(drafts)),
        pending=pending,
    )


def main():
    config.require_env(REQUIRED_ENV)
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    target = os.environ.get("ARTICLE_ID", "").strip()

    try:
        # every draft run includes Facebook, so pages_manage_posts is required here
        meta.preflight(meta.scopes_for(CHANNELS))
        if target:
            if not re.fullmatch(r"[0-9]+", target):
                raise ValueError(f"article_id must be the NUMERIC Shopify id, got {target!r}")
            article = blog.fetch_article(target)
            queue = [(article, check_explicit(target, article, ledger.load()))]
        else:
            articles = blog.fetch_recent_articles(os.environ["SHOPIFY_BLOG_HANDLE"])
            queue = [(a, list(CHANNELS)) for a in select_new_articles(articles, ledger.load())]
    except Exception as exc:
        print(f"Cannot start: {exc}")
        approval.send_failure_email(f"Draft not started (article_id={target or 'cron'})", str(exc))
        sys.exit(1)

    if not queue:
        print("No new articles to draft.")
        return

    failures = []
    for article, channels in queue:
        print(f"Drafting for: {article['title']} ({'+'.join(channels)})")
        try:
            process(article, model, channels)
            print("  Drafts saved, approval email sent.")
        except Exception as exc:
            # One article must not block the rest. No ledger entry is written,
            # so it is retried next run.
            print(f"  FAILED to draft '{article['title']}': {exc}")
            failures.append((article["title"], exc))
            try:
                approval.send_failure_email(f"Draft failed: {article['title']}", str(exc))
            except Exception as mail_exc:
                print(f"  (failure email also failed: {mail_exc})")

    if failures:
        print(f"\n{len(failures)} article(s) failed to draft")
        sys.exit(1)


if __name__ == "__main__":
    main()
