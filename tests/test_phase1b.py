"""Pending drafts stay approvable, bio link follows the latest publish, Meta token preflight, repo-rename safety."""

import html
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import draft
import publish
from core import approval, campaign, http, ledger, meta
from channels import instagram
from tests import test_pipeline as tp

A, B = "700000000001", "700000000002"      # A is OLDER than B
A_TS, B_TS = "2026-09-01T10:00:00Z", "2026-10-01T10:00:00Z"
ARTICLES = {
    A: {"id": f"gid://shopify/Article/{A}", "handle": "older", "title": "Older", "publishedAt": A_TS, "isPublished": True},
    B: {"id": f"gid://shopify/Article/{B}", "handle": "newer", "title": "Newer", "publishedAt": B_TS, "isPublished": True},
}


def make_draft(article_id, caption):
    a = ARTICLES[article_id]
    return {"channel": "ig", "article_id": article_id, "handle": a["handle"], "title": a["title"],
            "article_url": f"https://shop/blogs/blog/{a['handle']}", "full_caption": caption,
            "image_path": f"generated/ig-{article_id}.jpg", "target": "IG"}


def make_drafts(article_id, caption):
    """What draft.build_drafts returns: one draft per channel."""
    d = make_draft(article_id, caption)
    fb = {**d, "channel": "fb", "message": caption + " Zum Artikel: https://shop/x", "target": "FB"}
    fb.pop("full_caption")
    return {"ig": d, "fb": fb}


class DraftFlow(tp.InTmp):
    """Pending drafts stay approvable; re-drafts replace content under the same link."""

    def run_process(self, article_id, caption):
        sent = []
        with mock.patch.object(draft, "build_drafts", return_value=make_drafts(article_id, caption)), \
                mock.patch.object(approval, "_send", side_effect=lambda subj, body: sent.append(body)):
            draft.process(ARTICLES[article_id], "m")
        return sent[-1]

    def test_process_persists_then_emails(self):
        body = self.run_process(A, "cap A")
        entry = ledger.load()[A]["ig"]
        self.assertEqual((entry["status"], entry["article_id"], entry["handle"]), ("draft", A, "older"))
        self.assertEqual(ledger.read_draft(A, "ig")["full_caption"], "cap A")
        self.assertIn(f"title=approve%3A{A}%3Aig%2Bfb", body)
        self.assertNotIn("Pending approvals", body)  # nothing else pending

    def test_email_is_never_sent_if_persisting_fails(self):
        with mock.patch.object(draft, "build_drafts", return_value=make_drafts(A, "x")), \
                mock.patch.object(ledger, "update", side_effect=RuntimeError("push failed")), \
                mock.patch.object(approval, "_send") as send:
            with self.assertRaises(RuntimeError):
                draft.process(ARTICLES[A], "m")
        send.assert_not_called()

    def test_new_draft_never_touches_another_pending_draft(self):
        self.run_process(A, "cap A")
        before = (ledger.load()[A], ledger.draft_path(A, "ig").read_text(encoding="utf-8"))
        body = self.run_process(B, "cap B")
        after = (ledger.load()[A], ledger.draft_path(A, "ig").read_text(encoding="utf-8"))
        self.assertEqual(before, after)                       # not overwritten, expired or rejected
        self.assertEqual(ledger.load()[A]["ig"]["status"], "draft")
        self.assertIn("Pending approvals (1)", html.unescape(body))
        self.assertIn(f"title=approve%3A{A}%3Aig", body)       # A's Approve link is in B's email

    def test_pending_list_excludes_published_rejected_and_self(self):
        self.run_process(A, "cap A")
        data = ledger.load()
        data["1"] = {"ig": {"status": "published"}}
        data["2"] = {"ig": {"status": "rejected"}}
        data[ledger.BIO_KEY] = {"article_id": "1", "published_at": B_TS}
        ledger.save(data)
        body = self.run_process(B, "cap B")
        self.assertIn("Pending approvals (1)", html.unescape(body))
        pend = [(aid, ch) for aid, ch, _ in ledger.pending_drafts(ledger.load(), exclude_article_id=B)]
        self.assertEqual(pend, [(A, ["ig", "fb"])])

    def test_redraft_replaces_content_and_the_same_link_publishes_the_latest(self):
        first = self.run_process(A, "caption v1")
        second = self.run_process(A, "caption v2")
        self.assertEqual(ledger.read_draft(A, "ig")["full_caption"], "caption v2")
        link = f"title=approve%3A{A}%3Aig"
        self.assertIn(link, first)
        self.assertIn(link, second)                              # same approval link
        published = []

        def fake_publish(d, mark, revert):
            published.append(d["full_caption"])
            return {"post_id": "9", "url": "https://ig/p/9", "timestamp": "t"}

        with mock.patch.object(instagram, "publish", fake_publish), \
                mock.patch.object(instagram, "after_publish", return_value="ok"):
            line, ok = publish.do_approve(A, "ig")              # the link from the FIRST email
        self.assertTrue(ok)
        self.assertEqual(published, ["caption v2"])

    def test_redraft_after_rejection_makes_it_pending_again(self):
        self.run_process(A, "v1")
        publish.do_reject(A, "ig")
        self.assertEqual(ledger.load()[A]["ig"]["status"], "rejected")
        self.run_process(A, "v2")
        self.assertEqual(ledger.load()[A]["ig"]["status"], "draft")


class BioLinkLatestPublish(tp.InTmp):
    """/pages/latest-post follows the most recent successful IG publish (by publish time)."""
    env = {"BIO_LINK_PATH": "/pages/latest-post", "ACCESS_TOKEN": "t" * 12, "IG_USER_ID": "1"}

    def approve(self, article_id, published="2026-11-01T10:00:00+0000", redirect_error=None):
        data = ledger.load()
        data[article_id] = {"ig": {"status": "draft", "handle": ARTICLES[article_id]["handle"]}}
        ledger.save(data)
        ledger.write_draft(article_id, "ig", make_draft(article_id, "cap"))
        redirects = []

        def set_redirect(url):
            if redirect_error:
                raise RuntimeError(redirect_error)
            redirects.append(url)
            return {"path": "/pages/latest-post", "target": url}

        # pin the clock outside the campaign bio-link override ranges (Black Week / Christmas), or these tests flip with the real date
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(campaign, "now_local", return_value=datetime(2026, 10, 1, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))), \
                mock.patch.object(instagram, "publish",
                                  return_value={"post_id": "9", "url": "https://ig/p/9", "timestamp": published}), \
                mock.patch.object(instagram, "update_bio_redirect", side_effect=set_redirect), \
                mock.patch.object(approval, "_send") as send:
            line, ok = publish.do_approve(article_id, "ig")
        return line, ok, redirects, send

    def test_every_successful_publish_updates_the_bio_link_even_an_older_article(self):
        _, ok, redirects, _ = self.approve(B, published="2026-11-01T10:00:00+0000")
        self.assertEqual(redirects, ["https://shop/blogs/blog/newer"])
        # the OLDER article published afterwards still takes the bio link: it is the latest publish
        line, ok, redirects, _ = self.approve(A, published="2026-11-01T11:00:00+0000")
        self.assertTrue(ok)
        self.assertEqual(redirects, ["https://shop/blogs/blog/older"])
        data = ledger.load()
        self.assertEqual(data[ledger.BIO_KEY]["article_id"], A)
        self.assertEqual(data[A]["ig"]["status"], "published")
        self.assertEqual(data[B]["ig"]["status"], "published")

    def test_pointer_follows_each_publish_in_turn(self):
        for aid, ts in ((A, "2026-11-01T10:00:00+0000"), (B, "2026-11-02T10:00:00+0000"), (A, "2026-11-03T10:00:00+0000")):
            self.approve(aid, published=ts)
            self.assertEqual(ledger.load()[ledger.BIO_KEY]["article_id"], aid)

    def test_a_slower_concurrent_run_cannot_overwrite_a_more_recent_publish(self):
        self.approve(B, published="2026-11-01T12:00:00+0000")
        line, ok, redirects, _ = self.approve(A, published="2026-11-01T11:59:59+0000")  # published earlier
        self.assertTrue(ok)
        self.assertEqual(redirects, [])
        self.assertIn("left unchanged", line)
        self.assertEqual(ledger.load()[ledger.BIO_KEY]["article_id"], B)
        self.assertEqual(ledger.load()[A]["ig"]["status"], "published")  # the post itself still went live

    def test_works_with_no_prior_pointer_and_ignores_article_dates(self):
        # a redirect set by hand / by the old pipeline is simply overwritten by the next publish
        self.assertNotIn(ledger.BIO_KEY, ledger.load())
        _, _, redirects, _ = self.approve(A)
        self.assertEqual(redirects, ["https://shop/blogs/blog/older"])

    def test_graph_api_timestamp_formats_parse(self):
        for ts in ("2026-11-01T10:00:00+0000", "2026-11-01T10:00:00+00:00", "2026-11-01T10:00:00Z"):
            self.assertEqual(instagram._publish_time({"timestamp": ts}).hour, 10)
        self.assertIsNotNone(instagram._publish_time({}).tzinfo)  # missing -> now, timezone-aware

    def test_redirect_failure_keeps_the_post_published_and_emails_the_fix(self):
        line, ok, _, send = self.approve(A, redirect_error="Shopify says no")
        self.assertFalse(ok)
        self.assertEqual(ledger.load()[A]["ig"]["status"], "published")
        self.assertIn("post-publish step failed", line)
        body = html.unescape(send.call_args[0][1])
        self.assertIn("Shopify says no", body)
        self.assertIn("set the redirect by hand", body)
        self.assertIn("https://shop/blogs/blog/older", body)


class ImageUrlFromCurrentRepo(tp.InTmp):
    def test_stored_old_name_is_ignored_at_publish_time(self):
        ledger.save({tp.ID: {"ig": {"status": "draft", "handle": "h"}}})
        ledger.write_draft(tp.ID, "ig", {
            "full_caption": "c", "article_url": "u", "image_path": f"generated/ig-{tp.ID}.jpg",
            "image_url": "https://raw.githubusercontent.com/dstark-maison/mdc-instagram-pipeline/main/x.jpg"})
        seen = []

        def fake_publish(d, mark, revert):
            seen.append(d["image_url"])
            return {"post_id": "9", "url": "u", "timestamp": "t"}

        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "dstark-maison/mdc-social-pipeline"}), \
                mock.patch.object(instagram, "publish", fake_publish), \
                mock.patch.object(instagram, "after_publish", return_value=None):
            publish.do_approve(tp.ID, "ig")
        self.assertEqual(seen, [f"https://raw.githubusercontent.com/dstark-maison/mdc-social-pipeline/main/generated/ig-{tp.ID}.jpg"])


class FakeResp:
    def __init__(self, data):
        self._d, self.ok, self.status_code, self.text = data, True, 200, json.dumps(data)

    def json(self):
        return self._d


GOOD = {"data": {"is_valid": True, "type": "SYSTEM_USER", "expires_at": 0,
                 "scopes": ["instagram_basic", "instagram_content_publish", "pages_show_list",
                            "pages_read_engagement", "business_management", "public_profile"]}}


class TokenPreflight(tp.InTmp):
    TOKEN = "EAAB-super-secret-token-value"

    def pf(self, data=GOOD):
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": self.TOKEN}), \
                mock.patch.object(http, "request", return_value=FakeResp(data)) as req:
            return meta.preflight(), req

    def test_valid_token_passes_and_extra_scopes_are_ignored(self):
        result, req = self.pf()
        self.assertTrue(result["is_valid"])
        self.assertEqual(req.call_args.kwargs["headers"], {"Authorization": f"Bearer {self.TOKEN}"})

    def test_phase1_does_not_require_pages_manage_posts(self):
        self.assertNotIn("pages_manage_posts", meta.REQUIRED_SCOPES)

    def test_missing_scope_is_named(self):
        data = {"data": {**GOOD["data"], "scopes": ["instagram_basic", "pages_show_list", "pages_read_engagement"]}}
        with self.assertRaises(meta.TokenError) as cm:
            self.pf(data)
        self.assertIn("missing required scope(s): instagram_content_publish.", str(cm.exception))

    def test_invalid_or_missing_token(self):
        with self.assertRaises(meta.TokenError):
            self.pf({"data": {"is_valid": False, "error": {"message": "expired"}}})
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": ""}):
            with self.assertRaises(meta.TokenError):
                meta.preflight()

    def test_inspection_failure_never_leaks_the_token(self):
        err = http.ApiError(f"meta-debug-token failed: HTTP 400: bad input_token={self.TOKEN}")
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": self.TOKEN}), mock.patch.object(http, "request", side_effect=err):
            with self.assertRaises(meta.TokenError) as cm:
                meta.preflight()
            self.assertNotIn(self.TOKEN, http.redact(cm.exception))

    def test_network_error_message_never_contains_the_token(self):
        import requests
        boom = requests.ConnectionError(f"HTTPSConnectionPool: /debug_token?input_token={self.TOKEN} timed out")
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": self.TOKEN}), mock.patch("requests.request", side_effect=boom):
            with self.assertRaises(http.ApiError) as cm:
                http.request("GET", "https://x", "t")
        self.assertNotIn(self.TOKEN, str(cm.exception))

    def test_any_credential_named_env_var_is_redacted(self):
        with mock.patch.dict(os.environ, {"SOME_NEW_API_KEY": "abcdefgh12345", "PLAIN": "abcdefgh12345x"}):
            self.assertEqual(http.redact("x abcdefgh12345 y"), "x *** y")

    def test_publish_stops_with_a_failure_email_naming_the_scope(self):
        ledger.save({tp.ID: {"ig": {"status": "draft", "handle": "h"}}})
        ledger.write_draft(tp.ID, "ig", {"full_caption": "c", "article_url": "u", "image_path": "x.jpg"})
        env = {**tp.MainFlow.env, "RESULT_FILE": "result.md", "ACCESS_TOKEN": self.TOKEN}
        data = {"data": {**GOOD["data"], "scopes": ["instagram_basic"]}}
        with mock.patch.dict(os.environ, env), mock.patch.object(http, "request", return_value=FakeResp(data)), \
                mock.patch.object(approval, "_send") as send, mock.patch.object(instagram, "publish") as pub:
            with self.assertRaises(SystemExit):
                publish.main()
        pub.assert_not_called()
        subject, body = send.call_args[0]
        self.assertIn("instagram_content_publish", html.unescape(body))
        self.assertNotIn(self.TOKEN, body)
        self.assertEqual(ledger.load()[tp.ID]["ig"]["status"], "draft")

    def test_reject_does_not_need_a_working_token(self):
        ledger.save({tp.ID: {"ig": {"status": "draft", "handle": "h"}}})
        env = {**tp.MainFlow.env, "TITLE": f"reject:{tp.ID}:ig", "RESULT_FILE": "result.md", "ACCESS_TOKEN": ""}
        with mock.patch.dict(os.environ, env):
            publish.main()
        self.assertEqual(ledger.load()[tp.ID]["ig"]["status"], "rejected")

    def test_draft_stops_with_a_failure_email_naming_the_scope(self):
        env = {k: "x" * 10 for k in draft.REQUIRED_ENV}
        data = {"data": {**GOOD["data"], "scopes": []}}
        with mock.patch.dict(os.environ, env), mock.patch.object(http, "request", return_value=FakeResp(data)), \
                mock.patch.object(approval, "_send") as send, \
                mock.patch.object(draft.blog, "fetch_recent_articles") as fetch:
            with self.assertRaises(SystemExit):
                draft.main()
        fetch.assert_not_called()
        self.assertIn("instagram_basic", html.unescape(send.call_args[0][1]))


class RealGitRedraft(unittest.TestCase):
    """Reproduces the production failure: a re-draft rewrites a TRACKED file."""

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def test_update_survives_a_tracked_draft_file_being_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            bare, work = os.path.join(root, "bare.git"), os.path.join(root, "work")
            self.git(root, "init", "--bare", "-b", "main", bare)
            self.git(root, "clone", bare, work)
            self.git(work, "config", "user.email", "t@t")
            self.git(work, "config", "user.name", "t")
            self.git(work, "checkout", "-b", "main")
            cwd = os.getcwd()
            os.chdir(work)
            try:
                with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}):
                    path = ledger.draft_path(tp.ID, "ig")
                    mut = lambda d: ledger.set_entry(d, tp.ID, "ig", status="draft")
                    # seed the remote with v1 (the tracked file), then re-draft
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(ledger.draft_text({"full_caption": "v1"}), encoding="utf-8")
                    ledger.save({tp.ID: {"ig": {"status": "draft"}}})
                    self.git(work, "add", "-A")
                    self.git(work, "commit", "-m", "v1")
                    self.git(work, "push", "-u", "origin", "main")
                    ledger.update(mut, "redraft", {path: ledger.draft_text({"full_caption": "v2"})})
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["full_caption"], "v2")
                    remote = subprocess.run(["git", "--git-dir", bare, "show", f"main:{path.as_posix()}"],
                                            capture_output=True, text=True, encoding="utf-8").stdout
                    self.assertIn("v2", remote)
                    status = subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True).stdout
                    self.assertEqual(status.strip(), "")
            finally:
                os.chdir(cwd)

    def test_old_order_write_then_pull_really_fails(self):
        """Documents the bug: rewriting a tracked file before `git pull --rebase` is refused."""
        with tempfile.TemporaryDirectory() as root:
            bare, work = os.path.join(root, "bare.git"), os.path.join(root, "work")
            self.git(root, "init", "--bare", "-b", "main", bare)
            self.git(root, "clone", bare, work)
            self.git(work, "config", "user.email", "t@t")
            self.git(work, "config", "user.name", "t")
            self.git(work, "checkout", "-b", "main")
            f = os.path.join(work, "f.json")
            open(f, "w").write("v1")
            self.git(work, "add", "-A")
            self.git(work, "commit", "-m", "v1")
            self.git(work, "push", "-u", "origin", "main")
            open(f, "w").write("v2")
            r = subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=work, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unstaged changes", r.stderr)


if __name__ == "__main__":
    unittest.main()
