"""Environment handling."""

import os
import sys


def require_env(names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def public_raw_url(repo_path):
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["GITHUB_REF_NAME"]
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{repo_path}"


# --- Reddit (r/MaisonDeCocon) ---------------------------------------------------
REDDIT_SUBREDDIT = "MaisonDeCocon"
REDDIT_DRAFTS_PER_WEEK = 5      # max new drafts per run
REDDIT_MAX_OPEN = 5             # weekly run tops up to this many UNPOSTED drafts (pending + approved-not-posted)
REDDIT_MIN_IMPRESSIONS = 3      # Search Console question queries below this are noise (tiny site)
REDDIT_MAX_FROM_GSC = 3         # slots: GSC first, then the pasted findquestions list, then blog FAQ blocks
REDDIT_MAX_FROM_PASTED = 2
REDDIT_POOL_MAX = 60            # unused pasted questions carried over to later weeks

# --- draft selection ---------------------------------------------------------
# The scheduled draft job only drafts articles PUBLISHED AFTER this instant
# (UTC, set to the PR merge time). Anything older is drafted only on request:
# Actions -> Social Draft -> Run workflow -> article_id.
DRAFT_CUTOFF = "2026-10-31T13:54:28+00:00"

# Articles that must not be drafted by the cron OR on request, until their
# claims are fixed. {numeric article id: reason}. Remove an entry to release it.
HOLD_ARTICLE_IDS = {
    "751689728333": "bamboo-tencel-down-pillow: claims about down pillows; catalog has no down products",
    "751536243021": "bedding-certification-meaning-explained: Down Pass section; catalog has no down products",
}
