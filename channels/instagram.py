"""Instagram channel (Graph API).

The media container is created at PUBLISH time, not draft time: containers
expire after 24 h, which broke approvals that arrived the next day.
"""

import os
import time
from datetime import datetime, timezone

from core import campaign, http, ledger, shopify_client

CODE = "ig"
GRAPH_BASE = "https://graph.facebook.com/v25.0"


def _auth():
    # Header, not a query param: a URL containing the token ends up in
    # exception text, logs and failure emails.
    return {"Authorization": f"Bearer {os.environ['ACCESS_TOKEN']}"}


def full_caption(caption, hashtags):
    return caption + "\n\n" + " ".join(f"#{h}" for h in hashtags)


def build_draft(article, article_url, content, image_repo_path):
    """image_url is deliberately NOT stored: it is built from the current repo name
    when needed (a stored URL would break when the repo is renamed)."""
    return {
        "channel": CODE,
        "article_id": article["id"].rsplit("/", 1)[-1],
        "gid": article["id"],
        "handle": article["handle"],
        "title": article["title"],
        "published_at": article.get("publishedAt"),
        "article_url": article_url,
        "caption": content["caption"],
        "hashtags": content["hashtags"],
        "full_caption": full_caption(content["caption"], content["hashtags"]),
        "image_path": image_repo_path,
        "target": "Instagram feed (@maisondecocon)",
    }


def create_container(draft):
    resp = http.request(
        "POST", f"{GRAPH_BASE}/{os.environ['IG_USER_ID']}/media", "ig-create-container",
        headers=_auth(),
        data={"image_url": draft["image_url"], "caption": draft["full_caption"]},
    )
    return resp.json()["id"]


def wait_until_ready(creation_id, timeout_s=90, interval_s=3):
    waited = 0
    while waited < timeout_s:
        resp = http.request(
            "GET", f"{GRAPH_BASE}/{creation_id}", "ig-container-status",
            headers=_auth(), params={"fields": "status_code"},
        )
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Container processing failed: {resp.text}")
        time.sleep(interval_s)
        waited += interval_s
    raise TimeoutError("Container did not finish processing in time")


def verify(post_id, draft, attempts=5, interval_s=3):
    """Read the post back by id. The caption must match what we sent."""
    last = None
    for _ in range(attempts):
        try:
            resp = http.request(
                "GET", f"{GRAPH_BASE}/{post_id}", "ig-readback",
                headers=_auth(), params={"fields": "permalink,timestamp,caption"},
            )
            info = resp.json()
            if not info.get("permalink"):
                raise RuntimeError(f"Read-back has no permalink: {info}")
            sent, got = draft["full_caption"].strip()[:80], (info.get("caption") or "").strip()[:80]
            if sent != got:
                raise RuntimeError(f"Read-back caption differs from draft: {got!r} != {sent!r}")
            return info
        except http.ApiError as exc:
            last = exc
            time.sleep(interval_s)
    raise last


def publish(draft, mark_publishing, revert_publishing):
    """Returns {post_id, url, timestamp}. Raises on any failure.

    mark_publishing(container_id) must persist the `publishing` ledger state
    BEFORE media_publish is called, so a crash afterwards cannot lead to an
    automatic second post. If media_publish itself answers with an HTTP error,
    nothing was posted, so revert_publishing() puts the ledger back to `draft`.
    """
    creation_id = create_container(draft)
    wait_until_ready(creation_id)
    mark_publishing(creation_id)
    try:
        resp = http.request(
            "POST", f"{GRAPH_BASE}/{os.environ['IG_USER_ID']}/media_publish", "ig-media-publish",
            headers=_auth(), data={"creation_id": creation_id},
        )
    except http.ApiError:
        revert_publishing()
        raise
    post_id = resp.json()["id"]
    info = verify(post_id, draft)
    return {"post_id": post_id, "url": info["permalink"], "timestamp": info.get("timestamp")}


# --- bio-link redirect (Shopify) -------------------------------------------
# The Graph API cannot write the bio, so the bio points at a fixed path and
# this keeps the Shopify redirect behind that path aimed at the newest post.

def _find_redirect(path):
    data = shopify_client.graphql(
        """query($query: String!) { urlRedirects(first: 1, query: $query) { nodes { id path target } } }""",
        {"query": f"path:{path}"},
    )
    nodes = data["urlRedirects"]["nodes"]
    return nodes[0] if nodes else None


def update_bio_redirect(target_url):
    path = os.environ["BIO_LINK_PATH"]
    existing = _find_redirect(path)
    if existing:
        key, mutation, variables = "urlRedirectUpdate", """
        mutation($id: ID!, $target: String!) {
          urlRedirectUpdate(id: $id, urlRedirect: {target: $target}) {
            urlRedirect { id path target } userErrors { field message } } }""", \
            {"id": existing["id"], "target": target_url}
    else:
        key, mutation, variables = "urlRedirectCreate", """
        mutation($path: String!, $target: String!) {
          urlRedirectCreate(urlRedirect: {path: $path, target: $target}) {
            urlRedirect { id path target } userErrors { field message } } }""", \
            {"path": path, "target": target_url}
    result = shopify_client.graphql(mutation, variables)[key]
    if result["userErrors"]:
        raise RuntimeError(f"Shopify redirect error: {result['userErrors']}")
    # Read back: the redirect must now resolve to the new article.
    check = _find_redirect(path)
    if not check or check["target"] != target_url:
        raise RuntimeError(f"Redirect read-back mismatch for {path}: {check}")
    return check


def _publish_time(result):
    """When the post went live (Instagram's own timestamp; 'now' if it is missing)."""
    ts = result.get("timestamp") or ""
    for parse in (lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
                  lambda v: datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")):  # Graph API sends +0000
        try:
            return parse(ts)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def after_publish(draft, result):
    """Every successful publish repoints the bio redirect at its own article, so
    /pages/latest-post always follows the most recent successful IG publish.

    The ledger pointer (_bio_link) is claimed first, and only a publish that is
    not OLDER than the current holder may take it. That ordering guard is by
    publish time, never by article date: it only matters if two approvals run
    at the same moment, so a slower run cannot overwrite a more recent publish.
    """
    if draft.get("campaign"):
        return "Campaign post: the bio redirect is managed by the campaign schedule, not by this publish."
    published_time = _publish_time(result)
    outcome = {}

    def mutate(data):
        current = data.get(ledger.BIO_KEY)
        if current and datetime.fromisoformat(current["published_time"]) > published_time:
            outcome["lost_to"] = current
            return
        outcome.pop("lost_to", None)
        data[ledger.BIO_KEY] = {"article_id": draft["article_id"], "url": draft["article_url"],
                                "published_time": published_time.isoformat(), "updated": ledger.now()}

    data = ledger.update(mutate, f"Bio link -> {draft['article_id']} [skip ci]")
    if "lost_to" in outcome:
        cur = outcome["lost_to"]
        return (f"Bio redirect `{os.environ['BIO_LINK_PATH']}` left unchanged: a more recent publish "
                f"(article {cur['article_id']}, {cur['published_time']}) already owns it.")

    manifest = campaign.load_manifest()
    if campaign.override_active(manifest, campaign.now_local(manifest).date()):
        return (f"Bio redirect `{os.environ['BIO_LINK_PATH']}` left on the campaign link: the ledger pointer now says "
                f"article {draft['article_id']}, and the campaign schedule restores it from "
                f"{manifest['bio_link']['reset_on']}.")

    target = data[ledger.BIO_KEY]["url"]  # read after the claim: the winner's URL
    try:
        redirect = update_bio_redirect(target)
    except Exception as exc:
        raise RuntimeError(
            f"Could not set redirect {os.environ['BIO_LINK_PATH']} -> {target}: {http.redact(exc)}. "
            f"The ledger pointer already says {target}; set the redirect by hand.") from None
    return f"Bio redirect `{redirect['path']}` → {redirect['target']}"
