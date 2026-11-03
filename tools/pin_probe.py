import os, sys
import requests
from core import http, pinterest_auth

token, state = pinterest_auth.refresh()
print("refresh OK; refresh_expires_at:", state.get("refresh_expires_at"), "| rotated token stored:", bool(state.get("refresh_token_enc")))
for base in ("https://api.pinterest.com", "https://api-sandbox.pinterest.com"):
    h = {"Authorization": "Bearer " + token}
    for path in ("/v5/user_account", "/v5/boards?page_size=250"):
        r = requests.get(base + path, headers=h, timeout=60)
        names = ""
        if r.ok and "boards" in path:
            names = [b.get("name") for b in r.json().get("items", [])]
        elif r.ok:
            names = {k: v for k, v in r.json().items() if k in ("account_type", "username")}
        print(base, path.split("?")[0], r.status_code, names if r.ok else r.text[:300])
