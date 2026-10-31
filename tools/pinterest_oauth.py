"""
ONE-TIME Pinterest OAuth helper. The app secret lives only in GitHub secrets, so the code
exchange runs inside GitHub Actions; its output (a refresh token) must not appear in the
(public) logs, so it is encrypted to an RSA public key whose private half never leaves this
machine.

  local   keygen   --key private.pem            -> prints the PUBLIC key (PEM)
  actions url                                    -> prints the authorize URL (encrypted)
  actions exchange                               -> prints {refresh_token, scope, ...} (encrypted)
  local   decrypt  --key private.pem [--field refresh_token] < encrypted.txt

Env (actions): PINTEREST_APP_ID, PINTEREST_APP_SECRET, PINTEREST_REDIRECT_URI,
               PINTEREST_OAUTH_PUBKEY (PEM), PINTEREST_OAUTH_CODE (exchange only)
"""

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

SCOPES = ["boards:read", "pins:read", "pins:write", "user_accounts:read"]
AUTHORIZE = "https://www.pinterest.com/oauth/"
TOKEN = "https://api.pinterest.com/v5/oauth/token"
BEGIN, END = "===ENCRYPTED===", "===END==="


def _oaep():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def encrypt(text, pubkey_pem):
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_public_key(pubkey_pem.encode())
    return base64.b64encode(key.encrypt(text.encode(), _oaep())).decode()


def decrypt(b64, private_pem):
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return key.decrypt(base64.b64decode(b64), _oaep()).decode()


def emit(text):
    print(BEGIN)
    print(encrypt(text, os.environ["PINTEREST_OAUTH_PUBKEY"]))
    print(END)


def cmd_keygen(args):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    Path(args.key).write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    print(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())


def cmd_url(args):
    state = secrets.token_urlsafe(12)
    query = urlencode({"client_id": os.environ["PINTEREST_APP_ID"], "redirect_uri": os.environ["PINTEREST_REDIRECT_URI"],
                       "response_type": "code", "scope": ",".join(SCOPES), "state": state}, quote_via=quote)
    emit(json.dumps({"url": f"{AUTHORIZE}?{query}", "state": state}))


def cmd_exchange(args):
    import requests
    resp = requests.post(
        TOKEN, auth=(os.environ["PINTEREST_APP_ID"], os.environ["PINTEREST_APP_SECRET"]),
        data={"grant_type": "authorization_code", "code": os.environ["PINTEREST_OAUTH_CODE"],
              "redirect_uri": os.environ["PINTEREST_REDIRECT_URI"]}, timeout=60)
    if not resp.ok:
        # Pinterest's error body names the problem (expired/used code, redirect mismatch, ...); no secrets in it.
        print(f"TOKEN EXCHANGE FAILED [{resp.status_code}]: {resp.text}")
        sys.exit(1)
    d = resp.json()
    granted = str(d.get("scope", "")).replace(",", " ").split()
    missing = [s for s in SCOPES if s not in granted]
    print("exchange OK; granted scopes:", " ".join(granted) or "(none reported)", "| missing:", missing or "none",
          "| refresh_token present:", bool(d.get("refresh_token")), "| refresh_token_expires_in:", d.get("refresh_token_expires_in"))
    emit(json.dumps({k: d.get(k) for k in ("refresh_token", "scope", "refresh_token_expires_in",
                                           "refresh_token_expires_at", "expires_in", "token_type")}))


def cmd_decrypt(args):
    private = Path(args.key).read_text()
    blob = sys.stdin.read()
    b64 = blob.split(BEGIN)[1].split(END)[0].strip() if BEGIN in blob else blob.strip()
    plain = decrypt(b64, private)
    if args.field:
        sys.stdout.write(str(json.loads(plain)[args.field]))  # no trailing newline: piped into `gh secret set`
    else:
        print(plain)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--key", required=True); k.set_defaults(fn=cmd_keygen)
    sub.add_parser("url").set_defaults(fn=cmd_url)
    sub.add_parser("exchange").set_defaults(fn=cmd_exchange)
    d = sub.add_parser("decrypt"); d.add_argument("--key", required=True); d.add_argument("--field")
    d.set_defaults(fn=cmd_decrypt)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
