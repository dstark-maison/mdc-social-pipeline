"""Pinterest OAuth tokens: the pipeline refreshes access tokens itself.

* Access token (30 days): fetched fresh from the refresh token at the start of any
  Pinterest run, held in memory only, never stored.
* Refresh token (continuous, 60 days, "refreshable indefinitely"): bootstrapped ONCE
  from the secret PINTEREST_REFRESH_TOKEN (one-time OAuth, tools/pinterest_oauth.py).
  If Pinterest returns a different refresh token on refresh, the new one is stored
  ENCRYPTED (Fernet, key = secret PINTEREST_STATE_KEY) in state/pinterest_token.json,
  because a workflow cannot rewrite a repo secret. The file also keeps the expiry date.
  If the secret is replaced (a fresh OAuth), its fingerprint no longer matches and the
  new secret wins.
* Email 30 days before the refresh token expires (core.config.PIN_REFRESH_WARN_DAYS),
  at most once a week. A failed refresh is a failure email, never silent.

Required env: PINTEREST_APP_ID, PINTEREST_APP_SECRET, PINTEREST_REFRESH_TOKEN,
PINTEREST_STATE_KEY. Optional: PINTEREST_API_BASE (default https://api.pinterest.com).
"""

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import approval, config, http, ledger

STATE_PATH = Path("state/pinterest_token.json")
REQUIRED_SCOPES = ["boards:read", "pins:read", "pins:write", "user_accounts:read"]
REQUIRED_ENV = ["PINTEREST_APP_ID", "PINTEREST_APP_SECRET", "PINTEREST_REFRESH_TOKEN", "PINTEREST_STATE_KEY"]
WARN_EVERY = timedelta(days=7)
PERSIST_IF_EXPIRY_MOVED = timedelta(days=3)


class PinterestAuthError(RuntimeError):
    pass


def api_base():
    return os.environ.get("PINTEREST_API_BASE") or "https://api.pinterest.com"


def now():
    return datetime.now(timezone.utc)


def _fernet():
    from cryptography.fernet import Fernet  # imported lazily: only Pinterest runs need it
    return Fernet(os.environ["PINTEREST_STATE_KEY"].encode())


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def load_state():
    return json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}


def current_refresh_token(state=None):
    """The newest known refresh token: the stored (rotated) one, unless the secret was replaced."""
    state = load_state() if state is None else state
    secret = os.environ["PINTEREST_REFRESH_TOKEN"]
    if state.get("refresh_token_enc") and state.get("secret_fingerprint") == fingerprint(secret):
        try:
            return _fernet().decrypt(state["refresh_token_enc"].encode()).decode()
        except Exception as exc:  # wrong key / corrupted file: fall back to the secret
            print(f"  (stored Pinterest token unreadable, using the secret: {type(exc).__name__})")
    return secret


def _basic_auth():
    raw = f"{os.environ['PINTEREST_APP_ID']}:{os.environ['PINTEREST_APP_SECRET']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Content-Type": "application/x-www-form-urlencoded"}


def _expiry(data):
    if data.get("refresh_token_expires_at"):
        return datetime.fromtimestamp(int(data["refresh_token_expires_at"]), timezone.utc)
    if data.get("refresh_token_expires_in"):
        return now() + timedelta(seconds=int(data["refresh_token_expires_in"]))
    return None


def _persist(state, used_token, data):
    """Store a rotated refresh token and/or a moved expiry. Returns the new state."""
    new = dict(state)
    returned = data.get("refresh_token") or used_token
    expiry = _expiry(data)
    rotated = returned != used_token
    secret_fp = fingerprint(os.environ["PINTEREST_REFRESH_TOKEN"])
    old_expiry = datetime.fromisoformat(state["refresh_expires_at"]) if state.get("refresh_expires_at") else None
    moved = bool(expiry and (not old_expiry or expiry - old_expiry > PERSIST_IF_EXPIRY_MOVED))
    if rotated:
        new["refresh_token_enc"] = _fernet().encrypt(returned.encode()).decode()
        new["secret_fingerprint"] = secret_fp
        http.register_secret(returned)
    if expiry:
        new["refresh_expires_at"] = expiry.isoformat(timespec="seconds")
    new["refreshed_at"] = now().isoformat(timespec="seconds")
    new["scope"] = data.get("scope")
    if rotated or moved or not state:
        ledger.update(lambda d: None, "Pinterest token state [skip ci]",
                      {STATE_PATH: json.dumps(new, indent=2, sort_keys=True) + "\n"})
    return new


def refresh():
    """Refresh the access token. Returns (access_token, state). Raises PinterestAuthError."""
    missing = [n for n in REQUIRED_ENV if not os.environ.get(n)]
    if missing:
        raise PinterestAuthError(f"Pinterest is not configured, missing: {', '.join(missing)}")
    state = load_state()
    used = current_refresh_token(state)
    http.register_secret(used)
    try:
        resp = http.request("POST", f"{api_base()}/v5/oauth/token", "pinterest-token",
                            headers=_basic_auth(), data={"grant_type": "refresh_token", "refresh_token": used})
    except http.ApiError as exc:
        raise PinterestAuthError(f"Pinterest token refresh failed (re-run the one-time OAuth if the refresh "
                                 f"token expired or was revoked): {exc}") from None
    data = resp.json()
    http.register_secret(data.get("access_token"))
    granted = set(str(data.get("scope", "")).replace(",", " ").split())
    missing_scopes = [s for s in REQUIRED_SCOPES if s not in granted]
    if missing_scopes:
        raise PinterestAuthError("Pinterest token is missing required scope(s): " + ", ".join(missing_scopes)
                                 + f". Granted: {', '.join(sorted(granted)) or 'none'}.")
    state = _persist(state, used, data)
    return data["access_token"], state


_cache = {}


def access_token():
    """Access token for this process (one refresh per run)."""
    if "token" not in _cache:
        _cache["token"], _cache["state"] = refresh()
        print("  Pinterest token OK (scopes: " + ", ".join(REQUIRED_SCOPES) + ")")
    return _cache["token"]


def preflight():
    """Start-of-run check (same role as core.meta.preflight): refresh works, scopes granted,
    and warn by email if the refresh token is close to expiry."""
    access_token()
    maybe_warn_expiry(_cache["state"])


def days_left(state):
    if not state.get("refresh_expires_at"):
        return None
    return (datetime.fromisoformat(state["refresh_expires_at"]) - now()).total_seconds() / 86400


def maybe_warn_expiry(state):
    left = days_left(state)
    if left is None or left >= config.PIN_REFRESH_WARN_DAYS:
        return False
    warned = state.get("warned_at")
    if warned and now() - datetime.fromisoformat(warned) < WARN_EVERY:
        return False
    approval.send_failure_email(
        f"Pinterest refresh token expires in {max(left, 0):.0f} days ({state['refresh_expires_at']})",
        "Pinterest refreshes the token on every run, so it only runs out if refreshes stop extending it. "
        "Re-run the one-time OAuth (tools/pinterest_oauth.py, see README) and replace the secret "
        "PINTEREST_REFRESH_TOKEN before that date.")
    state = {**state, "warned_at": now().isoformat(timespec="seconds")}
    ledger.update(lambda d: None, "Pinterest token expiry warning sent [skip ci]",
                  {STATE_PATH: json.dumps(state, indent=2, sort_keys=True) + "\n"})
    return True
