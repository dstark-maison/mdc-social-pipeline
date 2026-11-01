"""
Maison de Cocon — social publish / reject stage.

Input: env TITLE, shaped exactly "<approve|reject>:<article_id>:<channels>"
(validated with a strict regex; <id> is the NUMERIC Shopify article id). It comes from an
approval issue title or from workflow_dispatch inputs — never from a shell
command line.

approve  → publish each channel, read it back by id, write the ledger.
           Already `published` → no-op.
reject   → mark the draft `rejected` in the ledger. No publish call.

The outcome is written to RESULT_FILE (markdown) for the workflow to post on
the approval issue. Any failure also sends a failure email with the raw error.
"""

import os
import re
import sys
from urllib.parse import urljoin
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from core import approval, config, http, ledger, meta, reddit_store
from core.approval import ACTION_RE
from channels import facebook, instagram

CHANNELS = {instagram.CODE: instagram, facebook.CODE: facebook}

REQUIRED_ENV = ["GITHUB_REPOSITORY", "GITHUB_REF_NAME", "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO"]
IG_ENV = ["ACCESS_TOKEN", "IG_USER_ID", "SHOPIFY_STORE_DOMAIN",
          "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "BIO_LINK_PATH"]
FB_ENV = ["ACCESS_TOKEN", "IG_USER_ID"]  # IG_USER_ID only identifies the linked Page
ENV_BY_CHANNEL = {instagram.CODE: IG_ENV, facebook.CODE: FB_ENV}


def parse_title(title):
    if not ACTION_RE.fullmatch(title or ""):
        raise ValueError(f"Rejected input: {title!r} does not match the approval format.")
    action, article_id, channels = title.split(":")
    return action, article_id, channels.split("+")


def do_reject(article_id, channel):
    data = ledger.load()
    entry = ledger.get(data, article_id, channel)
    if not entry:
        return f"- `{channel}`: no draft found, nothing to reject."
    if entry["status"] not in (ledger.DRAFT, ledger.APPROVED):
        return f"- `{channel}`: status is `{entry['status']}`, left unchanged."

    def mutate(d):
        ledger.set_entry(d, article_id, channel, status=ledger.REJECTED, timestamp=ledger.now())

    ledger.update(mutate, f"Reject {article_id} ({channel}) [skip ci]")
    return f"- `{channel}`: draft rejected."


class Blocked(RuntimeError):
    """The approval cannot proceed. Reported like any other failure (email + issue comment)."""


def label(article_id):
    """'id (handle)' for messages; the handle is display-only."""
    for entry in ledger.load().get(article_id, {}).values():
        if entry.get("handle"):
            return f"{article_id} ({entry['handle']})"
    return article_id


# --- Reddit: manual publishing (no Reddit API) -------------------------------------
# approve -> copy-ready email; the owner posts by hand and submits the URL -> published.
# The approval issue comment and the ledger never contain the draft text.
_REDDIT_HOST = r"(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com"
_REDDIT_BASE = r"https://" + _REDDIT_HOST + r"/r/" + config.REDDIT_SUBREDDIT
REDDIT_POST_RE = re.compile(_REDDIT_BASE + r"/comments/([a-z0-9]{5,10})(?:/[^\s?#]*)?", re.I)   # slug / trailing slash optional
REDDIT_SHARE_RE = re.compile(_REDDIT_BASE + r"/s/([A-Za-z0-9]{5,20})/?", re.I)                    # Reddit "share" links
REDDIT_HOST_RE = re.compile(r"https://" + _REDDIT_HOST + r"(?:/|$)", re.I)
SHARE_UA = "Mozilla/5.0 (compatible; mdc-social-pipeline/1.0)"


class ShareResolveError(RuntimeError):
    pass


def _clean_url(url):
    """Trim blanks, drop the query string and fragment (utm/share parameters are irrelevant)."""
    return (url or "").strip().split("?")[0].split("#")[0]


def reddit_post_id(url):
    """Post id from a post URL (with or without slug, trailing slash, query; www/old/new/np/m), or None."""
    m = REDDIT_POST_RE.fullmatch(_clean_url(url))
    return m.group(1).lower() if m else None


def reddit_share_id(url):
    m = REDDIT_SHARE_RE.fullmatch(_clean_url(url))
    return m.group(1) if m else None


def resolve_share(url, max_hops=5):
    """Follow a /r/<sub>/s/<id> share link's HTTP redirects (a plain redirect lookup, NOT the Reddit API) to the
    canonical post id. Only reddit.com hosts are ever contacted. Raises ShareResolveError."""
    current = url.strip()
    for _ in range(max_hops):
        if not REDDIT_HOST_RE.match(current):
            raise ShareResolveError("the link redirects away from reddit.com")
        try:
            resp = requests.get(current, allow_redirects=False, timeout=20, headers={"User-Agent": SHARE_UA})
        except requests.RequestException as exc:
            raise ShareResolveError(f"Reddit could not be reached ({type(exc).__name__})") from None
        location = resp.headers.get("Location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            current = urljoin(current, location)
            post_id = reddit_post_id(current)
            if post_id:
                return post_id
            continue
        raise ShareResolveError(f"Reddit answered HTTP {resp.status_code} without a redirect "
                                f"(it may block automated lookups)")
    raise ShareResolveError("too many redirects")


def reddit_resubmit_help(question_id, reason):
    """The failure text for an unusable URL: says what to do and lists the exact form inputs."""
    repo = os.environ.get("GITHUB_REPOSITORY", "dstark-maison/mdc-social-pipeline")
    return (f"Re-submit with the full URL.\n\nWhy it failed: {reason}\n\n"
            f"Open the post on Reddit and copy its full address from the browser's address bar "
            f"(https://www.reddit.com/r/{config.REDDIT_SUBREDDIT}/comments/<id>/<title>/). Then:\n"
            f"Actions -> Social Publish -> Run workflow: https://github.com/{repo}/actions/workflows/social-publish.yml\n\n"
            f"Exact form inputs:\n"
            f"  article: {question_id}\n"
            f"  channel: reddit\n"
            f"  url: <the full address-bar URL of the post>\n"
            f"  test_bad_token: leave unchecked")


def suggest_day(data, today=None):
    """First day from tomorrow on that has no other approved/published Reddit post (one post per day)."""
    today = today or datetime.now(timezone.utc).date()
    taken = set()
    for chs in data.values():
        e = chs.get("reddit") if isinstance(chs, dict) else None
        if e and e.get("status") == ledger.APPROVED and e.get("post_on"):
            taken.add(e["post_on"])
        if e and e.get("status") == ledger.PUBLISHED and e.get("published_on"):
            taken.add(e["published_on"])
    day = today + timedelta(days=1)
    while day.isoformat() in taken:
        day += timedelta(days=1)
    return day


def do_reddit(question_id):
    """Approve, or (with REDDIT_URL set) record the posted URL. Returns (markdown line, ok)."""
    data = ledger.load()
    entry = ledger.get(data, question_id, "reddit")
    if not entry:
        raise Blocked(f"No Reddit draft with id {question_id} in the ledger.")
    url = os.environ.get("REDDIT_URL", "").strip()

    if url:
        if entry["status"] == ledger.PUBLISHED:  # a re-submit is a no-op: no resolution, no second email
            return f"- `reddit`: already published, nothing done → {entry['url']}", True
        if entry["status"] != ledger.APPROVED:
            raise Blocked(f"Draft {question_id} is `{entry['status']}`: approve it first, then submit the URL.")
        post_id = reddit_post_id(url)
        if not post_id and reddit_share_id(url):
            try:
                post_id = resolve_share(url)
            except ShareResolveError as exc:
                raise Blocked(reddit_resubmit_help(
                    question_id, f"the share link could not be turned into the post URL ({exc}). "
                                 f"Please paste the address-bar URL of the post instead of the share link."))
        if not post_id:
            raise Blocked(reddit_resubmit_help(
                question_id, f"{url!r} is not a post URL in r/{config.REDDIT_SUBREDDIT} "
                             f"(accepted: .../comments/<id>/..., old.reddit.com, or a /s/<id> share link)"))
        canonical = f"https://www.reddit.com/r/{config.REDDIT_SUBREDDIT}/comments/{post_id}/"

        def published(d):
            ledger.set_entry(d, question_id, "reddit", status=ledger.PUBLISHED, post_id=post_id, url=canonical,
                             timestamp=ledger.now(), published_on=ledger.now()[:10])
        ledger.update(published, f"Reddit {question_id} posted [skip ci]")
        info = {}
        try:  # article title for the confirmation; not essential
            batch = reddit_store.load_batch(reddit_store.batch_id_of(question_id))
            d = next((x for x in (batch or {}).get("drafts", []) if x["id"] == question_id), None)
            if d:
                info = {"article_title": d.get("article_title"), "article_link": d.get("link")}
        except Exception:
            pass
        try:  # exactly ONE short confirmation, only on this transition
            approval.send_reddit_published_email(canonical, info)
        except Exception as exc:
            print(f"  (confirmation email failed: {type(exc).__name__})")
        return (f"- `reddit`: marked published → {canonical} "
                f"(recorded from the URL you gave; Reddit cannot be read back, its API is closed)"), True

    if entry["status"] == ledger.PUBLISHED:
        return f"- `reddit`: already published, nothing done → {entry['url']}", True
    if entry["status"] == ledger.APPROVED:
        return f"- `reddit`: already approved (suggested day {entry.get('post_on')}); nothing done", True
    if entry["status"] == ledger.REJECTED:
        raise Blocked("This draft was rejected; it will not be posted.")
    try:
        batch = reddit_store.load_batch(reddit_store.batch_id_of(question_id))
    except reddit_store.StoreError as exc:
        raise Blocked(str(exc))
    if batch is None:
        raise Blocked("The draft's encrypted artifact is gone (90-day retention) or was never uploaded: it cannot be "
                      "approved any more. Re-run the weekly Reddit job to draft new questions.")
    draft = next((d for d in batch["drafts"] if d["id"] == question_id), None)
    if not draft:
        raise Blocked(f"Draft {question_id} is not in the artifact of batch {reddit_store.batch_id_of(question_id)}.")
    post_on = suggest_day(data)

    def approved(d):
        ledger.set_entry(d, question_id, "reddit", status=ledger.APPROVED, post_on=post_on.isoformat(),
                         timestamp=ledger.now())
    ledger.update(approved, f"Reddit {question_id} approved [skip ci]")
    weekday = post_on.strftime("%A")
    approval.send_reddit_ready_email(draft, post_on.isoformat(), weekday)
    return (f"- `reddit`: approved. A copy-ready email with title and body is on its way; "
            f"suggested posting day {weekday} {post_on.isoformat()}"), True


def do_approve(article_id, channel):
    """Returns (markdown line, ok). Raises on anything that blocks or breaks publishing."""
    if channel == "reddit":
        return do_reddit(article_id)
    module = CHANNELS.get(channel)
    if not module:
        raise Blocked(f"Channel `{channel}` is not implemented in this phase.")

    data = ledger.load()
    entry = ledger.get(data, article_id, channel)
    if not entry:
        raise Blocked(f"No draft in the ledger for article {article_id} / {channel}.")
    if entry["status"] == ledger.PUBLISHED:
        return f"- `{channel}`: already published, nothing done → {entry['url']}", True
    if entry["status"] == ledger.PUBLISHING:
        raise Blocked(
            "A previous run reached the publish call but never verified the post "
            "(container " + str(entry.get("container_id")) + "). Not retrying automatically: "
            "it could double-post.\n\n" + ledger.stuck_entry_help(article_id, channel, entry))
    if entry["status"] == ledger.REJECTED:
        raise Blocked("This draft was rejected; it will not be published.")

    draft = ledger.read_draft(article_id, channel)
    # Never use a stored URL: build it from the CURRENT repo name (renames break old ones).
    draft["image_url"] = config.public_raw_url(draft["image_path"])

    def mark_publishing(container_id):
        def mutate(d):
            ledger.set_entry(d, article_id, channel, status=ledger.PUBLISHING,
                             container_id=container_id, timestamp=ledger.now())
        ledger.update(mutate, f"Publishing {article_id} ({channel}) [skip ci]")

    def revert_publishing():
        def mutate(d):
            ledger.set_entry(d, article_id, channel, status=ledger.DRAFT, container_id=None,
                             timestamp=ledger.now())
        ledger.update(mutate, f"Publish refused for {article_id} ({channel}), back to draft [skip ci]")

    result = module.publish(draft, mark_publishing, revert_publishing)

    def mutate(d):
        ledger.set_entry(d, article_id, channel, status=ledger.PUBLISHED, post_id=result["post_id"],
                         url=result["url"], timestamp=result["timestamp"] or ledger.now())

    ledger.update(mutate, f"Published {article_id} ({channel}) [skip ci]")
    line = f"- `{channel}`: published and verified → {result['url']}"

    ok = True
    try:
        extra = module.after_publish(draft, result)
        if extra:
            line += f"\n  - {extra}"
    except Exception as exc:
        ok = False
        line += f"\n  - **post-publish step failed** (the post itself is live): {http.redact(exc)}"
        approval.send_failure_email(f"Post live, follow-up step failed ({channel}): {article_id}", str(exc))
    return line, ok


def write_result(text):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = os.environ.get("RESULT_FILE")
    if path:
        Path(path).write_text(text, encoding="utf-8")
    print(text)


def main():
    config.require_env(REQUIRED_ENV)
    title = os.environ.get("TITLE", "")
    try:
        action, article_id, channels = parse_title(title)
    except ValueError as exc:
        write_result(f"❌ {exc}")
        sys.exit(1)

    name = label(article_id)
    lines, failed = [], False
    if action == "reject":
        for ch in channels:
            lines.append(do_reject(article_id, ch))
        write_result(f"**Reject `{name}`**\n\n" + "\n".join(lines))
        return

    if "reddit" in channels:
        # approving needs the encrypted draft; recording a posted URL needs nothing but the ledger
        config.require_env([] if os.environ.get("REDDIT_URL") else ["REDDIT_ARTIFACT_KEY", "GITHUB_TOKEN"])
    known = [ch for ch in channels if ch in CHANNELS]
    if known:
        config.require_env(sorted({v for ch in known for v in ENV_BY_CHANNEL[ch]}))
        try:
            # pages_manage_posts is required only when Facebook is part of this run
            meta.preflight(meta.scopes_for(known))
        except meta.TokenError as exc:
            text = http.redact(exc)
            approval.send_failure_email(f"Meta token check failed: {name}", text)
            write_result(f"❌ **Not published.** {text}")
            sys.exit(1)
    for ch in channels:
        try:
            line, ok = do_approve(article_id, ch)
            failed |= not ok
        except Exception as exc:
            failed = True
            text = http.redact(exc)
            # A failure after media_publish leaves the entry in `publishing`:
            # attach the exact ledger fix (Blocked already carries it).
            entry = ledger.get(ledger.load(), article_id, ch)
            if entry and entry.get("status") == ledger.PUBLISHING and "LEDGER ENTRY TO FIX" not in text:
                text += "\n\n" + ledger.stuck_entry_help(article_id, ch, entry)
            elif entry and entry.get("status") == ledger.DRAFT and len(channels) > 1:
                text += (f"\n\nOnly this channel failed and it is still a draft. Retry just it: Actions -> "
                         f"Social Publish -> article={article_id}, channel={ch} "
                         f"(or the '{ch.upper()} only' button of the draft email).")
            line = f"- `{ch}`: **FAILED** — {text}"
            try:
                approval.send_failure_email(f"Publish failed ({ch}): {name}", text)
            except Exception as mail_exc:
                line += f"\n  - (failure email also failed: {http.redact(mail_exc)})"
        lines.append(line)

    write_result(f"**{'⚠️ Approve' if failed else '✅ Approve'} `{name}`**\n\n" + "\n".join(lines))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
