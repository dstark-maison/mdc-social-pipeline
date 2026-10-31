"""
Maison de Cocon — social draft stage (Instagram + Facebook + Pinterest).

Two ways in:
  * scheduled (cron): articles PUBLISHED AFTER config.DRAFT_CUTOFF that have no
    ledger entry and are not on config.HOLD_ARTICLE_IDS
  * on request (workflow_dispatch, env ARTICLE_ID = numeric Shopify id, optional
    DRAFT_CHANNELS = "ig,fb,pin" subset): that one article, whatever its age. Still
    refuses held articles, and never re-drafts a channel that is already published,
    scheduled or mid-publish.

For each article:
  1. IG + FB (German): copy once, 1:1 image, both drafts + both ledger entries in ONE
     commit, ONE approval email (Approve both / IG only / FB only / Reject)
  2. Pinterest (German): keyword-led copy, topic board, 1000x1500 image with the title,
     its OWN draft + ledger entry + its OWN approval email (Approve / Reject)

A Pinterest problem never blocks IG/FB (and vice versa): each part has its own failure
email. Nothing is created on any platform at draft time.
"""

import os
import re
import sys
from pathlib import Path

from core import approval, blog, config, copy, image, ledger, meta, pin_draft, pinterest_auth
from channels import facebook, instagram, pinterest

BASE_ENV = [
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "SHOPIFY_BLOG_HANDLE", "ANTHROPIC_API_KEY",
    "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO",
    "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
]
META_ENV = ["ACCESS_TOKEN"]
REQUIRED_ENV = BASE_ENV + META_ENV  # kept for callers/tests: the default run includes IG + FB
SOCIAL = (instagram.CODE, facebook.CODE)
CHANNELS = SOCIAL                       # the IG + FB email
ALL_CHANNELS = SOCIAL + (pinterest.CODE,)
APPROVABLE_COMBOS = ("ig", "fb", "ig+fb")  # what an approval title accepts for the pending list
LOCKED = (ledger.PUBLISHED, ledger.PUBLISHING, ledger.SCHEDULED)


def requested_channels(value=None):
    raw = os.environ.get("DRAFT_CHANNELS", "") if value is None else value
    wanted = [c.strip() for c in raw.replace("+", ",").split(",") if c.strip()] or list(ALL_CHANNELS)
    unknown = [c for c in wanted if c not in ALL_CHANNELS]
    if unknown:
        raise ValueError(f"Unknown channel(s) {unknown}; allowed: {list(ALL_CHANNELS)}")
    return [c for c in ALL_CHANNELS if c in wanted]


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


def channels_to_draft(article_id, ledger_data, requested=ALL_CHANNELS):
    """Requested channels that may be (re-)drafted: not already published, scheduled or mid-publish."""
    return [c for c in ALL_CHANNELS if c in requested
            and (ledger.get(ledger_data, article_id, c) or {}).get("status") not in LOCKED]


def check_explicit(article_id, article, ledger_data, hold=None, requested=ALL_CHANNELS):
    """Raises if an on-request draft must not go ahead; returns the channels to draft."""
    hold = config.HOLD_ARTICLE_IDS if hold is None else hold
    if article_id in hold:
        raise RuntimeError(f"Article {article_id} is on hold: {hold[article_id]}")
    if not article["isPublished"]:
        raise RuntimeError(f"Article {article_id} is not published")
    channels = channels_to_draft(article_id, ledger_data, requested)
    if not channels:
        raise RuntimeError(f"Article {article_id} is already published/scheduled (or mid-publish) on every "
                           f"requested channel; not drafting again")
    return channels


# --- Instagram + Facebook -----------------------------------------------------

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


def process(article, model, channels=SOCIAL):
    """Draft one article for IG/FB. Order matters: persist FIRST, email SECOND, so an email
    can never point at content that was not saved. Re-drafting the same article
    replaces its drafts (same approval links); other articles are untouched."""
    article_id = ledger.numeric_id(article["id"])
    channels = [c for c in SOCIAL if c in channels]
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


# --- Pinterest ------------------------------------------------------------------

def process_pin(article, model):
    """Draft one article's pin: own copy/image/board, own ledger entry, own email."""
    article_id = ledger.numeric_id(article["id"])
    pinterest_auth.preflight()  # refresh works, scopes granted, 30-day expiry warning
    draft, image_path = pin_draft.build(article, model)
    ledger.commit_and_push([image_path], f"Add pin image for {article_id} ({article['handle']}) [skip ci]")

    def mutate(data):
        ledger.set_entry(
            data, article_id, pinterest.CODE,
            status=ledger.DRAFT, article_id=article_id, handle=article["handle"],
            post_id=None, url=None, container_id=None, batch=None, publish_on=None,
            timestamp=ledger.now(),
        )

    ledger.update(mutate, f"Draft {article_id} ({article['handle']}, pin) [skip ci]",
                  {ledger.draft_path(article_id, pinterest.CODE): ledger.draft_text(draft)})
    approval.send_pin_email({**article, "article_id": article_id}, draft, config.public_raw_url(draft["image_path"]))


# --- main -----------------------------------------------------------------------

def main():
    try:
        wanted = requested_channels()
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    social_wanted = [c for c in wanted if c in SOCIAL]
    config.require_env(BASE_ENV + (META_ENV if social_wanted else []))
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    target = os.environ.get("ARTICLE_ID", "").strip()

    try:
        if social_wanted:
            # a default draft run includes Facebook, so pages_manage_posts is required then
            meta.preflight(meta.scopes_for(social_wanted))
        if target:
            if not re.fullmatch(r"[0-9]+", target):
                raise ValueError(f"article_id must be the NUMERIC Shopify id, got {target!r}")
            article = blog.fetch_article(target)
            queue = [(article, check_explicit(target, article, ledger.load(), requested=wanted))]
        else:
            articles = blog.fetch_recent_articles(os.environ["SHOPIFY_BLOG_HANDLE"])
            queue = [(a, list(wanted)) for a in select_new_articles(articles, ledger.load())]
    except Exception as exc:
        print(f"Cannot start: {exc}")
        approval.send_failure_email(f"Draft not started (article_id={target or 'cron'})", str(exc))
        sys.exit(1)

    if not queue:
        print("No new articles to draft.")
        return

    failures = []

    def attempt(label, article, fn, *args):
        try:
            fn(article, model, *args)
            print(f"  {label}: drafts saved, approval email sent.")
        except Exception as exc:
            # One part must not block the others, and one article must not block the rest.
            # No ledger entry is written for the failed part, so it is retried next run.
            print(f"  {label}: FAILED to draft '{article['title']}': {exc}")
            failures.append((label, article["title"], exc))
            try:
                approval.send_failure_email(f"Draft failed ({label}): {article['title']}", str(exc))
            except Exception as mail_exc:
                print(f"  (failure email also failed: {mail_exc})")

    for article, channels in queue:
        print(f"Drafting for: {article['title']} ({'+'.join(channels)})")
        social = [c for c in channels if c in SOCIAL]
        if social:
            attempt("IG/FB", article, process, social)
        if pinterest.CODE in channels:
            attempt("Pinterest", article, process_pin)

    if failures:
        print(f"\n{len(failures)} draft part(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
