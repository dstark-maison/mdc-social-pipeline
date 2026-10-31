"""
Pinterest backfill: every existing blog article (not on the hold list) becomes a pin on
its mapped board, at most PIN_DAILY_CAP per day, with ONE approval email per week.

OFF until Pinterest approves Standard access: config.PINTEREST_STANDARD_ACCESS must be
True, otherwise this exits immediately (Trial pins are only visible to the app owner).

Weekly run (Mondays):
  * skips while a previous batch still has draft/scheduled pins (no piling up)
  * picks up to PIN_WEEKLY_MAX (=3/day x 7) published, un-held articles that have no pin
    ledger entry yet, newest first
  * builds each pin (German copy, topic board, 1000x1500 image) and saves ALL drafts in one
    commit with status `draft`, `batch` = the run date (YYYYMMDD) and `publish_on` = the day
    it will go out (3 per day, starting tomorrow)
  * sends ONE email: Approve all / Reject all, or Approve / Reject per pin
Approving only SCHEDULES a pin; pinterest_daily.py publishes it on its day.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from core import approval, blog, config, ledger, pin_draft, pinterest_auth
from channels import pinterest

BASE_ENV = ["SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SHOPIFY_BLOG_HANDLE",
            "ANTHROPIC_API_KEY", "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO",
            "GITHUB_REPOSITORY", "GITHUB_REF_NAME"]


def open_backfill_pins(ledger_data):
    """Backfill pins still waiting (draft or scheduled)."""
    return [aid for aid, chs in ledger_data.items()
            if not aid.startswith("_") and (chs.get("pin") or {}).get("batch")
            and chs["pin"].get("status") in (ledger.DRAFT, ledger.SCHEDULED)]


def select(articles, ledger_data, hold=None, limit=config.PIN_WEEKLY_MAX):
    """Published, un-held articles with no pin entry, newest first."""
    hold = config.HOLD_ARTICLE_IDS if hold is None else hold
    picked = [a for a in articles
              if a["isPublished"] and a.get("publishedAt")
              and ledger.numeric_id(a["id"]) not in hold
              and not (ledger_data.get(ledger.numeric_id(a["id"])) or {}).get("pin")]
    picked.sort(key=lambda a: blog.parse_ts(a["publishedAt"]), reverse=True)
    return picked[:limit]


def assign_days(count, first_day, per_day=config.PIN_DAILY_CAP):
    """publish_on (YYYY-MM-DD) for count pins, per_day per day, starting at first_day."""
    return [(first_day + timedelta(days=i // per_day)).isoformat() for i in range(count)]


def main(today=None):
    if not config.PINTEREST_STANDARD_ACCESS:
        print("Pinterest backfill is OFF (config.PINTEREST_STANDARD_ACCESS is False: waiting for Standard access).")
        return
    config.require_env(BASE_ENV + pinterest_auth.REQUIRED_ENV)
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    today = today or datetime.now(timezone.utc).date()
    batch = today.strftime("%Y%m%d")

    try:
        pinterest_auth.preflight()
        data = ledger.load()
        if open_backfill_pins(data):
            print(f"A previous backfill batch still has {len(open_backfill_pins(data))} draft/scheduled pin(s); "
                  "not creating a new one.")
            return
        articles = select(blog.fetch_all_articles(os.environ["SHOPIFY_BLOG_HANDLE"]), data)
    except Exception as exc:
        print(f"Cannot start: {exc}")
        approval.send_failure_email("Pinterest backfill not started", str(exc))
        sys.exit(1)
    if not articles:
        print("Nothing left to backfill.")
        return

    days = assign_days(len(articles), today + timedelta(days=1))
    built, failed = [], []
    for article, publish_on in zip(articles, days):
        try:
            draft, path = pin_draft.build(article, model)
            built.append((article, draft, path, publish_on))
        except Exception as exc:
            print(f"  FAILED '{article['title']}': {exc}")
            failed.append((article["title"], str(exc)))
    if failed:
        approval.send_failure_email(
            f"Pinterest backfill: {len(failed)} article(s) skipped",
            "\n".join(f"{t}: {e}" for t, e in failed))
    if not built:
        sys.exit(1)

    ledger.commit_and_push([p for _, _, p, _ in built], f"Pinterest backfill {batch}: pin images [skip ci]")

    def mutate(d):
        for article, draft, _, publish_on in built:
            ledger.set_entry(d, ledger.numeric_id(article["id"]), pinterest.CODE,
                             status=ledger.DRAFT, article_id=ledger.numeric_id(article["id"]),
                             handle=article["handle"], post_id=None, url=None, container_id=None,
                             batch=batch, publish_on=publish_on, timestamp=ledger.now())

    files = {ledger.draft_path(ledger.numeric_id(a["id"]), pinterest.CODE): ledger.draft_text(d)
             for a, d, _, _ in built}
    ledger.update(mutate, f"Pinterest backfill {batch}: {len(built)} drafts [skip ci]", files)

    approval.send_pin_batch_email(
        batch,
        [{"article_id": ledger.numeric_id(a["id"]), "draft": d, "publish_on": po} for a, d, _, po in built],
        lambda d: config.public_raw_url(d["image_path"]),
    )
    print(f"Backfill batch {batch}: {len(built)} pins drafted, approval email sent.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
