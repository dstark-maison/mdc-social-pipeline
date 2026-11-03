"""Blog source: Shopify articles."""

import os
from datetime import datetime
from urllib.parse import quote

from core import shopify_client


def fetch_recent_articles(blog_handle, limit=10):
    """Most recent blog articles via the Admin GraphQL API.

    QueryRoot has no `blogByHandle` (confirmed against the live schema), so
    the blog is looked up via `blogs(query: "handle:...")`. Blog.articles has
    no sortKey either — reverse:true on the default (ID) order matches
    publish order for a blog that only publishes forward in time.
    """
    query = """
    query RecentArticles($blogHandleQuery: String!, $first: Int!) {
      blogs(first: 1, query: $blogHandleQuery) {
        nodes {
          articles(first: $first, reverse: true) {
            nodes {
              id
              title
              handle
              publishedAt
              isPublished
              summary
              body
              image { url }
            }
          }
        }
      }
    }
    """
    data = shopify_client.graphql(
        query, {"blogHandleQuery": f"handle:{blog_handle}", "first": limit}
    )
    blogs = data["blogs"]["nodes"]
    if not blogs:
        raise RuntimeError(f"Blog handle '{blog_handle}' not found or no access")
    return blogs[0]["articles"]["nodes"]


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_article_url(blog_handle, article_handle, utm=None):
    storefront = os.environ.get("STOREFRONT_DOMAIN", "www.maisondecocon.com")
    url = f"https://{storefront}/blogs/{blog_handle}/{article_handle}"
    if utm:
        url += "?" + "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in utm.items())
    return url


def fetch_article(article_id):
    """One article by NUMERIC id (used for explicit, on-request drafts)."""
    data = shopify_client.graphql(
        """query($id: ID!) { article(id: $id) {
             id title handle publishedAt isPublished summary body image { url } } }""",
        {"id": f"gid://shopify/Article/{article_id}"},
    )
    if not data["article"]:
        raise RuntimeError(f"No article with id {article_id}")
    return data["article"]


def german_texts(article_id):
    """German title / summary / body from Shopify translations, or {} (no translation, or the app
    lacks read_translations: callers then fall back to writing German from the source text)."""
    try:
        data = shopify_client.graphql(
            """query($id: ID!) { article(id: $id) { translations(locale: "de") { key value } } }""",
            {"id": f"gid://shopify/Article/{article_id}"},
        )
        return {t["key"]: t["value"] for t in (data["article"] or {}).get("translations", [])}
    except Exception as exc:
        print(f"  (no German translation available: {str(exc)[:120]})")
        return {}


def fetch_all_articles(blog_handle):
    """Every article of the blog (published or not), oldest page first."""
    query = """
    query($q: String!, $after: String) {
      blogs(first: 1, query: $q) { nodes { articles(first: 50, after: $after) {
        nodes { id title handle publishedAt isPublished summary body image { url } }
        pageInfo { hasNextPage endCursor } } } }
    }"""
    out, after = [], None
    while True:
        data = shopify_client.graphql(query, {"q": f"handle:{blog_handle}", "after": after})
        blogs = data["blogs"]["nodes"]
        if not blogs:
            raise RuntimeError(f"Blog handle '{blog_handle}' not found or no access")
        page = blogs[0]["articles"]
        out += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return out
        after = page["pageInfo"]["endCursor"]
