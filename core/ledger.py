"""State: drafts, ledger, and the git plumbing that persists them.

state/ledger.json:  {article_id: {channel: {status, post_id, url, timestamp, ...}}}
state/drafts/{article_id}/{channel}.json:  copy + image + target + link (handle = display only)

Statuses:
  draft       copy saved, nothing on the platform
  publishing  media_publish was called but not yet verified. Blocks automatic
              retries: re-running could double-post. Needs a manual check.
  published   posted AND read back by id
  rejected    rejected in the approval step
  paused      (Reddit only) parked while the Reddit channel is paused; not published, not rejected
  approved    (Reddit only) approved; the owner posts it by hand and submits the URL -> published
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path("state/ledger.json")
DRAFTS_DIR = Path("state/drafts")

DRAFT, PUBLISHING, PUBLISHED, REJECTED = "draft", "publishing", "published", "rejected"
PAUSED = "paused"      # Reddit only: parked while the channel is paused; neither posted nor rejected
APPROVED = "approved"  # Reddit only: approved, waiting for the owner to post it manually and send the URL back
PLATFORM_NAMES = {"ig": "the Instagram feed", "fb": "the Facebook Page", "reddit": "r/MaisonDeCocon"}
CHANNEL_ORDER = ("ig", "fb")


def numeric_id(gid):
    """'gid://shopify/Article/752115122509' -> '752115122509'. Ledger, drafts and
    approval titles are all keyed by this, never by the (renameable) handle."""
    return str(gid).rsplit("/", 1)[-1]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    return json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}


def save(ledger):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def get(ledger, article_id, channel):
    return ledger.get(article_id, {}).get(channel)


def set_entry(ledger, key, channel, **fields):
    """key = numeric article id. (Not named article_id: entries carry an `article_id` field.)"""
    entry = ledger.setdefault(key, {}).setdefault(channel, {})
    entry.update(fields)
    return entry


def draft_path(article_id, channel):
    return DRAFTS_DIR / article_id / f"{channel}.json"


def write_draft(article_id, channel, draft):
    path = draft_path(article_id, channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n")
    return path


def read_draft(article_id, channel):
    path = draft_path(article_id, channel)
    if not path.exists():
        raise FileNotFoundError(f"No draft at {path}")
    return json.loads(path.read_text())


def stuck_entry_help(article_id, channel, entry):
    """Exact, copy-pasteable instructions for a ledger entry stuck in `publishing`."""
    keep = {k: entry[k] for k in ("article_id", "handle") if k in entry}
    live = {**keep, "status": PUBLISHED, "post_id": "<media id from Instagram>",
            "url": "<permalink of the post>", "timestamp": "<ISO time of the post>"}
    not_live = {**keep, "status": DRAFT, "post_id": None, "url": None, "container_id": None,
                "timestamp": now()}
    dump = lambda o: json.dumps(o, indent=2, ensure_ascii=False)
    return (
        f'LEDGER ENTRY TO FIX BY HAND: state/ledger.json -> "{article_id}" -> "{channel}"\n\n'
        f"Current entry (stuck, blocks automatic retries):\n{dump(entry)}\n\n"
        f"1. Check {PLATFORM_NAMES.get(channel, channel)} for this article.\n\n"
        f"2a. Post IS live -> replace the entry with:\n{dump(live)}\n\n"
        f"2b. Post is NOT live -> replace the entry with:\n{dump(not_live)}\n\n"
        f"Commit the change, then (2b only) re-run Social Publish: article={article_id}, channel={channel}."
    )


# --- git persistence -------------------------------------------------------

def _git(*args, check=True):
    return subprocess.run(["git", *args], check=check)


def git_identity():
    _git("config", "user.name", "social-pipeline-bot")
    _git("config", "user.email", "actions@users.noreply.github.com")


def commit_and_push(paths, message):
    """Commit + push specific paths (used for images, which must be public
    before anything can fetch them)."""
    git_identity()
    _git("add", *[str(p) for p in paths])
    if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
        return
    _git("commit", "-m", message)
    _git("pull", "--rebase", "origin", os.environ["GITHUB_REF_NAME"])
    _git("push")


def draft_text(draft):
    return json.dumps(draft, indent=2, ensure_ascii=False) + "\n"


def update(mutator, message, files=None, attempts=4):
    """Apply mutator(ledger) and persist the ledger plus `files` ({Path: text}).

    Everything is written AFTER syncing with origin, never before: a tracked
    file rewritten before `git pull --rebase` (a re-draft) would make the
    rebase refuse to run. Concurrent runs edit the same JSON, so on a rejected
    push we drop our local commit, re-sync and re-apply (CI only, where the
    checkout is disposable).
    """
    files = files or {}
    branch = os.environ["GITHUB_REF_NAME"]
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    git_identity()
    last = None
    for attempt in range(1, attempts + 1):
        _git("pull", "--rebase", "origin", branch)
        ledger = load()
        mutator(ledger)
        save(ledger)
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git("add", str(LEDGER_PATH), *[str(p) for p in files])
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return ledger
        _git("commit", "-m", message)
        push = subprocess.run(["git", "push"])
        if push.returncode == 0:
            return ledger
        last = push.returncode
        print(f"  push rejected (attempt {attempt}/{attempts}), re-syncing")
        if not in_ci:
            break
        _git("fetch", "origin", branch)
        _git("reset", "--hard", f"origin/{branch}")
    raise RuntimeError(f"Could not push ledger update after {attempts} attempts (git exit {last})")


def pending_drafts(ledger_data, exclude_article_id=None):
    """[(article_id, [channels], draft)] for every article with a draft-status
    entry, except exclude_article_id. Oldest first."""
    out = []
    for key, channels in ledger_data.items():
        if key.startswith("_") or key == exclude_article_id:
            continue
        pending = [c for c in CHANNEL_ORDER if channels.get(c, {}).get("status") == DRAFT]
        if not pending:
            continue
        try:
            draft = read_draft(key, pending[0])
        except FileNotFoundError:
            draft = {}
        out.append((key, pending, draft))
    return sorted(out, key=lambda t: min((channels_ts(ledger_data[t[0]], c) for c in t[1]), default=""))


def channels_ts(channels, channel):
    return channels.get(channel, {}).get("timestamp") or ""


BIO_KEY = "_bio_link"  # reserved ledger key: {article_id, url, published_time}; the most recent IG publish owns /pages/latest-post
