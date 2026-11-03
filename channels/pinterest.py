"""Pinterest channel (API v5): one pin per article on a topic board.

Same rules as IG/FB: the draft is saved at draft time, the pin is created only at
publish time, then read back by id before the ledger says `published`.
Boards are resolved by EXACT name via the API at runtime (ids are never stored).
"""

import os
from urllib.parse import parse_qsl, urlparse

from core import blog, boards, http, pinterest_auth

CODE = "pin"
TITLE_MAX, DESCRIPTION_MAX = 100, 500
_board_cache = {}


def api(path):
    return f"{pinterest_auth.api_base()}/v5{path}"


def _headers():
    return {"Authorization": f"Bearer {pinterest_auth.access_token()}"}


def article_url_with_utm(blog_handle, handle):
    return blog.build_article_url(
        blog_handle, handle,
        utm={"utm_source": "pinterest", "utm_medium": "social", "utm_campaign": handle},
    )


def build_draft(article, article_url, copy_out, board_name, image_repo_path):
    """copy_out = {title, description, alt_text} (German, already length-fitted)."""
    if len(copy_out["title"]) > TITLE_MAX or len(copy_out["description"]) > DESCRIPTION_MAX:
        raise ValueError("Pin copy exceeds Pinterest limits")
    return {
        "channel": CODE,
        "article_id": article["id"].rsplit("/", 1)[-1],
        "gid": article["id"],
        "handle": article["handle"],
        "title": copy_out["title"],              # the PIN title (German); also what the pending list shows
        "description": copy_out["description"],
        "alt_text": copy_out["alt_text"],
        "article_url": article_url,              # carries utm_source=pinterest
        "board": board_name,                     # board NAME; the id is resolved at publish time
        "published_at": article.get("publishedAt"),
        "image_path": image_repo_path,           # 1000x1500; image_url is built from the current repo name
        "target": f"Pinterest board: {board_name}",
    }


def list_boards():
    """{normalised name: board id} for every board the token can see (paged by bookmark)."""
    if "boards" in _board_cache:
        return _board_cache["boards"]
    found, bookmark = {}, None
    while True:
        params = {"page_size": 250, **({"bookmark": bookmark} if bookmark else {})}
        data = http.request("GET", api("/boards"), "pinterest-boards", headers=_headers(), params=params).json()
        for b in data.get("items", []):
            found[boards.norm(b["name"])] = b["id"]
        bookmark = data.get("bookmark")
        if not bookmark:
            _board_cache["boards"] = found
            return found


def resolve_board_id(name):
    found = list_boards()
    board_id = found.get(boards.norm(name))
    if not board_id:
        raise RuntimeError(f"Pinterest board {name!r} was not found. Boards the token can see "
                           f"({len(found)}): {sorted(found)}. Create it on Pinterest with exactly this name "
                           f"(Trial tokens only see Sandbox boards).")
    return board_id


def pin_url(pin_id):
    return f"https://www.pinterest.com/pin/{pin_id}/"


def _same_link(a, b):
    pa, pb = urlparse(a or ""), urlparse(b or "")
    return (pa.netloc, pa.path.rstrip("/"), sorted(parse_qsl(pa.query))) == (pb.netloc, pb.path.rstrip("/"), sorted(parse_qsl(pb.query)))


def verify(pin_id, draft, board_id):
    """Read the pin back by id: board, title and destination link must match the draft."""
    info = http.request("GET", api(f"/pins/{pin_id}"), "pinterest-readback", headers=_headers()).json()
    problems = []
    if str(info.get("board_id")) != str(board_id):
        problems.append(f"board_id {info.get('board_id')} != {board_id}")
    if (info.get("title") or "").strip() != draft["title"].strip():
        problems.append(f"title {info.get('title')!r} != {draft['title']!r}")
    if not _same_link(info.get("link"), draft["article_url"]):
        problems.append(f"link {info.get('link')!r} != {draft['article_url']!r}")
    if problems:
        raise RuntimeError("Pinterest read-back differs from the draft: " + "; ".join(problems))
    return info


def publish(draft, mark_publishing, revert_publishing):
    """Returns {post_id, url, timestamp}. mark_publishing/revert_publishing as for the other channels:
    `publishing` is persisted BEFORE the pin is created; an HTTP error from the create call means
    nothing was created, so the ledger goes back to `draft`."""
    board_id = resolve_board_id(draft["board"])
    mark_publishing(None)
    try:
        resp = http.request(
            "POST", api("/pins"), "pinterest-create-pin", headers=_headers(),
            json={"board_id": board_id, "title": draft["title"], "description": draft["description"],
                  "link": draft["article_url"], "alt_text": draft["alt_text"],
                  "media_source": {"source_type": "image_url", "url": draft["image_url"]}},
        )
    except http.ApiError:
        revert_publishing()
        raise
    pin_id = resp.json()["id"]
    info = verify(pin_id, draft, board_id)
    return {"post_id": pin_id, "url": pin_url(pin_id), "timestamp": info.get("created_at")}


def after_publish(draft, result):
    return None
