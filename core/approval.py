"""Approval email (Resend) and the issue-based Approve / Reject buttons.

Email buttons are plain links, so they cannot call the workflow_dispatch API.
Each button opens a prefilled GitHub issue (label "approval", title
"<action>:<handle>:<channels>"); submitting it triggers social-publish.yml,
which only trusts OWNER/COLLABORATOR authors.
"""

import html
import os
import re
from urllib.parse import quote, urlencode

from core import config, http

# The ONLY accepted shape of an approval title. fullmatch(), never match():
# `$` would accept a trailing newline.
ACTION_RE = re.compile(r"(approve|reject):[0-9]+:(ig|fb|ig\+fb|pin|reddit)")


def approval_link(action, article_id, channels):
    """article_id is the NUMERIC Shopify id (handles get renamed; ids do not)."""
    title = f"{action}:{article_id}:{channels}"
    if not ACTION_RE.fullmatch(title):
        raise ValueError(f"Approval title would be rejected by the workflow: {title!r}")
    query = urlencode(
        {
            "labels": "approval",
            "title": title,
            "body": "Submit this issue to confirm. Do not edit the title.",
        },
        quote_via=quote,
    )
    return f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/issues/new?{query}"


def _send(subject, body_html):
    http.request(
        "POST", "https://api.resend.com/emails", "resend",
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "from": os.environ["RESEND_FROM"],
            "to": [os.environ["RESEND_TO"]],
            "subject": subject,
            "html": body_html,
        },
    )


def _button(label, url, color):
    return (
        f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;'
        f'padding:10px 18px;margin:4px 6px 4px 0;background:{color};color:#fff;'
        f'text-decoration:none;font-family:sans-serif">{html.escape(label)}</a>'
    )


def pending_section(pending):
    """pending: [(article_id, channels "ig" / "ig+fb", title, handle)] -> HTML list with Approve links."""
    if not pending:
        return ""
    e = html.escape
    items = "".join(
        f"<li>{e(title or handle or aid)} <span style='color:#666'>({e(handle or aid)}, "
        f"{e(channels.replace('+', ' + ').upper())})</span> "
        f"{_button('Approve both' if channels == 'ig+fb' else 'Approve ' + channels.upper(), approval_link('approve', aid, channels), '#2e6b3a')}</li>"
        for aid, channels, title, handle in pending
    )
    return (f"<hr><h3>Pending approvals ({len(pending)})</h3>"
            f"<p style='color:#666;font-size:12px'>Other drafts still waiting. They stay approvable; "
            f"newer drafts never replace them.</p><ul>{items}</ul>")


def send_draft_email(article, previews, buttons, pending=None):
    """previews: [{label, image_url, text, target, link}], buttons: [(label, action, channels)],
    pending: see pending_section()."""
    e = html.escape
    blocks = "".join(
        f"""
        <h3>{e(p['label'])}</h3>
        <p><img src="{e(p['image_url'], quote=True)}" style="max-width:400px" /></p>
        <p style="white-space:pre-wrap">{e(p['text'])}</p>
        <p style="color:#666">Target: {e(p['target'])}<br>Link: {e(p['link'])}</p>
        """
        for p in previews
    )
    btns = "".join(
        _button(label, approval_link(action, article["article_id"], channels),
                "#2e6b3a" if action == "approve" else "#8a2f2f")
        for label, action, channels in buttons
    )
    body = f"""
    <h2>Review: {e(article['title'])}</h2>
    {blocks}
    <hr>
    <p>{btns}</p>
    <p style="color:#666;font-size:12px">A button opens a prefilled GitHub issue.
    Click "Submit new issue" to confirm (you must be signed in as a repo
    collaborator). Nothing publishes on its own.</p>
    {pending_section(pending)}
    """
    _send(f"Review + Approve: {article['title']}", body)


def send_failure_email(context, error_text):
    e = html.escape
    _send(
        f"[FAILED] {context}",
        f"<h2>Social pipeline failure</h2><p><strong>{e(context)}</strong></p>"
        f"<pre style='white-space:pre-wrap'>{e(http.redact(error_text))}</pre>",
    )


# --- Reddit emails ------------------------------------------------------------

def _source_line(d):
    sig = d.get("signals") or {}
    if d["source"] == "gsc":
        return (f"Search Console (last 7 days): {sig.get('impressions')} impressions, {sig.get('clicks')} clicks, "
                f"CTR {sig.get('ctr', 0):.1%}, average position {sig.get('position', 0):.1f}")
    if d["source"] == "findquestions":
        return "findquestions list pasted by you"
    return "FAQ block of the matching blog article"


def send_reddit_draft_email(d, redraft=False):
    """One review email per Reddit draft (Approve / Reject). d is the draft dict (see reddit_draft.py)."""
    e = html.escape
    note = ('<p style="background:#e8f1fb;border:1px solid #9bbbe0;padding:8px 12px;font-family:sans-serif">'
            "<strong>Re-draft</strong> with the updated prompt. It replaces the earlier version of this draft; "
            "the Approve link is the same.</p>") if redraft else ""
    flags = "".join(f"<li>{e(f)}</li>" for f in d["flags"])
    flag_block = (f'<div style="background:#fff3cd;border:1px solid #e0c36a;padding:10px 14px;margin:12px 0">'
                  f'<strong>Check before approving</strong><ul style="margin:6px 0 0">{flags}</ul></div>') if flags else (
        '<p style="color:#2e6b3a">No flags: every number and claim traces to the article.</p>')
    link = d.get("link")
    btns = (_button("Approve", approval_link("approve", d["id"], "reddit"), "#2e6b3a")
            + _button("Reject", approval_link("reject", d["id"], "reddit"), "#8a2f2f"))
    body = f"""
    {note}
    <h2>Reddit draft: {e(d['question'])}</h2>
    <p style="color:#666;font-family:sans-serif">Language: <strong>{e(d['lang'].upper())}</strong> &middot;
       Source: {e(_source_line(d))}<br>
       Matching article: {e(d.get('article_title') or 'none')} {('&middot; ' + e(link)) if link else ''}</p>
    {flag_block}
    <h3>Post title</h3>
    <p style="font-size:16px"><strong>{e(d['title'])}</strong></p>
    <h3>Post body</h3>
    <div style="white-space:pre-wrap;border:1px solid #ddd;padding:12px 16px;background:#faf9f7">{e(d['body'])}</div>
    <hr><p>{btns}</p>
    <p style="color:#666;font-size:12px">Approve sends you a copy-ready email with a suggested posting day. Nothing is posted
    for you: Reddit's API is closed, so you post it by hand to r/{e(config.REDDIT_SUBREDDIT)} and send the URL back.</p>
    """
    _send(f"Reddit {'RE-DRAFT' if redraft else 'draft'} ({d['lang'].upper()}): {d['question'][:80]}", body)


def send_reddit_ready_email(d, post_on, weekday):
    """After Approve: title + body ready to copy, the suggested day, how to report the URL."""
    e = html.escape
    from core import reddit_copy  # lazy: keeps approval importable on its own
    repo = os.environ["GITHUB_REPOSITORY"]
    if not reddit_copy.needs_disclosure():  # older drafts may still carry the disclosure line: never in our own subreddit
        d = {**d, "body": reddit_copy.strip_disclosure(d["body"])}
    body = f"""
    <h2>Ready to post: r/{e(config.REDDIT_SUBREDDIT)}</h2>
    <p style="font-family:sans-serif"><strong>Suggested posting day: {e(weekday)} {e(post_on)}</strong>
       (one Reddit post per day). Language: {e(d['lang'].upper())}.</p>
    <h3>Title (copy)</h3>
    <div style="white-space:pre-wrap;border:1px solid #ddd;padding:10px 14px;background:#faf9f7;font-size:16px">{e(d['title'])}</div>
    <h3>Body (copy)</h3>
    <div style="white-space:pre-wrap;border:1px solid #ddd;padding:12px 16px;background:#faf9f7">{e(d['body'])}</div>
    <hr>
    <p style="font-family:sans-serif"><strong>After you posted it</strong>, send the URL back:
    <a href="https://github.com/{e(repo)}/actions/workflows/social-publish.yml">Actions &rarr; Social Publish &rarr; Run workflow</a>
    with <code>article</code> = <code>{e(d['id'])}</code>, <code>channel</code> = <code>reddit</code>,
    <code>url</code> = the post URL (https://www.reddit.com/r/{e(config.REDDIT_SUBREDDIT)}/comments/...).
    The ledger then marks it published.</p>
    """
    _send(f"Ready to post ({weekday} {post_on}): {d['title'][:80]}", body)


def send_reddit_skip_email(pending, approved, limit):
    """Short note when the weekly Reddit run is skipped because the open list is already full."""
    e = html.escape
    _send(
        f"Reddit weekly run skipped: {len(pending) + len(approved)} of {limit} drafts still open",
        f"<h2>Reddit: no new drafts this week</h2>"
        f"<p style='font-family:sans-serif'>{len(pending) + len(approved)} unposted drafts are already open "
        f"(limit {limit}): <strong>{len(pending)} pending approval</strong>, "
        f"<strong>{len(approved)} approved but not posted yet</strong>.</p>"
        f"<p style='font-family:sans-serif'>Pending: {e(', '.join(pending) or 'none')}<br>"
        f"Approved: {e(', '.join(approved) or 'none')}</p>"
        f"<p style='color:#666;font-family:sans-serif'>A slot frees up when you reject a draft or post an approved one "
        f"(send its URL via Social Publish). The next Monday run tops the list up again.</p>",
    )


def send_reddit_published_email(url, info):
    """The ONE short confirmation after the owner submitted the URL of a post."""
    e = html.escape
    sub = config.REDDIT_SUBREDDIT
    article = ""
    if info.get("article_title"):
        article = (f"<p style='font-family:sans-serif'>Article: <a href=\"{e(info.get('article_link') or '#', quote=True)}\">"
                   f"{e(info['article_title'])}</a></p>")
    _send(
        f"Published on r/{sub}",
        f"<h2>Published on r/{e(sub)}</h2>"
        f"<p style='font-family:sans-serif'><a href=\"{e(url, quote=True)}\">{e(url)}</a></p>{article}"
        f"<p style='color:#666;font-size:12px;font-family:sans-serif'>Recorded in the ledger from the URL you sent "
        f"(Reddit cannot be read back).</p>",
    )
