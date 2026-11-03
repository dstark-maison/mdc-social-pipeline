"""Build one Pinterest draft (copy + board + 2:3 image). Shared by the new-post flow
(draft.py) and the weekly backfill (backfill.py)."""

import os
from pathlib import Path

from channels import pinterest
from core import blog, boards, image, ledger, pin_copy, pin_image


def build(article, model, board_name=None):
    """Returns (draft, image_path). Does not persist or commit anything.

    The board must exist on Pinterest NOW (fails early with the board names the token sees)
    instead of at approval time."""
    article_id = ledger.numeric_id(article["id"])
    handle = article["handle"]
    if not (article.get("image") or {}).get("url"):
        raise RuntimeError("Article has no featured image")

    copy_out = pin_copy.generate(article, blog.german_texts(article_id), model)
    board = board_name or boards.classify(article_id, handle)
    pinterest.resolve_board_id(board)

    path = Path(f"generated/pin-{article_id}.jpg")
    pin_image.save(image.download(article["image"]["url"]), copy_out["title"], path)

    url = pinterest.article_url_with_utm(os.environ["SHOPIFY_BLOG_HANDLE"], handle)
    return pinterest.build_draft(article, url, copy_out, board, path.as_posix()), path
