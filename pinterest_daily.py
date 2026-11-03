"""
Daily Pinterest publisher for approved (scheduled) backfill pins.

OFF until Standard access is approved (config.PINTEREST_STANDARD_ACCESS). Publishes, oldest
`publish_on` first, the scheduled pins that are due (publish_on <= today, UTC), but never more
than config.PIN_DAILY_CAP pins per day in total (pins already published today count).
Every pin goes through publish.do_approve(publish_now=True): create, read back, ledger.
A failure on one pin is a failure email; the pin stays `scheduled` and is retried the next day.
"""

import sys
from datetime import datetime, timezone

import publish
from core import approval, config, http, ledger, pinterest_auth
from channels import pinterest


def due(ledger_data, today):
    """[(publish_on, article_id)] scheduled and due, oldest first."""
    rows = [(chs["pin"].get("publish_on") or "", aid) for aid, chs in ledger_data.items()
            if not aid.startswith("_") and (chs.get("pin") or {}).get("status") == ledger.SCHEDULED
            and (chs["pin"].get("publish_on") or "") <= today]
    return sorted(rows)


def published_today(ledger_data, today):
    return sum(1 for aid, chs in ledger_data.items()
               if not aid.startswith("_") and (chs.get("pin") or {}).get("published_on") == today)


def main(today=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # log lines contain arrows; Windows consoles are cp1252
    if not config.PINTEREST_STANDARD_ACCESS:
        print("Daily Pinterest publisher is OFF (waiting for Standard access).")
        return
    config.require_env(publish.REQUIRED_ENV + pinterest_auth.REQUIRED_ENV)
    today = today or datetime.now(timezone.utc).date().isoformat()
    data = ledger.load()
    budget = config.PIN_DAILY_CAP - published_today(data, today)
    queue = due(data, today)[:max(budget, 0)]
    if not queue:
        print(f"Nothing to publish today (cap {config.PIN_DAILY_CAP}, already {published_today(data, today)}).")
        return
    try:
        pinterest_auth.preflight()
    except pinterest_auth.PinterestAuthError as exc:
        approval.send_failure_email("Pinterest daily publish: token check failed", http.redact(exc))
        sys.exit(1)

    failures = 0
    for publish_on, aid in queue:
        try:
            line, ok = publish.do_approve(aid, pinterest.CODE, publish_now=True)
            print(f"{aid}: {line}")
            failures += not ok
        except Exception as exc:
            failures += 1
            text = http.redact(exc)
            print(f"{aid}: FAILED {text}")
            entry = ledger.get(ledger.load(), aid, pinterest.CODE) or {}
            if entry.get("status") == ledger.PUBLISHING:
                text += "\n\n" + ledger.stuck_entry_help(aid, pinterest.CODE, entry)
            approval.send_failure_email(f"Pinterest daily publish failed: {aid}", text)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
