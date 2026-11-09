"""
Maison de Cocon — campaign runner (Black Week + Christmas 2026), run by social-campaign.yml.

Every tick (idempotent, so a late or repeated cron is harmless) does, in this order:
  1. sweep   : drafts / approved-but-unpublished posts whose day is over -> `expired`
  2. bio     : /pages/latest-post follows the campaign schedule (read back after each change)
  3. draft   : posts due today, from draft_time on, that have no ledger entry -> draft + ONE email
               (an entry whose email failed is re-emailed; the ledger flag `email_sent` prevents duplicates)
  4. publish : posts due today, from publish_time on, that were approved earlier -> publish + read back

Nothing is ever published without an approval. Manual inputs arrive only via env (validated):
  FORCE_POST   numeric post id          FORCE_ACTION  dryrun | reset
  dryrun: draft that post now with a DRY RUN banner (approving it is refused until its date)
  reset : delete a post's drafts + ledger entries (only if none is published/publishing); the
          next tick drafts it again on its day
"""

import os
import re
import subprocess
import sys

from core import approval, campaign, config, http, ledger, meta
import publish
from channels import instagram

REQUIRED_ENV = ["GITHUB_REPOSITORY", "GITHUB_REF_NAME", "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO",
                "ACCESS_TOKEN", "IG_USER_ID", "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID",
                "SHOPIFY_CLIENT_SECRET", "BIO_LINK_PATH"]
ORDER = ("ig", "fb", "story")
LIVE = (ledger.PUBLISHED, ledger.PUBLISHING)


def entries(data, pid):
    return {ch: e for ch, e in (data.get(pid) or {}).items() if isinstance(e, dict)}


# --- 1. sweep ---------------------------------------------------------------

def sweep(m, now):
    stale = []
    data = ledger.load()
    for post in m["posts"]:
        if campaign.window_state(post, m, now) != "expired":
            continue
        for ch, e in entries(data, str(post["id"])).items():
            if e.get("status") in (ledger.DRAFT, ledger.APPROVED):
                stale.append((str(post["id"]), ch))
    if not stale:
        return []

    def mutate(d):
        for pid, ch in stale:
            ledger.set_entry(d, pid, ch, status=ledger.EXPIRED, timestamp=ledger.now())
    ledger.update(mutate, "Campaign posts expired: " + ", ".join(f"{p}/{c}" for p, c in stale) + " [skip ci]")
    return stale


# --- 2. bio link ------------------------------------------------------------

def sync_bio(m, today):
    kind, path = campaign.bio_decision(m, today)
    if kind == "none":
        return "bio: nothing to do"
    if kind == "campaign":
        storefront = os.environ.get("STOREFRONT_DOMAIN", "www.maisondecocon.com")
        target = f"https://{storefront}{path}"
    else:
        pointer = ledger.load().get(ledger.BIO_KEY)
        if not pointer:
            return "bio: reset day but the ledger has no blog pointer; left unchanged"
        target = pointer["url"]
    current = instagram._find_redirect(os.environ["BIO_LINK_PATH"])
    if current and current["target"] == target:
        return f"bio: already {target}"
    redirect = instagram.update_bio_redirect(target)  # raises unless the read-back matches
    return f"bio: {redirect['path']} -> {redirect['target']} (read back)"


# --- 3. drafts --------------------------------------------------------------

def buttons_for(channels):
    if len(channels) == 2:
        return [("Approve both", "approve", "ig+fb"), ("IG only", "approve", "ig"),
                ("FB only", "approve", "fb"), ("Reject", "reject", "ig+fb")]
    return [("Approve", "approve", channels[0]), ("Reject", "reject", channels[0])]


def preview(ch, d):
    text = {"ig": d.get("full_caption"), "fb": d.get("message")}.get(ch) or "(Story: no caption)"
    return {"label": {"ig": "Instagram feed", "fb": "Facebook", "story": "Instagram Story"}[ch],
            "image_url": d["image_url"], "text": text, "target": d["target"], "link": d["article_url"]}


def note_html(m, post, dry):
    at = campaign.publish_at(post, m).strftime("%H:%M")
    base = (f"Scheduled for <strong>{post['date']} {at} Berlin</strong>. Approve before {at}: it publishes at {at}. "
            f"Approve later the same day: it publishes right away. After 23:59 that day it expires and is never published.")
    if dry:
        return ('<p style="background:#fff3cd;border:1px solid #e0c36a;padding:8px 12px;font-family:sans-serif">'
                f"<strong>DRY RUN.</strong> Looks exactly like the real email. Approving is refused until {post['date']}, "
                "and this draft is cancelled afterwards. " + base + "</p>")
    return f'<p style="background:#e8f1fb;border:1px solid #9bbbe0;padding:8px 12px;font-family:sans-serif">{base}</p>'


def pending_for_email(data, exclude):
    return [(aid, "+".join(chs), d.get("title"), d.get("handle"))
            for aid, chs, d in ledger.pending_drafts(data, exclude_article_id=exclude)
            if "+".join(chs) in ("ig", "fb", "ig+fb", "story")]


def send_email(m, post, data, dry=False):
    pid = str(post["id"])
    chans = [c for c in ORDER if c in entries(data, pid)]
    drafts = {c: ledger.read_draft(pid, c) for c in chans}
    approval.send_draft_email(
        {"title": campaign.label(post), "article_id": pid},
        previews=[preview(c, d) for c, d in drafts.items()],
        buttons=buttons_for(chans), pending=pending_for_email(data, pid),
        note=note_html(m, post, dry), subject_prefix="[DRY RUN] " if dry else "")
    return chans


def mark_emailed(pid, chans):
    def mutate(d):
        for c in chans:
            ledger.set_entry(d, pid, c, email_sent=True)
    ledger.update(mutate, f"Campaign {pid}: confirmation email sent [skip ci]")


def draft_post(m, post, dry=False):
    pid = str(post["id"])
    drafts = campaign.build_drafts(post, m)

    def mutate(d):
        for ch in drafts:
            ledger.set_entry(d, pid, ch, status=ledger.DRAFT, article_id=pid, handle=drafts[ch]["handle"],
                             campaign=m["campaign"], post_id=None, url=None, container_id=None,
                             email_sent=False, dry_run=dry, timestamp=ledger.now())
    files = {ledger.draft_path(pid, c): ledger.draft_text(d) for c, d in drafts.items()}
    # persist FIRST, email SECOND: an email can never point at content that was not saved
    data = ledger.update(mutate, f"Draft campaign post {pid} ({'+'.join(drafts)}) [skip ci]", files)
    mark_emailed(pid, send_email(m, post, data, dry))


def run_drafts(m, now):
    done = []
    if now < campaign.draft_due(m, now):
        return done
    for post in m["posts"]:
        if post.get("skip") or campaign.window_state(post, m, now) not in ("before_publish_time", "open"):
            continue
        pid = str(post["id"])
        data = ledger.load()
        have = entries(data, pid)
        try:
            if not have:
                draft_post(m, post)
                done.append(f"{pid}: drafted + emailed")
            elif any(e.get("status") == ledger.DRAFT and e.get("email_sent") is False for e in have.values()):
                mark_emailed(pid, send_email(m, post, data))  # the earlier email failed: send it (once) now
                done.append(f"{pid}: email re-sent")
        except Exception as exc:
            approval.send_failure_email(f"Campaign draft failed: {pid}", http.redact(exc))
            done.append(f"{pid}: FAILED {type(exc).__name__}")
    return done


# --- 4. publish -------------------------------------------------------------

def run_publish(m, now):
    done, failed = [], False
    for post in m["posts"]:
        pid = str(post["id"])
        if campaign.window_state(post, m, now) != "open":
            continue
        data = ledger.load()
        due = [ch for ch in ORDER if entries(data, pid).get(ch, {}).get("status") == ledger.APPROVED]
        if not due:
            continue
        meta.preflight(meta.scopes_for(due))
        for ch in due:
            try:
                line, ok = publish.do_approve(pid, ch)
                failed |= not ok
            except Exception as exc:
                failed = True
                text = http.redact(exc)
                entry = ledger.get(ledger.load(), pid, ch)
                if entry and entry.get("status") == ledger.PUBLISHING and "LEDGER ENTRY TO FIX" not in text:
                    text += "\n\n" + ledger.stuck_entry_help(pid, ch, entry)
                line = f"- `{ch}`: FAILED {text}"
                approval.send_failure_email(f"Campaign publish failed ({ch}): {pid}", text)
            done.append(f"{pid}/{ch}: {line}")
    return done, failed


# --- manual: reset ----------------------------------------------------------

def reset_post(pid):
    data = ledger.load()
    live = [c for c, e in entries(data, pid).items() if e.get("status") in LIVE]
    if live:
        raise RuntimeError(f"Post {pid} is published/publishing on {live}; not resetting.")

    def mutate(d):
        d.pop(pid, None)
    ledger.update(mutate, f"Reset campaign post {pid} [skip ci]")
    subprocess.run(["git", "rm", "-rq", "--ignore-unmatch", f"state/drafts/{pid}"], check=True)
    if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
        ledger._git("commit", "-m", f"Reset campaign post {pid}: drafts removed [skip ci]")
        ledger._git("pull", "--rebase", "origin", os.environ["GITHUB_REF_NAME"])
        ledger._git("push")
    return f"post {pid} reset (drafts + ledger entries removed)"


def main():
    config.require_env(REQUIRED_ENV)
    m = campaign.load_manifest()
    now = campaign.now_local(m)
    force_post, force_action = os.environ.get("FORCE_POST", "").strip(), os.environ.get("FORCE_ACTION", "").strip()
    if force_action:
        if force_action not in ("dryrun", "reset") or not re.fullmatch(r"[0-9]+", force_post) \
                or not campaign.find_post(m, force_post):
            print(f"Rejected manual input: action={force_action!r} post={force_post!r}")
            sys.exit(1)
        post = campaign.find_post(m, force_post)
        ledger.git_identity()
        if force_action == "reset":
            print(reset_post(force_post))
        else:
            meta.preflight(meta.scopes_for(post["channels"]))
            draft_post(m, post, dry=True)
            print(f"dry run: draft + email for {force_post} sent")
        return

    problems = []
    print(f"campaign tick at {now.isoformat(timespec='minutes')}")
    expired = sweep(m, now)
    if expired:
        print("expired:", expired)
    steps = [("bio", lambda: [sync_bio(m, now.date())]), ("draft", lambda: run_drafts(m, now))]
    for name, fn in steps:
        try:
            for line in fn():
                print(" ", line)
                problems.append("FAILED" in line)
        except Exception as exc:
            text = http.redact(exc)
            print(f"  {name} step failed: {text}")
            approval.send_failure_email(f"Campaign {name} step failed", text)
            problems.append(True)
    try:
        lines, failed = run_publish(m, now)
        for line in lines:
            print(" ", line)
        problems.append(failed)
    except Exception as exc:
        text = http.redact(exc)
        print(f"  publish step failed: {text}")
        approval.send_failure_email("Campaign publish step failed", text)
        problems.append(True)
    if any(problems):
        sys.exit(1)


if __name__ == "__main__":
    main()
