"""'removed' status (owner deleted the live post by hand), per-channel result comments, email button layout."""

import html
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import draft
import publish
from core import approval, ledger
from tests import test_phase2 as t2
from tests import test_pipeline as tp

ID = t2.ID


class RemovedGuard(t2.ApprovePaths):
    def seed_removed(self):
        self.seed(ig="removed", fb="draft")

    def test_approving_a_removed_entry_is_refused_and_nothing_is_posted(self):
        self.seed_removed()
        code, _, send = self.approve(f"approve:{ID}:ig")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls["ig"], [])
        self.assertEqual(self.status("ig"), "removed")
        self.assertIn("removed manually by the owner", send.call_args.args[1])      # failure email goes out
        self.assertIn("force=true", Path("result.md").read_text(encoding="utf-8"))

    def test_both_with_one_removed_posts_only_the_other_channel(self):
        self.seed_removed()
        code, _, _ = self.approve(f"approve:{ID}:ig+fb")
        self.assertEqual(code, 1)                                                   # the refused channel is reported
        self.assertEqual(self.calls["ig"], [])
        self.assertEqual(self.status("fb"), "published")

    def test_force_republishes_it(self):
        self.seed_removed()
        code, _, _ = self.approve(f"approve:{ID}:ig", extra_env={"FORCE_REMOVED": "true"})
        self.assertEqual((code, self.status("ig")), (0, "published"))
        self.assertEqual(len(self.calls["ig"]), 1)

    def test_any_other_force_value_does_not_count(self):
        self.seed_removed()
        for v in ("", "false", "1", "yes"):
            code, _, _ = self.approve(f"approve:{ID}:ig", extra_env={"FORCE_REMOVED": v})
            self.assertEqual((code, self.status("ig"), self.calls["ig"]), (1, "removed", []), v)

    def test_reject_leaves_a_removed_entry_alone(self):
        self.seed_removed()
        self.approve(f"reject:{ID}:ig")
        self.assertEqual(self.status("ig"), "removed")


class ResultListsEveryChannel(t2.ApprovePaths):
    def result(self):
        return Path("result.md").read_text(encoding="utf-8")

    def test_ig_only_says_fb_is_still_a_draft(self):
        self.seed(ig="draft", fb="draft")
        self.approve(f"approve:{ID}:ig")
        r = self.result()
        self.assertIn("`ig`: published and verified", r)
        self.assertIn("`fb`: still a draft, not approved, use 'FB only' to post it", r)

    def test_fb_only_says_ig_is_still_a_draft(self):
        self.seed(ig="draft", fb="draft")
        self.approve(f"approve:{ID}:fb")
        self.assertIn("`ig`: still a draft, not approved, use 'IG only' to post it", self.result())

    def test_approve_both_has_no_other_channel_section(self):
        self.seed(ig="draft", fb="draft")
        self.approve(f"approve:{ID}:ig+fb")
        self.assertNotIn("Other channels", self.result())

    def test_other_states_are_spelled_out(self):
        self.seed(ig="draft", fb="removed")
        self.approve(f"approve:{ID}:ig")
        self.assertIn("`fb`: removed manually by the owner", self.result())
        self.seed(ig="draft", fb="published")
        data = ledger.load()
        data[ID]["fb"]["url"] = "https://fb/1"
        ledger.save(data)
        self.approve(f"approve:{ID}:ig")
        self.assertIn("`fb`: already published → https://fb/1", self.result())

    def test_reject_of_one_channel_lists_the_other(self):
        self.seed(ig="draft", fb="draft")
        self.approve(f"reject:{ID}:ig")
        r = self.result()
        self.assertIn("`ig`: draft rejected", r)
        self.assertIn("`fb`: still a draft, not approved, use 'FB only' to post it", r)


class DraftForce(unittest.TestCase):
    ART = {"isPublished": True}

    def data(self):
        return {"5": {"ig": {"status": "removed"}, "fb": {"status": "draft"}}}

    def test_removed_blocks_a_redraft_unless_forced(self):
        with self.assertRaises(RuntimeError) as cm:
            draft.check_explicit("5", self.ART, self.data(), hold={})
        self.assertIn("removed manually", str(cm.exception))
        self.assertEqual(draft.check_explicit("5", self.ART, self.data(), hold={}, force_removed=True), ["ig", "fb"])

    def test_the_cron_never_picks_up_an_article_that_has_a_removed_entry(self):
        art = {"id": "gid://shopify/Article/5", "isPublished": True, "publishedAt": "2026-12-01T00:00:00Z"}
        self.assertEqual(draft.select_new_articles([art], self.data(), hold={}), [])


class RedraftClearsRemoved(tp.InTmp):
    def test_redrafting_with_force_gives_two_clean_drafts(self):
        ledger.save({t2.ID: {"ig": {"status": "removed", "note": "removed manually by owner", "post_id": "9",
                                    "url": "https://ig/x", "handle": "h", "article_id": t2.ID},
                             "fb": {"status": "draft", "handle": "h", "article_id": t2.ID}}})
        sent = []
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"}), \
                mock.patch.object(draft, "build_drafts", return_value=t2.drafts_for()), \
                mock.patch.object(approval, "_send", side_effect=lambda s, b: sent.append(b)):
            draft.process(t2.ART, "m", ("ig", "fb"))
        data = ledger.load()[t2.ID]
        for c in ("ig", "fb"):
            self.assertEqual(data[c]["status"], "draft")
            self.assertIsNone(data[c]["url"])
            self.assertNotIn("note", data[c])
        self.assertEqual(len(sent), 1)


class EmailButtons(unittest.TestCase):
    def block(self, buttons):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"}):
            return approval.button_block(ID, buttons)

    def anchors(self, body):
        return {m.group(2): m.group(1) for m in re.finditer(r'<a href="[^"]*" style="([^"]*)">([^<]*)</a>', body)}

    def size(self, style):
        return int(re.search(r"font-size:(\d+)px", style).group(1))

    def test_approve_both_is_big_and_the_rest_small_and_apart(self):
        body = self.block(draft.buttons_for(["ig", "fb"]))
        a = self.anchors(body)
        self.assertEqual(list(a), ["Approve both", "IG only", "FB only", "Reject"])
        self.assertGreaterEqual(self.size(a["Approve both"]), 18)
        for small in ("IG only", "FB only", "Reject"):
            self.assertLessEqual(self.size(a[small]), 13, small)
        self.assertIn("font-weight:bold", a["Approve both"])
        # IG only / FB only sit in their own paragraph, after the primary and before Reject
        self.assertLess(body.index("Approve both"), body.index("Only one channel"))
        self.assertLess(body.index("Only one channel"), body.index(">IG only<"))
        self.assertLess(body.index(">FB only<"), body.index("Discard"))
        self.assertLess(body.index("Discard"), body.index(">Reject<"))
        self.assertEqual(body.count("<p>") , 3)                                       # three separate rows

    def test_links_still_carry_the_right_titles(self):
        body = html.unescape(self.block(draft.buttons_for(["ig", "fb"])))
        for label, title in (("Approve both", f"approve%3A{ID}%3Aig%2Bfb"), ("IG only", f"approve%3A{ID}%3Aig"),
                             ("FB only", f"approve%3A{ID}%3Afb"), ("Reject", f"reject%3A{ID}%3Aig%2Bfb")):
            href = re.search(rf'<a href="([^"]*)"[^>]*>{label}</a>', body).group(1)
            self.assertIn(f"title={title}&", href + "&")

    def test_single_channel_email_has_one_big_approve_and_a_small_reject(self):
        a = self.anchors(self.block(draft.buttons_for(["fb"])))
        self.assertEqual(list(a), ["Approve", "Reject"])
        self.assertGreaterEqual(self.size(a["Approve"]), 18)
        self.assertLessEqual(self.size(a["Reject"]), 13)

    def test_full_email_has_exactly_four_links_for_the_article(self):
        sent = []
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"}), \
                mock.patch.object(approval, "_send", side_effect=lambda s, b: sent.append(b)):
            approval.send_draft_email({"article_id": ID, "title": "T"},
                                      [{"label": "Instagram", "image_url": "https://i", "text": "x", "target": "t", "link": "l"}],
                                      draft.buttons_for(["ig", "fb"]))
        self.assertEqual(sent[0].count("<a href"), 4)


class Workflows(unittest.TestCase):
    def test_force_inputs_reach_the_scripts_and_default_to_off(self):
        pub = Path(".github/workflows/social-publish.yml").read_text(encoding="utf-8")
        drf = Path(".github/workflows/social-draft.yml").read_text(encoding="utf-8")
        self.assertIn("FORCE_REMOVED: ${{ inputs.force }}", pub)
        self.assertIn("FORCE_REMOVED: ${{ inputs.force_removed }}", drf)
        self.assertRegex(pub, r"force:\n(?:.*\n)*?\s+default: false")
        self.assertRegex(drf, r"force_removed:\n(?:.*\n)*?\s+default: false")
        # an issue title can never force anything: the env var comes only from a dispatch input
        self.assertNotIn("issue.title", re.search(r"FORCE_REMOVED:[^\n]*", pub).group(0))


if __name__ == "__main__":
    unittest.main()
