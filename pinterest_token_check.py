"""
Weekly Pinterest token keepalive: refreshes the token (which also resets the refresh-token
clock and stores a rotated token), checks the granted scopes, and emails 30 days before the
refresh token expires. A failed refresh is a failure email. No manual renewals.

Skips quietly if Pinterest is not configured yet (before the one-time OAuth).
"""

import os
import sys

from core import approval, config, http, pinterest_auth


def main():
    if not os.environ.get("PINTEREST_REFRESH_TOKEN"):
        print("Pinterest is not configured yet (no PINTEREST_REFRESH_TOKEN): nothing to check.")
        return
    config.require_env(["RESEND_API_KEY", "RESEND_FROM", "RESEND_TO", "GITHUB_REPOSITORY", "GITHUB_REF_NAME"])
    try:
        pinterest_auth.preflight()
    except pinterest_auth.PinterestAuthError as exc:
        text = http.redact(exc)
        print(text)
        approval.send_failure_email("Pinterest token check failed", text)
        sys.exit(1)
    left = pinterest_auth.days_left(pinterest_auth._cache["state"])
    print(f"Pinterest refresh token OK, about {left:.0f} days left." if left is not None else "Pinterest token OK.")


if __name__ == "__main__":
    main()
