"""Black Week + Christmas 2026 campaign: manifest, time windows, drafts, approval gate, stories, bio link."""

import os
import unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import campaign_run
import publish
from core import approval, campaign, http, ledger
from channels import instagram, story
from tests import test_pipeline as tp

BERLIN = ZoneInfo("Europe/Berlin")
M = campaign.load_manifest()
P1, P2, P3, P4, P5 = (str(p["id"]) for p in M["posts"])


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BERLIN)


def post(pid):
    return campaign.find_post(M, pid)


class Manifest(unittest.TestCase):
    def test_five_posts_in_the_agreed_order(self):
        got = [(str(p["date"]), p["type"], p["channels"], p["image"]) for p in M["posts"]]
        self.assertEqual(got, [
            ("2026-11-23", "feed", ["ig", "fb"], "black-week-de"),
            ("2026-11-27", "story", ["story"], "black-week-de"),
            ("2026-11-30", "story", ["story"], "black-week-de"),
            ("2026-12-01", "feed", ["ig", "fb"], "christmas-de"),
            ("2026-12-08", "story", ["story"], "christmas-de"),
        ])

    def test_images_are_shopify_cdn_urls_of_the_de_files(self):
        for key, img in M["images"].items():
            self.assertTrue(img["url"].startswith("https://cdn.shopify.com/"), key)
            self.assertIn("-de-1080x1350.jpg", img["file"])
            self.assertIn(img["file"], img["url"])

    def test_captions_are_exact(self):
        a, b = post(P1)["caption"], post(P4)["caption"]
        self.assertTrue(a.startswith("Black Week bei Maison de Cocon.\n\nVom 23. bis 30. November: −10% auf VIVARAISE"))
        self.assertIn("nicht mit anderen Rabattcodes kombinierbar.\n\nRuhige Nächte, schöne Stoffe. Jetzt etwas leichter zu haben.\n\nLink in der Bio.\n\nEN: Black Week, 23–30 Nov", a)
        self.assertTrue(a.rstrip().endswith("#maisondecocon #blackweek #bettwäsche #schlafzimmer #wohnen #interior"))
        self.assertTrue(b.startswith("Geschenke für ruhige Nächte.\n\nBis 25. Dezember: −5% auf VIVARAISE"))
        self.assertIn("Bestellen Sie bis zum 10. Dezember für die Lieferung vor Weihnachten.\n\nLink in der Bio.", b)
        self.assertTrue(b.rstrip().endswith("#maisondecocon #weihnachtsgeschenke #geschenkideen #bettwäsche #wohnen #interior"))

    def test_stories_have_no_caption(self):
        for pid in (P2, P3, P5):
            self.assertNotIn("caption", post(pid))

    def test_ids_are_accepted_by_the_approval_regex_for_every_channel(self):
        for pid in (P1, P2):
            for ch in ("ig", "fb", "ig+fb", "story"):
                approval.approval_link("approve", pid, ch)


class Windows(unittest.TestCase):
    def test_states(self):
        p = post(P1)
        self.assertEqual(campaign.window_state(p, M, at(2026, 11, 22, 23, 59)), "too_early")
        self.assertEqual(campaign.window_state(p, M, at(2026, 11, 23, 0, 0)), "before_publish_time")
        self.assertEqual(campaign.window_state(p, M, at(2026, 11, 23, 8, 59)), "before_publish_time")
        self.assertEqual(campaign.window_state(p, M, at(2026, 11, 23, 9, 0)), "open")
        self.assertEqual(campaign.window_state(p, M, at(2026, 11, 23, 23, 59)), "open")
        self.assertEqual(campaign.window_state(p, M, at(2026, 11, 24, 0, 0)), "expired")

    def test_utc_instants_are_converted_to_berlin(self):
        # 2026-11-23 22:30 UTC is already 23:30 Berlin on the 23rd; 23:30 UTC is 00:30 on the 24th
        p = post(P1)
        self.assertEqual(campaign.window_state(p, M, datetime(2026, 11, 23, 22, 30, tzinfo=ZoneInfo("UTC"))), "open")
        self.assertEqual(campaign.window_state(p, M, datetime(2026, 11, 23, 23, 30, tzinfo=ZoneInfo("UTC"))), "expired")


class Drafts(unittest.TestCase):
    def test_feed_post_drafts(self):
        d = campaign.build_drafts(post(P1), M)
        self.assertEqual(set(d), {"ig", "fb"})
        self.assertEqual(d["ig"]["full_caption"], post(P1)["caption"].rstrip("\n"))
        self.assertIn("Link in der Bio.", d["ig"]["full_caption"])
        fb = d["fb"]["message"]
        self.assertNotIn("Bio", fb)
        self.assertIn("Zum Angebot: https://maisondecocon.com/collections/black-friday?utm_source=facebook&utm_medium=social&utm_campaign=bw26", fb)
        # everything else is untouched
        self.assertEqual(fb.replace("Zum Angebot: " + post(P1)["fb_link"], "Link in der Bio."), d["ig"]["full_caption"])
        self.assertTrue(d["fb"]["image_url"].startswith("https://cdn.shopify.com/"))

    def test_christmas_facebook_link(self):
        fb = campaign.build_drafts(post(P4), M)["fb"]["message"]
        self.assertIn("https://maisondecocon.com/collections/weihnachtsgeschenke?utm_source=facebook&utm_medium=social&utm_campaign=xm26", fb)

    def test_story_draft_has_image_and_no_caption(self):
        d = campaign.build_drafts(post(P2), M)["story"]
        self.assertNotIn("full_caption", d)
        self.assertNotIn("message", d)
        self.assertEqual(d["channel"], "story")
        self.assertIn("black-week-2026-social-de", d["image_url"])

    def test_caption_without_the_bio_line_is_refused_for_facebook(self):
        with self.assertRaises(ValueError):
            campaign.facebook_message("kein Link", "https://x")


class BioSchedule(unittest.TestCase):
    def test_decisions_by_date(self):
        d = lambda y, m, dd: campaign.bio_decision(M, date(y, m, dd))
        self.assertEqual(d(2026, 11, 22), ("none", None))
        self.assertEqual(d(2026, 11, 23), ("campaign", "/collections/black-friday"))
        self.assertEqual(d(2026, 11, 30), ("campaign", "/collections/black-friday"))
        self.assertEqual(d(2026, 12, 1), ("campaign", "/collections/weihnachtsgeschenke"))
        self.assertEqual(d(2026, 12, 25), ("campaign", "/collections/weihnachtsgeschenke"))
        self.assertEqual(d(2026, 12, 26), ("reset", None))
        self.assertEqual(d(2026, 12, 29), ("reset", None))
        self.assertEqual(d(2026, 12, 30), ("none", None))   # the job has stopped by itself
        self.assertEqual(d(2027, 3, 1), ("none", None))

    def test_override_window(self):
        self.assertFalse(campaign.override_active(M, date(2026, 11, 22)))
        self.assertTrue(campaign.override_active(M, date(2026, 11, 23)))
        self.assertTrue(campaign.override_active(M, date(2026, 12, 25)))
        self.assertFalse(campaign.override_active(M, date(2026, 12, 26)))


class BioSync(tp.InTmp):
    ENV = {"BIO_LINK_PATH": "/pages/latest-post", "STOREFRONT_DOMAIN": "www.maisondecocon.com"}

    def run_sync(self, today, current_target, pointer=None):
        if pointer:
            d = ledger.load(); d[ledger.BIO_KEY] = pointer; ledger.save(d)
        updated = []
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(instagram, "_find_redirect", return_value={"id": "r", "path": "/pages/latest-post", "target": current_target}), \
                mock.patch.object(instagram, "update_bio_redirect", side_effect=lambda t: updated.append(t) or {"path": "/pages/latest-post", "target": t}):
            msg = campaign_run.sync_bio(M, today)
        return msg, updated

    def test_flips_to_black_friday_at_the_start(self):
        msg, upd = self.run_sync(date(2026, 11, 23), "https://www.maisondecocon.com/blogs/blog/x")
        self.assertEqual(upd, ["https://www.maisondecocon.com/collections/black-friday"])
        self.assertIn("read back", msg)

    def test_no_write_when_already_correct(self):
        _, upd = self.run_sync(date(2026, 11, 24), "https://www.maisondecocon.com/collections/black-friday")
        self.assertEqual(upd, [])

    def test_before_the_campaign_nothing_changes(self):
        _, upd = self.run_sync(date(2026, 11, 22), "https://www.maisondecocon.com/blogs/blog/x")
        self.assertEqual(upd, [])

    def test_reset_goes_back_to_the_newest_blog_post_from_the_ledger_pointer(self):
        ptr = {"article_id": "7", "url": "https://www.maisondecocon.com/blogs/blog/newest", "published_time": "2026-12-20T10:00:00+00:00"}
        _, upd = self.run_sync(date(2026, 12, 26), "https://www.maisondecocon.com/collections/weihnachtsgeschenke", ptr)
        self.assertEqual(upd, ["https://www.maisondecocon.com/blogs/blog/newest"])

    def test_blog_publish_during_the_campaign_updates_only_the_ledger_pointer(self):
        draft = {"article_id": "752000000001", "article_url": "https://www.maisondecocon.com/blogs/blog/new-post"}
        result = {"post_id": "1", "url": "u", "timestamp": "2026-11-25T08:00:00+0000"}
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(campaign, "now_local", return_value=at(2026, 11, 25, 10)), \
                mock.patch.object(instagram, "update_bio_redirect") as upd:
            msg = instagram.after_publish(draft, result)
        upd.assert_not_called()
        self.assertEqual(ledger.load()[ledger.BIO_KEY]["article_id"], "752000000001")
        self.assertIn("campaign", msg)

    def test_blog_publish_after_the_campaign_repoints_normally(self):
        draft = {"article_id": "752000000001", "article_url": "https://www.maisondecocon.com/blogs/blog/new-post"}
        result = {"post_id": "1", "url": "u", "timestamp": "2026-12-27T08:00:00+0000"}
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(campaign, "now_local", return_value=at(2026, 12, 27, 10)), \
                mock.patch.object(instagram, "update_bio_redirect", return_value={"path": "p", "target": "t"}) as upd:
            instagram.after_publish(draft, result)
        upd.assert_called_once_with("https://www.maisondecocon.com/blogs/blog/new-post")

    def test_campaign_posts_never_touch_the_bio_redirect(self):
        with mock.patch.object(instagram, "update_bio_redirect") as upd:
            instagram.after_publish({"campaign": "peak-2026", "article_id": P1}, {"timestamp": None})
        upd.assert_not_called()
        self.assertNotIn(ledger.BIO_KEY, ledger.load())


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class StoryChannel(unittest.TestCase):
    ENV = {"ACCESS_TOKEN": "tok-abcdefgh", "IG_USER_ID": "1784"}

    def graph(self, readback=None, calls=None):
        def fake(method, url, label, **kw):
            (calls if calls is not None else []).append((method, url.rsplit("/", 1)[-1], kw))
            if url.endswith("/media") and method == "POST":
                return FakeResp({"id": "C1"})
            if url.endswith("/C1"):
                return FakeResp({"status_code": "FINISHED"})
            if url.endswith("/media_publish"):
                return FakeResp({"id": "S1"})
            if url.endswith("/S1"):
                return FakeResp(readback or {"permalink": "https://instagram.com/stories/x/1", "timestamp": "t", "media_product_type": "STORY"})
            raise AssertionError(url)
        return fake

    def draft(self):
        return {"image_url": "https://cdn.shopify.com/a.jpg", "channel": "story"}

    def test_container_is_a_story_with_image_url_and_no_caption(self):
        calls = []
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(http, "request", self.graph(calls=calls)):
            out = story.publish(self.draft(), lambda c: None, lambda: None)
        create = next(kw for m, last, kw in calls if last == "media" and m == "POST")
        self.assertEqual(create["data"], {"image_url": "https://cdn.shopify.com/a.jpg", "media_type": "STORIES"})
        self.assertEqual(out["post_id"], "S1")
        self.assertEqual(out["url"], "https://instagram.com/stories/x/1")

    def test_publishing_is_marked_before_media_publish(self):
        order = []
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(http, "request", self.graph(calls=order)):
            story.publish(self.draft(), lambda c: order.append(("MARK", c, {})), lambda: None)
        names = [o[1] for o in order]
        self.assertLess(names.index("C1") if "C1" in names else 0, names.index("media_publish"))
        self.assertIn("MARK", [o[0] for o in order])
        self.assertLess([o[0] for o in order].index("MARK"), names.index("media_publish"))

    def test_readback_that_is_not_a_story_fails(self):
        with mock.patch.dict(os.environ, self.ENV), mock.patch("time.sleep"), \
                mock.patch.object(http, "request", self.graph(readback={"permalink": "p", "media_product_type": "FEED"})):
            with self.assertRaises(RuntimeError):
                story.publish(self.draft(), lambda c: None, lambda: None)

    def test_readback_without_permalink_fails(self):
        with mock.patch.dict(os.environ, self.ENV), mock.patch("time.sleep"), \
                mock.patch.object(http, "request", self.graph(readback={"timestamp": "t"})):
            with self.assertRaises(RuntimeError):
                story.publish(self.draft(), lambda c: None, lambda: None)

    def test_definite_error_on_publish_reverts(self):
        def fake(method, url, label, **kw):
            if url.endswith("/media_publish"):
                raise http.ApiError("nope")
            return self.graph()(method, url, label, **kw)
        reverted = []
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(http, "request", fake):
            with self.assertRaises(http.ApiError):
                story.publish(self.draft(), lambda c: None, lambda: reverted.append(1))
        self.assertEqual(reverted, [1])


class ApprovalGate(tp.InTmp):
    def seed(self, pid, status="draft", channels=("ig", "fb")):
        d = ledger.load()
        for ch in channels:
            ledger.set_entry(d, pid, ch, status=status, article_id=pid, handle=f"peak-2026-{pid}")
        ledger.save(d)
        for ch, dr in campaign.build_drafts(post(pid), M).items():
            ledger.write_draft(pid, ch, dr)

    def approve(self, pid, ch, now):
        fake_mod = mock.Mock()
        fake_mod.publish.return_value = {"post_id": "9", "url": "https://u", "timestamp": "t"}
        fake_mod.after_publish.return_value = None
        with mock.patch.dict(publish.CHANNELS, {ch: fake_mod}), mock.patch.object(campaign, "now_local", return_value=now):
            return publish.do_approve(pid, ch), fake_mod

    def test_before_the_day_it_is_refused(self):
        self.seed(P1)
        with self.assertRaises(publish.Blocked) as cm:
            self.approve(P1, "ig", at(2026, 11, 20, 12))
        self.assertIn("2026-11-23", str(cm.exception))

    def test_before_09_it_is_parked_as_approved_and_not_published(self):
        self.seed(P1)
        (line, ok), mod = self.approve(P1, "ig", at(2026, 11, 23, 7, 30))
        mod.publish.assert_not_called()
        self.assertTrue(ok)
        self.assertIn("09:00", line)
        self.assertEqual(ledger.load()[P1]["ig"]["status"], "approved")
        self.assertEqual(ledger.load()[P1]["fb"]["status"], "draft")

    def test_the_scheduled_run_publishes_an_approved_post_from_09(self):
        self.seed(P1, status="approved")
        (line, ok), mod = self.approve(P1, "ig", at(2026, 11, 23, 9, 1))
        mod.publish.assert_called_once()
        self.assertEqual(ledger.load()[P1]["ig"]["status"], "published")
        # campaign drafts carry their CDN image url, no repo path needed
        self.assertTrue(mod.publish.call_args[0][0]["image_url"].startswith("https://cdn.shopify.com/"))

    def test_late_approval_publishes_immediately(self):
        self.seed(P1)
        (line, ok), mod = self.approve(P1, "fb", at(2026, 11, 23, 17, 45))
        mod.publish.assert_called_once()
        self.assertEqual(ledger.load()[P1]["fb"]["status"], "published")

    def test_a_still_parked_post_is_not_published_before_09(self):
        self.seed(P1, status="approved")
        (line, ok), mod = self.approve(P1, "ig", at(2026, 11, 23, 8, 0))
        mod.publish.assert_not_called()
        self.assertEqual(ledger.load()[P1]["ig"]["status"], "approved")

    def test_after_midnight_nothing_publishes(self):
        self.seed(P1)
        with self.assertRaises(publish.Blocked):
            self.approve(P1, "ig", at(2026, 11, 24, 0, 0))

    def test_an_expired_entry_is_never_published(self):
        self.seed(P1, status="expired")
        with self.assertRaises(publish.Blocked):
            self.approve(P1, "ig", at(2026, 11, 23, 12))

    def test_story_publishes_through_the_same_gate(self):
        self.seed(P2, channels=("story",))
        (line, ok), mod = self.approve(P2, "story", at(2026, 11, 27, 10))
        mod.publish.assert_called_once()
        self.assertEqual(ledger.load()[P2]["story"]["status"], "published")

    def test_blog_articles_are_not_affected(self):
        self.assertIsNone(publish.campaign_gate("752115122509", {"status": "draft", "channel_code": "ig"}))


class Runner(tp.InTmp):
    """The daily tick, with the email, the Graph API and git faked."""

    def setUp(self):
        super().setUp()
        self.emails = []
        for target, fn in (("send_draft_email", lambda a, **kw: self.emails.append((a["article_id"], kw))),
                           ("send_failure_email", lambda *a, **kw: self.emails.append(("FAIL", a)))):
            p = mock.patch.object(approval, target, fn)
            p.start()
            self.addCleanup(p.stop)
        self.published = []
        self.env = mock.patch.dict(os.environ, {"BIO_LINK_PATH": "/pages/latest-post"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tick(self, now):
        sync = mock.patch.object(campaign_run, "sync_bio", return_value="bio: ok")
        with sync, mock.patch.object(campaign, "now_local", return_value=now), \
                mock.patch.object(campaign_run.meta, "preflight"), \
                mock.patch.object(publish, "do_approve", side_effect=lambda pid, ch: self.published.append((pid, ch)) or ("ok", True)):
            return campaign_run.run_drafts(M, now), campaign_run.run_publish(M, now), campaign_run.sweep(M, now)

    def test_nothing_before_07_and_nothing_on_other_days(self):
        self.tick(at(2026, 11, 23, 6, 59))
        self.tick(at(2026, 11, 24, 8))
        self.assertEqual(self.emails, [])
        self.assertEqual(ledger.load(), {})

    def test_exactly_one_email_per_post_however_often_the_job_ticks(self):
        for h, mi in ((7, 0), (8, 0), (8, 30), (9, 5), (12, 0)):
            self.tick(at(2026, 11, 23, h, mi))
        self.assertEqual([e[0] for e in self.emails], [P1])
        kw = self.emails[0][1]
        self.assertEqual([b[2] for b in kw["buttons"]], ["ig+fb", "ig", "fb", "ig+fb"])
        self.assertEqual(ledger.load()[P1]["ig"]["email_sent"], True)

    def test_story_email_has_two_buttons_and_no_caption(self):
        self.tick(at(2026, 11, 27, 7, 0))
        (pid, kw), = self.emails
        self.assertEqual(pid, P2)
        self.assertEqual([b[1:] for b in kw["buttons"]], [("approve", "story"), ("reject", "story")])
        self.assertIn("no caption", kw["previews"][0]["text"])

    def test_a_failed_email_is_resent_once_and_never_duplicated(self):
        calls = {"n": 0}

        def flaky(a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http.ApiError("resend down")
            self.emails.append((a["article_id"], kw))
        with mock.patch.object(approval, "send_draft_email", flaky):
            self.tick(at(2026, 11, 23, 7, 0))      # draft saved, email fails -> failure email
            self.tick(at(2026, 11, 23, 8, 0))      # re-sent
            self.tick(at(2026, 11, 23, 8, 30))     # no duplicate
        self.assertEqual([e[0] for e in self.emails if e[0] != "FAIL"], [P1])

    def test_a_rejected_or_pending_post_is_not_drafted_again(self):
        self.tick(at(2026, 11, 23, 7))
        d = ledger.load(); ledger.set_entry(d, P1, "ig", status="rejected"); ledger.set_entry(d, P1, "fb", status="rejected"); ledger.save(d)
        self.tick(at(2026, 11, 23, 8))
        self.assertEqual(len(self.emails), 1)

    def test_publishes_only_approved_entries_from_09(self):
        self.tick(at(2026, 11, 23, 7))
        d = ledger.load(); ledger.set_entry(d, P1, "ig", status="approved"); ledger.save(d)
        self.tick(at(2026, 11, 23, 8, 59))
        self.assertEqual(self.published, [])
        self.tick(at(2026, 11, 23, 9, 0))
        self.assertEqual(self.published, [(P1, "ig")])   # fb is still a draft: not approved, not published

    def test_expired_after_midnight_and_the_expired_post_is_never_picked_up(self):
        self.tick(at(2026, 11, 23, 7))
        d = ledger.load(); ledger.set_entry(d, P1, "ig", status="approved"); ledger.save(d)
        _, _, stale = self.tick(at(2026, 11, 24, 7))
        self.assertEqual(sorted(stale), [(P1, "fb"), (P1, "ig")])
        self.assertEqual({c: e["status"] for c, e in ledger.load()[P1].items()}, {"ig": "expired", "fb": "expired"})
        self.tick(at(2026, 11, 24, 9))
        self.assertEqual(self.published, [])

    def test_expired_drafts_drop_out_of_the_pending_list(self):
        self.tick(at(2026, 11, 23, 7))
        self.tick(at(2026, 11, 24, 7))
        self.assertEqual(campaign_run.pending_for_email(ledger.load(), "x"), [])

    def test_after_the_last_post_the_job_has_nothing_to_do(self):
        self.tick(at(2026, 12, 8, 7)); self.emails.clear()
        d = ledger.load(); ledger.set_entry(d, P5, "story", status="published"); ledger.save(d)
        for day in (9, 10, 25, 26, 31):
            self.tick(at(2026, 12, day, 9))
        self.tick(at(2027, 1, 15, 9))
        self.assertEqual(self.emails, [])
        self.assertEqual(self.published, [])

    def test_skip_flag_in_the_manifest_cancels_a_post(self):
        m2 = dict(M, posts=[dict(p, skip=True) if str(p["id"]) == P1 else p for p in M["posts"]])
        with mock.patch.object(campaign_run.meta, "preflight"):
            self.assertEqual(campaign_run.run_drafts(m2, at(2026, 11, 23, 7)), [])

    def test_reset_removes_drafts_and_entries_but_not_a_published_post(self):
        self.tick(at(2026, 11, 23, 7))
        self.assertTrue(ledger.draft_path(P1, "ig").exists())
        with mock.patch.object(campaign_run.subprocess, "run") as run, mock.patch.object(ledger, "_git"):
            run.return_value = mock.Mock(returncode=0)
            campaign_run.reset_post(P1)
        self.assertNotIn(P1, ledger.load())
        d = ledger.load(); ledger.set_entry(d, P2, "story", status="published"); ledger.save(d)
        with self.assertRaises(RuntimeError):
            campaign_run.reset_post(P2)


if __name__ == "__main__":
    unittest.main()
