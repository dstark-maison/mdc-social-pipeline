"""Phase 3: Pinterest (copy limits, boards, image, token handling, channel, draft email,
approvals, and the gated backfill + daily publisher)."""

import base64
import html
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from PIL import Image

import backfill
import draft
import pinterest_daily
import pinterest_token_check
import publish
from channels import facebook, instagram, pinterest
from core import approval, boards, config, http, ledger, pin_copy, pin_draft, pin_image, pinterest_auth
from tests import test_pipeline as tp

ID = "700000000021"
ART = {"id": f"gid://shopify/Article/{ID}", "handle": "upholstered-headboard-guide", "title": "Upholstered Bed Headboards",
       "publishedAt": "2026-11-02T10:00:00Z", "isPublished": True, "image": {"url": "https://img/x.jpg"}}
URL = "https://www.maisondecocon.com/blogs/blog/upholstered-headboard-guide?utm_source=pinterest&utm_medium=social&utm_campaign=upholstered-headboard-guide"
BOARDS_API = {"items": [{"id": "b-bam", "name": "Bambus-Bettwäsche"}, {"id": "b-ten", "name": "Tencel-Bettwäsche"},
                        {"id": "b-dau", "name": "Daunendecken"}, {"id": "b-bed", "name": "Betten & Schlafzimmer"},
                        {"id": "b-wis", "name": "Schlafwissen"}]}


def pin_draft_json(article_id=ID, board="Betten & Schlafzimmer", title="Polsterbett-Kopfteile: Vorteile und Pflege"):
    return pinterest.build_draft(
        {"id": f"gid://shopify/Article/{article_id}", "handle": "h-" + article_id, "title": "T", "publishedAt": "2026-11-02T10:00:00Z"},
        URL, {"title": title, "description": "Kopfteil Polsterbett: Materialien und Pflege.", "alt_text": "Schlafzimmer"},
        board, f"generated/pin-{article_id}.jpg")


class FakeResp:
    def __init__(self, data):
        self._d, self.ok, self.status_code, self.text = data, True, 200, json.dumps(data)

    def json(self):
        return self._d


class PinCopy(unittest.TestCase):
    def test_fit_respects_limits_and_never_cuts_mid_word(self):
        long = "Kopfteil Polsterbett " * 40
        for limit in (100, 500):
            out = pin_copy.fit(long, limit)
            self.assertLessEqual(len(out), limit)
            self.assertFalse(out.endswith("Polsterb…"))
        self.assertEqual(pin_copy.fit("Kurz.", 100), "Kurz.")
        s = "Erster Satz ist ziemlich lang und endet hier. " + "Zweiter Satz " * 30
        self.assertEqual(pin_copy.fit(s, 60), "Erster Satz ist ziemlich lang und endet hier.")

    def test_generate_trims_model_output_to_pinterest_limits(self):
        out = {"title": "T" * 140, "description": "Schlafen schwitzen: " + "x " * 400, "alt_text": "a" * 900}
        with mock.patch("core.copy._call_claude", return_value=json.dumps(out)):
            r = pin_copy.generate({"title": "Night sweats", "summary": "s"}, {"title": "Nachtschweiß"}, "m")
        self.assertLessEqual(len(r["title"]), 100)
        self.assertLessEqual(len(r["description"]), 500)
        self.assertLessEqual(len(r["alt_text"]), 500)
        self.assertTrue(r["description"].startswith("Schlafen schwitzen:"))  # keyword-led stays first

    def test_prompt_is_german_keyword_led_and_forbids_claims(self):
        seen = {}
        with mock.patch("core.copy._call_claude", side_effect=lambda s, u, m, max_tokens=0: seen.update(s=s, u=u) or
                        json.dumps({"title": "t", "description": "d", "alt_text": "a"})):
            pin_copy.generate({"title": "EN", "summary": "x"}, {"title": "Deutscher Titel", "body_html": "<p>Inhalt</p>"}, "m")
        self.assertIn("Alles auf Deutsch", seen["s"])
        self.assertIn("keyword-led", seen["s"])
        self.assertIn("Deutscher Titel", seen["s"])
        self.assertIn("Niemals Gesundheits- oder Allergieversprechen", seen["s"])
        self.assertIn("Inhalt", seen["u"])


class PinImage(unittest.TestCase):
    def test_is_2_to_3_with_a_paper_card_and_fits_a_100_char_title(self):
        src = Image.new("RGB", (1600, 1200), (60, 90, 120))
        title = "Polsterbett-Kopfteile: Vorteile, Materialien und Pflege im Überblick – worauf es beim Kauf wirklich ankommt"[:100]
        out = pin_image.render(src, title)
        self.assertEqual(out.size, (1000, 1500))
        self.assertEqual(out.getpixel((900, 1420)), pin_image.PAPER)       # blank spot inside the card = #FAF9F7
        self.assertEqual(out.getpixel((10, 10)), (60, 90, 120))             # the article image fills the rest
        font, lines, size = pin_image.fit_title(title, 1000 - 2 * 64 - 2 * 56)
        self.assertLessEqual(len(lines), 4)
        self.assertTrue(all(font.getlength(l) <= 760 for l in lines))

    def test_fonts_are_the_brand_fonts(self):
        self.assertTrue((pin_image.FONT_DIR / "CormorantGaramond-Variable.ttf").exists())
        self.assertTrue((pin_image.FONT_DIR / "Jost-Variable.ttf").exists())
        self.assertEqual(pin_image.PAPER, (250, 249, 247))


class Boards(unittest.TestCase):
    def test_declared_boards_are_exactly_the_five_names(self):
        self.assertEqual(boards.load()["boards"], ["Bambus-Bettwäsche", "Tencel-Bettwäsche", "Daunendecken",
                                                    "Betten & Schlafzimmer", "Schlafwissen"])
        self.assertTrue(boards.validate())

    def test_rules(self):
        cases = {"bamboo-bed-linen-breathable-soft-and-sustainable": "Bambus-Bettwäsche",
                 "bamboo-vs-tencel-bedding": "Bambus-Bettwäsche",
                 "tencel-and-lyocell-bed-linen-silky-soft-and-sustainable": "Tencel-Bettwäsche",
                 "how-to-care-for-a-duvet": "Daunendecken", "ideal-bedroom-temperature-for-sleep": "Schlafwissen",
                 "upholstered-headboard-guide": "Betten & Schlafzimmer", "bed-linen-sizes-overview": "Schlafwissen",
                 "ironing-bed-sheets-guide": "Schlafwissen", "something-new-and-unknown": "Schlafwissen"}
        for handle, board in cases.items():
            self.assertEqual(boards.classify("1", handle), board, handle)

    def test_override_beats_rules_and_unknown_boards_are_rejected(self):
        cfg = boards.load()
        cfg["overrides"] = {"42": "Daunendecken"}
        self.assertEqual(boards.classify("42", "upholstered-headboard-guide", cfg), "Daunendecken")
        cfg["rules"] = cfg["rules"] + [{"board": "Nicht Deklariert", "any": ["x"]}]
        with self.assertRaises(ValueError):
            boards.validate(cfg)


class AuthBase(tp.InTmp):
    KEY = None

    def setUp(self):
        super().setUp()
        from cryptography.fernet import Fernet
        self.key = Fernet.generate_key().decode()
        self.env = mock.patch.dict(os.environ, {
            "PINTEREST_APP_ID": "appid", "PINTEREST_APP_SECRET": "appsecret-value", "PINTEREST_REFRESH_TOKEN": "pinr-secret-token-111",
            "PINTEREST_STATE_KEY": self.key, "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main",
            "RESEND_API_KEY": "x" * 12, "RESEND_FROM": "a@b.c", "RESEND_TO": "d@e.f"})
        self.env.start()
        self.addCleanup(self.env.stop)
        pinterest_auth._cache.clear()
        self.addCleanup(pinterest_auth._cache.clear)
        self.addCleanup(http._runtime_secrets.clear)

    def token_response(self, refresh="pinr-secret-token-111", scope="boards:read,pins:read,pins:write,user_accounts:read",
                       expires_in=5184000, access="pina-access-token-xyz"):
        return FakeResp({"access_token": access, "refresh_token": refresh, "scope": scope, "expires_in": 2592000,
                         "refresh_token_expires_in": expires_in})


class PinterestAuth(AuthBase):
    def test_refresh_uses_basic_auth_and_grants_scopes(self):
        with mock.patch.object(http, "request", return_value=self.token_response()) as req:
            token, state = pinterest_auth.refresh()
        self.assertEqual(token, "pina-access-token-xyz")
        kwargs = req.call_args.kwargs
        self.assertEqual(kwargs["data"], {"grant_type": "refresh_token", "refresh_token": "pinr-secret-token-111"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic " + base64.b64encode(b"appid:appsecret-value").decode())
        self.assertEqual(http.redact("t pina-access-token-xyz"), "t ***")   # registered for redaction

    def test_missing_scope_is_named(self):
        with mock.patch.object(http, "request", return_value=self.token_response(scope="boards:read,pins:read")):
            with self.assertRaises(pinterest_auth.PinterestAuthError) as cm:
                pinterest_auth.refresh()
        self.assertIn("pins:write", str(cm.exception))
        self.assertIn("user_accounts:read", str(cm.exception))

    def test_rotated_refresh_token_is_stored_encrypted_and_used_next_time(self):
        with mock.patch.object(http, "request", return_value=self.token_response(refresh="pinr-ROTATED-token-222")):
            pinterest_auth.refresh()
        raw = open(pinterest_auth.STATE_PATH, encoding="utf-8").read()
        self.assertNotIn("pinr-ROTATED-token-222", raw)                    # never plaintext in the (public) repo
        self.assertNotIn("pinr-secret-token-111", raw)
        state = json.loads(raw)
        self.assertEqual(pinterest_auth.current_refresh_token(state), "pinr-ROTATED-token-222")
        with mock.patch.object(http, "request", return_value=self.token_response(refresh="pinr-ROTATED-token-222")) as req:
            pinterest_auth.refresh()
        self.assertEqual(req.call_args.kwargs["data"]["refresh_token"], "pinr-ROTATED-token-222")

    def test_replacing_the_secret_wins_over_a_stored_token(self):
        with mock.patch.object(http, "request", return_value=self.token_response(refresh="pinr-ROTATED-token-222")):
            pinterest_auth.refresh()
        with mock.patch.dict(os.environ, {"PINTEREST_REFRESH_TOKEN": "pinr-NEW-oauth-token-333"}):
            self.assertEqual(pinterest_auth.current_refresh_token(), "pinr-NEW-oauth-token-333")

    def test_unrotated_token_does_not_rewrite_state_every_run(self):
        calls = []
        with mock.patch.object(http, "request", return_value=self.token_response()), \
                mock.patch.object(ledger, "update", side_effect=lambda *a, **k: calls.append(1)):
            pinterest_auth.refresh()                                       # first run: no state yet -> written
        self.assertEqual(len(calls), 1)
        state = {"refresh_expires_at": (pinterest_auth.now() + timedelta(days=59)).isoformat(timespec="seconds")}
        calls.clear()
        with mock.patch.object(http, "request", return_value=self.token_response()), \
                mock.patch.object(ledger, "update", side_effect=lambda *a, **k: calls.append(1)):
            pinterest_auth._persist(state, "pinr-secret-token-111", json.loads(self.token_response().text))
        self.assertEqual(calls, [])                                        # expiry moved < 3 days: nothing to commit

    def test_failed_refresh_is_a_clear_error_with_the_raw_body(self):
        with mock.patch.object(http, "request", side_effect=http.ApiError('pinterest-token failed: HTTP 401: {"message":"invalid_refresh_token"}')):
            with self.assertRaises(pinterest_auth.PinterestAuthError) as cm:
                pinterest_auth.refresh()
        self.assertIn("invalid_refresh_token", str(cm.exception))
        self.assertIn("one-time OAuth", str(cm.exception))

    def test_not_configured_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"PINTEREST_STATE_KEY": ""}):
            with self.assertRaises(pinterest_auth.PinterestAuthError) as cm:
                pinterest_auth.refresh()
        self.assertIn("PINTEREST_STATE_KEY", str(cm.exception))

    def test_warns_by_email_30_days_before_expiry_but_only_once_a_week(self):
        soon = (pinterest_auth.now() + timedelta(days=29)).isoformat(timespec="seconds")
        with mock.patch.object(approval, "_send") as send:
            self.assertTrue(pinterest_auth.maybe_warn_expiry({"refresh_expires_at": soon}))
            self.assertIn("expires in", send.call_args[0][0])
            state = json.load(open(pinterest_auth.STATE_PATH, encoding="utf-8"))
            self.assertIn("warned_at", state)
            self.assertFalse(pinterest_auth.maybe_warn_expiry(state))      # warned just now
            old = {**state, "warned_at": (pinterest_auth.now() - timedelta(days=8)).isoformat(timespec="seconds")}
            self.assertTrue(pinterest_auth.maybe_warn_expiry(old))         # a week later: again
        far = (pinterest_auth.now() + timedelta(days=31)).isoformat(timespec="seconds")
        with mock.patch.object(approval, "_send") as send:
            self.assertFalse(pinterest_auth.maybe_warn_expiry({"refresh_expires_at": far}))
        send.assert_not_called()

    def test_weekly_token_job(self):
        with mock.patch.dict(os.environ, {"PINTEREST_REFRESH_TOKEN": ""}), mock.patch.object(approval, "_send") as send:
            pinterest_token_check.main()                                   # not configured yet: quiet skip
        send.assert_not_called()
        with mock.patch.object(http, "request", side_effect=http.ApiError("pinterest-token failed: HTTP 400: bad")), \
                mock.patch.object(approval, "_send") as send:
            with self.assertRaises(SystemExit):
                pinterest_token_check.main()
        self.assertIn("Pinterest token check failed", send.call_args[0][0])
        with mock.patch.object(http, "request", return_value=self.token_response()), mock.patch.object(approval, "_send") as send:
            pinterest_token_check.main()
        send.assert_not_called()


class PinterestChannel(AuthBase):
    def api(self, pages=None, create=None, readback=None, calls=None):
        calls = calls if calls is not None else []
        pages = pages or [BOARDS_API]

        def fake(method, url, label, **kw):
            calls.append((method, url, label, kw))
            if label == "pinterest-token":
                return self.token_response()
            if label == "pinterest-boards":
                return FakeResp({**pages[len(calls and [c for c in calls if c[2] == "pinterest-boards"]) - 1]})
            if label == "pinterest-create-pin":
                if isinstance(create, Exception):
                    raise create
                return FakeResp(create or {"id": "999"})
            if label == "pinterest-readback":
                return FakeResp(readback)
            raise AssertionError(label)
        return fake, calls

    def setUp(self):
        super().setUp()
        pinterest._board_cache.clear()
        self.addCleanup(pinterest._board_cache.clear)

    def good_readback(self, d, board_id="b-bed"):
        return {"id": "999", "board_id": board_id, "title": d["title"], "link": d["article_url"],
                "created_at": "2026-11-02T10:00:00"}

    def test_boards_are_resolved_by_exact_name_across_pages(self):
        p1 = {"items": [{"id": "b-bam", "name": "Bambus-Bettwäsche"}], "bookmark": "next"}
        p2 = {"items": [{"id": "b-bed", "name": "Betten & Schlafzimmer"}]}
        fake, calls = self.api(pages=[p1, p2])
        with mock.patch.object(http, "request", fake):
            self.assertEqual(pinterest.resolve_board_id("Betten & Schlafzimmer"), "b-bed")
            self.assertEqual(pinterest.resolve_board_id("Bambus-Bettwäsche"), "b-bam")
        self.assertEqual([c[2] for c in calls].count("pinterest-boards"), 2)   # cached after the first listing
        self.assertEqual(calls[-1][3]["params"]["bookmark"], "next")

    def test_missing_board_names_what_the_token_does_see(self):
        fake, _ = self.api(pages=[{"items": [{"id": "x", "name": "Other"}]}])
        with mock.patch.object(http, "request", fake):
            with self.assertRaises(RuntimeError) as cm:
                pinterest.resolve_board_id("Daunendecken")
        self.assertIn("Daunendecken", str(cm.exception))
        self.assertIn("Other", str(cm.exception))
        self.assertIn("Sandbox", str(cm.exception))

    def test_publish_creates_the_pin_and_reads_it_back(self):
        d = pin_draft_json()
        d["image_url"] = "https://raw.githubusercontent.com/dstark-maison/mdc-social-pipeline/main/generated/pin-x.jpg"
        fake, calls = self.api(readback=self.good_readback(d))
        marks = []
        with mock.patch.object(http, "request", fake):
            result = pinterest.publish(d, lambda c: marks.append("mark"), lambda: marks.append("revert"))
        self.assertEqual(result, {"post_id": "999", "url": "https://www.pinterest.com/pin/999/", "timestamp": "2026-11-02T10:00:00"})
        create = next(c for c in calls if c[2] == "pinterest-create-pin")
        self.assertEqual(create[3]["json"], {
            "board_id": "b-bed", "title": d["title"], "description": d["description"], "link": URL, "alt_text": d["alt_text"],
            "media_source": {"source_type": "image_url", "url": d["image_url"]}})
        self.assertEqual(create[3]["headers"], {"Authorization": "Bearer pina-access-token-xyz"})
        self.assertEqual(marks, ["mark"])
        self.assertIn("utm_source=pinterest&utm_medium=social&utm_campaign=upholstered-headboard-guide", create[3]["json"]["link"])

    def test_readback_mismatch_leaves_publishing(self):
        d = pin_draft_json()
        d["image_url"] = "u"
        fake, _ = self.api(readback={**self.good_readback(d), "board_id": "other"})
        marks = []
        with mock.patch.object(http, "request", fake):
            with self.assertRaises(RuntimeError) as cm:
                pinterest.publish(d, lambda c: marks.append("mark"), lambda: marks.append("revert"))
        self.assertIn("board_id", str(cm.exception))
        self.assertEqual(marks, ["mark"])                                  # no automatic second pin

    def test_definite_error_reverts(self):
        d = pin_draft_json()
        d["image_url"] = "u"
        fake, _ = self.api(create=http.ApiError("pinterest-create-pin failed: HTTP 403: Trial access"))
        marks = []
        with mock.patch.object(http, "request", fake):
            with self.assertRaises(http.ApiError):
                pinterest.publish(d, lambda c: marks.append("mark"), lambda: marks.append("revert"))
        self.assertEqual(marks, ["mark", "revert"])

    def test_draft_enforces_pinterest_limits(self):
        with self.assertRaises(ValueError):
            pinterest.build_draft({"id": "gid://shopify/Article/1", "handle": "h", "title": "t"}, URL,
                                  {"title": "x" * 101, "description": "d", "alt_text": "a"}, "Schlafwissen", "p.jpg")
        with self.assertRaises(ValueError):
            pinterest.build_draft({"id": "gid://shopify/Article/1", "handle": "h", "title": "t"}, URL,
                                  {"title": "ok", "description": "d" * 501, "alt_text": "a"}, "Schlafwissen", "p.jpg")

    def test_utm_link(self):
        with mock.patch.dict(os.environ, {"STOREFRONT_DOMAIN": "www.maisondecocon.com"}):
            self.assertEqual(pinterest.article_url_with_utm("blog", "upholstered-headboard-guide"), URL)

    def test_api_base_is_configurable(self):
        self.assertEqual(pinterest.api("/pins"), "https://api.pinterest.com/v5/pins")
        with mock.patch.dict(os.environ, {"PINTEREST_API_BASE": "https://api-sandbox.pinterest.com"}):
            self.assertEqual(pinterest.api("/pins"), "https://api-sandbox.pinterest.com/v5/pins")


class DraftWithPinterest(AuthBase):
    def test_process_pin_persists_then_sends_its_own_email_with_approve_reject_only(self):
        d = pin_draft_json()
        sent = []
        with mock.patch.object(pinterest_auth, "preflight"), \
                mock.patch.object(pin_draft, "build", return_value=(d, __import__("pathlib").Path("generated/pin-x.jpg"))), \
                mock.patch.object(ledger, "commit_and_push"), \
                mock.patch.object(approval, "_send", side_effect=lambda s, b: sent.append((s, b))):
            draft.process_pin(ART, "m")
        entry = ledger.load()[ID]["pin"]
        self.assertEqual((entry["status"], entry["batch"], entry["publish_on"]), ("draft", None, None))
        self.assertEqual(ledger.read_draft(ID, "pin")["board"], "Betten & Schlafzimmer")
        subject, body = sent[0]
        self.assertTrue(subject.startswith("Review + Approve (Pinterest)"))
        self.assertEqual(body.count("<a href"), 2)
        self.assertIn(f"title=approve%3A{ID}%3Apin", body)
        self.assertIn(f"title=reject%3A{ID}%3Apin", body)
        text = html.unescape(body)
        self.assertIn("Betten & Schlafzimmer", text)
        self.assertIn("Board:", text)
        self.assertIn("/100)", text)
        self.assertIn("/500)", text)

    def test_pinterest_failure_never_blocks_instagram_and_facebook(self):
        env = {k: "x" * 10 for k in draft.BASE_ENV + draft.META_ENV}
        calls = []
        with mock.patch.dict(os.environ, env), mock.patch.object(draft.meta, "preflight"), \
                mock.patch.object(draft.blog, "fetch_article", return_value=ART), \
                mock.patch.object(draft, "process", side_effect=lambda a, m, ch: calls.append(("social", ch))), \
                mock.patch.object(draft, "process_pin", side_effect=RuntimeError("board missing")), \
                mock.patch.object(approval, "_send") as send, mock.patch.dict(os.environ, {"ARTICLE_ID": ID}):
            with self.assertRaises(SystemExit):
                draft.main()
        self.assertEqual(calls, [("social", ["ig", "fb"])])
        self.assertIn("Draft failed (Pinterest)", send.call_args[0][0])
        self.assertIn("board missing", html.unescape(send.call_args[0][1]))

    def test_instagram_failure_never_blocks_pinterest(self):
        env = {k: "x" * 10 for k in draft.BASE_ENV + draft.META_ENV}
        pins = []
        with mock.patch.dict(os.environ, {**env, "ARTICLE_ID": ID}), mock.patch.object(draft.meta, "preflight"), \
                mock.patch.object(draft.blog, "fetch_article", return_value=ART), \
                mock.patch.object(draft, "process", side_effect=RuntimeError("caption failed")), \
                mock.patch.object(draft, "process_pin", side_effect=lambda a, m: pins.append(1)), \
                mock.patch.object(approval, "_send"):
            with self.assertRaises(SystemExit):
                draft.main()
        self.assertEqual(pins, [1])

    def test_channel_selection_for_explicit_requests(self):
        self.assertEqual(draft.requested_channels(""), ["ig", "fb", "pin"])
        self.assertEqual(draft.requested_channels("pin"), ["pin"])
        self.assertEqual(draft.requested_channels("pin, ig"), ["ig", "pin"])
        with self.assertRaises(ValueError):
            draft.requested_channels("pin,tiktok")
        env = {k: "x" * 10 for k in draft.BASE_ENV}            # no Meta token at all
        pins, social = [], []
        with mock.patch.dict(os.environ, {**env, "ARTICLE_ID": ID, "DRAFT_CHANNELS": "pin", "ACCESS_TOKEN": ""}), \
                mock.patch.object(draft.meta, "preflight") as meta_pf, \
                mock.patch.object(draft.blog, "fetch_article", return_value=ART), \
                mock.patch.object(draft, "process", side_effect=lambda *a: social.append(1)), \
                mock.patch.object(draft, "process_pin", side_effect=lambda a, m: pins.append(1)):
            draft.main()
        self.assertEqual((pins, social), ([1], []))             # a pin-only request drafts no IG/FB, needs no Meta token
        meta_pf.assert_not_called()

    def test_pin_is_locked_once_scheduled_or_published(self):
        for status in ("scheduled", "published", "publishing"):
            self.assertEqual(draft.channels_to_draft(ID, {ID: {"pin": {"status": status}}}, ["pin"]), [])
        self.assertEqual(draft.channels_to_draft(ID, {ID: {"pin": {"status": "draft"}}}, ["pin"]), ["pin"])

    def test_ig_fb_email_is_unchanged_and_pins_are_not_in_the_pending_list(self):
        ledger.save({"700000000099": {"pin": {"status": "draft"}}})
        ledger.write_draft("700000000099", "pin", {"title": "t", "handle": "h"})
        self.assertEqual(ledger.pending_drafts(ledger.load()), [])


class PinApprovals(AuthBase):
    base_env = {**tp.MainFlow.env, "RESULT_FILE": "result.md"}

    def seed(self, aid=ID, status="draft", **extra):
        data = ledger.load()
        data[aid] = {"pin": {"status": status, "handle": "h", "article_id": aid, "url": None, "post_id": None, **extra}}
        ledger.save(data)
        ledger.write_draft(aid, "pin", {**pin_draft_json(aid), "image_url": "u"})

    def fake_pin(self, fail=None):
        m = mock.Mock()
        self.published = []

        def publish_fn(d, mark, revert):
            mark(None)
            if fail:
                revert()
                raise http.ApiError(fail)
            self.published.append(d["article_id"])
            return {"post_id": "p" + d["article_id"][-3:], "url": "https://www.pinterest.com/pin/1/", "timestamp": "t"}
        m.publish.side_effect = publish_fn
        m.after_publish.return_value = None
        return m

    def run_main(self, title, mod=None):
        mod = mod or self.fake_pin()
        with mock.patch.dict(os.environ, {**self.base_env, "TITLE": title}), mock.patch.dict(publish.CHANNELS, {"pin": mod}), \
                mock.patch.object(pinterest_auth, "preflight") as pf, mock.patch.object(approval, "_send") as send:
            try:
                publish.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, pf, send, mod

    def test_new_article_pin_publishes_immediately_on_approval(self):
        self.seed()
        code, pf, _, mod = self.run_main(f"approve:{ID}:pin")
        self.assertEqual(code, 0)
        pf.assert_called_once()
        e = ledger.load()[ID]["pin"]
        self.assertEqual((e["status"], e["url"]), ("published", "https://www.pinterest.com/pin/1/"))
        self.assertEqual(e["published_on"], ledger.now()[:10])
        self.assertEqual(self.published, [ID])

    def test_rerun_does_nothing(self):
        self.seed()
        self.run_main(f"approve:{ID}:pin")
        before = ledger.load()
        code, _, _, mod = self.run_main(f"approve:{ID}:pin")
        self.assertEqual((code, ledger.load()), (0, before))
        mod.publish.assert_not_called()

    def test_reject(self):
        self.seed()
        code, pf, _, mod = self.run_main(f"reject:{ID}:pin")
        self.assertEqual((code, ledger.load()[ID]["pin"]["status"]), (0, "rejected"))
        mod.publish.assert_not_called()

    def test_failure_emails_raw_error_and_returns_to_draft(self):
        self.seed()
        code, _, send, _ = self.run_main(f"approve:{ID}:pin", self.fake_pin(fail="HTTP 403: Trial access cannot create pins"))
        self.assertEqual((code, ledger.load()[ID]["pin"]["status"]), (1, "draft"))
        self.assertIn("Trial access cannot create pins", html.unescape(send.call_args[0][1]))

    def test_pinterest_token_failure_stops_before_any_pin(self):
        self.seed()
        mod = self.fake_pin()
        with mock.patch.dict(os.environ, {**self.base_env, "TITLE": f"approve:{ID}:pin"}), mock.patch.dict(publish.CHANNELS, {"pin": mod}), \
                mock.patch.object(pinterest_auth, "preflight", side_effect=pinterest_auth.PinterestAuthError("scope missing: pins:write")), \
                mock.patch.object(approval, "_send") as send:
            with self.assertRaises(SystemExit):
                publish.main()
        mod.publish.assert_not_called()
        self.assertIn("pins:write", html.unescape(send.call_args[0][1]))

    def test_backfill_pin_is_scheduled_not_published_by_approval(self):
        self.seed(batch="20261109", publish_on="2026-11-10")
        code, pf, _, mod = self.run_main(f"approve:{ID}:pin")
        e = ledger.load()[ID]["pin"]
        self.assertEqual((code, e["status"]), (0, "scheduled"))
        mod.publish.assert_not_called()
        code, _, _, _ = self.run_main(f"approve:{ID}:pin")                  # again: still just scheduled
        self.assertEqual(code, 0)
        self.assertEqual(ledger.load()[ID]["pin"]["status"], "scheduled")

    def test_batch_approval_schedules_only_that_batchs_drafts(self):
        for aid, batch, status in (("700000000031", "20261109", "draft"), ("700000000032", "20261109", "draft"),
                                   ("700000000033", "20261102", "draft"), ("700000000034", "20261109", "rejected")):
            self.seed(aid, status, batch=batch, publish_on="2026-11-10")
        code, pf, _, mod = self.run_main("approve-batch:20261109:pin")
        data = ledger.load()
        self.assertEqual(code, 0)
        self.assertEqual([data[a]["pin"]["status"] for a in ("700000000031", "700000000032", "700000000033", "700000000034")],
                         ["scheduled", "scheduled", "draft", "rejected"])
        mod.publish.assert_not_called()
        pf.assert_not_called()                                              # scheduling needs no Pinterest call
        self.assertIn("2 pin(s)", open("result.md", encoding="utf-8").read())

    def test_batch_reject_rejects_drafts_and_scheduled(self):
        self.seed("700000000031", "draft", batch="20261109", publish_on="x")
        self.seed("700000000032", "scheduled", batch="20261109", publish_on="x")
        self.seed("700000000033", "published", batch="20261109", publish_on="x")
        self.run_main("reject-batch:20261109:pin")
        data = ledger.load()
        self.assertEqual([data[a]["pin"]["status"] for a in ("700000000031", "700000000032", "700000000033")],
                         ["rejected", "rejected", "published"])

    def test_title_regex_accepts_batches_and_nothing_loose(self):
        for good in ("approve-batch:20261109:pin", "reject-batch:20261109:pin"):
            self.assertEqual(publish.parse_title(good)[1], "20261109")
        for bad in ("approve-batch:2026-11-09:pin", "approve-batch:20261109:ig", "approve-batch:20261109:pin\n",
                    "approve-batch:20261109:ig+pin", "approve:20261109:pin+fb"):
            with self.assertRaises(ValueError, msg=bad):
                publish.parse_title(bad)


class Backfill(AuthBase):
    def articles(self, n=30):
        return [{"id": f"gid://shopify/Article/{800000000000 + i}", "handle": f"h{i}", "title": f"T{i}", "isPublished": True,
                 "publishedAt": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)).isoformat()} for i in range(n)]

    def test_selection_excludes_held_pinned_unpublished_and_is_newest_first_max_21(self):
        arts = self.articles(30)
        arts[5]["isPublished"] = False
        held_id = ledger.numeric_id(arts[29]["id"])
        ledgered = {ledger.numeric_id(arts[28]["id"]): {"pin": {"status": "rejected"}},
                    ledger.numeric_id(arts[27]["id"]): {"ig": {"status": "published"}}}   # IG-only: still gets a pin
        picked = backfill.select(arts, ledgered, hold={held_id: "claims"})
        self.assertEqual(len(picked), 21)
        ids = [ledger.numeric_id(a["id"]) for a in picked]
        self.assertNotIn(held_id, ids)
        self.assertNotIn(ledger.numeric_id(arts[28]["id"]), ids)                          # already has a pin entry
        self.assertNotIn(ledger.numeric_id(arts[5]["id"]), ids)
        self.assertEqual(ids[0], ledger.numeric_id(arts[27]["id"]))                       # newest eligible first
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_three_per_day_starting_tomorrow(self):
        days = backfill.assign_days(21, date(2026, 11, 10))
        self.assertEqual(days[:4], ["2026-11-10"] * 3 + ["2026-11-11"])
        self.assertEqual(days[-1], "2026-11-16")
        self.assertTrue(all(days.count(d) <= 3 for d in set(days)))
        self.assertEqual(backfill.assign_days(4, date(2026, 11, 10)), ["2026-11-10"] * 3 + ["2026-11-11"])

    def test_off_until_standard_access(self):
        self.assertFalse(config.PINTEREST_STANDARD_ACCESS)
        with mock.patch.object(backfill.blog, "fetch_all_articles") as fetch, \
                mock.patch.object(approval, "_send") as send:
            backfill.main()
            pinterest_daily.main()
        fetch.assert_not_called()
        send.assert_not_called()

    def run_backfill(self, n=30, today=date(2026, 11, 9), built_failures=()):
        arts = self.articles(n)
        sent = []

        def fake_build(article, model):
            if ledger.numeric_id(article["id"]) in built_failures:
                raise RuntimeError("board missing")
            aid = ledger.numeric_id(article["id"])
            return pin_draft_json(aid), __import__("pathlib").Path(f"generated/pin-{aid}.jpg")

        env = {k: "x" * 10 for k in backfill.BASE_ENV}
        with mock.patch.object(config, "PINTEREST_STANDARD_ACCESS", True), mock.patch.dict(os.environ, env), \
                mock.patch.object(pinterest_auth, "preflight"), mock.patch.object(backfill.blog, "fetch_all_articles", return_value=arts), \
                mock.patch.object(pin_draft, "build", side_effect=fake_build), mock.patch.object(ledger, "commit_and_push") as push, \
                mock.patch.object(approval, "_send", side_effect=lambda s, b: sent.append((s, b))):
            try:
                backfill.main(today)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, sent, push

    def test_weekly_batch_creates_21_drafts_and_one_email(self):
        code, sent, push = self.run_backfill()
        data = ledger.load()
        pins = {aid: c["pin"] for aid, c in data.items()}
        self.assertEqual((code, len(pins)), (0, 21))
        self.assertTrue(all(p["status"] == "draft" and p["batch"] == "20261109" for p in pins.values()))
        per_day = {}
        for p in pins.values():
            per_day[p["publish_on"]] = per_day.get(p["publish_on"], 0) + 1
        self.assertEqual(sorted(per_day), [f"2026-11-{d}" for d in range(10, 17)])
        self.assertTrue(all(n == 3 for n in per_day.values()))                      # 3 pins per day maximum
        self.assertEqual(len(sent), 1)                                              # ONE email for the week
        subject, body = sent[0]
        self.assertIn("approve-batch%3A20261109%3Apin", body)
        self.assertIn("reject-batch%3A20261109%3Apin", body)
        self.assertEqual(body.count("approve%3A8000"), 21)                          # per-pin approve links
        self.assertEqual(body.count("reject%3A8000"), 21)
        self.assertIn("Approve all 21", html.unescape(body))
        push.assert_called_once()                                                    # one commit for all images
        self.assertEqual(len(push.call_args[0][0]), 21)

    def test_second_run_waits_while_a_batch_is_open(self):
        self.run_backfill()
        code, sent, _ = self.run_backfill(today=date(2026, 11, 16))
        self.assertEqual((code, sent), (0, []))                                      # no piling up
        data = ledger.load()
        for aid in data:
            data[aid]["pin"]["status"] = "published"
        ledger.save(data)
        code, sent, _ = self.run_backfill(today=date(2026, 11, 16))
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)                                               # next week's batch once the last is done

    def test_failed_pins_are_skipped_reported_and_do_not_block_the_rest(self):
        bad = {ledger.numeric_id(a["id"]) for a in self.articles(30)[-2:]}
        code, sent, _ = self.run_backfill(built_failures=bad)
        self.assertEqual(code, 1)
        self.assertEqual(len(ledger.load()), 19)
        self.assertEqual(sum(1 for s, _ in sent if "skipped" in s), 1)
        self.assertEqual(sum(1 for s, _ in sent if s.startswith("Pinterest backfill 2")), 1)


class DailyPublisher(AuthBase):
    def seed(self, aid, status, publish_on, published_on=None):
        data = ledger.load()
        data[aid] = {"pin": {"status": status, "handle": "h", "article_id": aid, "batch": "20261109", "publish_on": publish_on,
                             "published_on": published_on, "url": None, "post_id": None}}
        ledger.save(data)
        ledger.write_draft(aid, "pin", {**pin_draft_json(aid), "image_url": "u"})

    def run_daily(self, today, fail_ids=()):
        published = []

        def publish_fn(d, mark, revert):
            if d["article_id"] in fail_ids:
                mark(None)
                revert()
                raise http.ApiError("HTTP 429: rate limit")
            mark(None)
            published.append(d["article_id"])
            return {"post_id": "p", "url": "https://www.pinterest.com/pin/1/", "timestamp": "t"}
        mod = mock.Mock()
        mod.publish.side_effect = publish_fn
        mod.after_publish.return_value = None
        env = {**tp.MainFlow.env}
        with mock.patch.object(config, "PINTEREST_STANDARD_ACCESS", True), mock.patch.dict(os.environ, env), \
                mock.patch.dict(publish.CHANNELS, {"pin": mod}), mock.patch.object(pinterest_auth, "preflight"), \
                mock.patch.object(approval, "_send") as send, \
                mock.patch.object(ledger, "now", return_value=f"{today}T09:43:00+00:00"):
            try:
                pinterest_daily.main(today)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, published, send

    def test_publishes_at_most_three_a_day_oldest_first(self):
        for i in range(5):
            self.seed(f"70000000004{i}", "scheduled", "2026-11-10")
        code, published, _ = self.run_daily("2026-11-10")
        self.assertEqual((code, published), (0, ["700000000040", "700000000041", "700000000042"]))
        data = ledger.load()
        self.assertEqual([data[f"70000000004{i}"]["pin"]["status"] for i in range(5)],
                         ["published"] * 3 + ["scheduled"] * 2)
        code, published, _ = self.run_daily("2026-11-10")                           # same day again: cap already used
        self.assertEqual(published, [])
        code, published, _ = self.run_daily("2026-11-11")                           # next day: the other two
        self.assertEqual(published, ["700000000043", "700000000044"])

    def test_counts_pins_already_published_today_and_ignores_future_and_unapproved(self):
        self.seed("700000000050", "published", "2026-11-10", published_on="2026-11-10")
        self.seed("700000000051", "published", "2026-11-10", published_on="2026-11-10")
        self.seed("700000000052", "scheduled", "2026-11-10")
        self.seed("700000000053", "scheduled", "2026-11-10")
        self.seed("700000000054", "scheduled", "2026-11-12")                        # future
        self.seed("700000000055", "draft", "2026-11-10")                            # never approved
        code, published, _ = self.run_daily("2026-11-10")
        self.assertEqual(published, ["700000000052"])                               # only 1 slot left today

    def test_failure_keeps_the_pin_scheduled_and_emails(self):
        self.seed("700000000060", "scheduled", "2026-11-10")
        self.seed("700000000061", "scheduled", "2026-11-10")
        code, published, send = self.run_daily("2026-11-10", fail_ids={"700000000060"})
        self.assertEqual((code, published), (1, ["700000000061"]))
        self.assertEqual(ledger.load()["700000000060"]["pin"]["status"], "scheduled")  # retried tomorrow
        self.assertIn("rate limit", html.unescape(send.call_args[0][1]))

    def test_held_articles_are_never_backfilled_even_if_released_later_is_explicit(self):
        held = next(iter(config.HOLD_ARTICLE_IDS))
        arts = [{"id": f"gid://shopify/Article/{held}", "handle": "x", "title": "t", "isPublished": True,
                 "publishedAt": "2026-01-01T00:00:00Z"}]
        self.assertEqual(backfill.select(arts, {}), [])


class OauthTool(unittest.TestCase):
    def test_encrypted_roundtrip_and_authorize_url(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
        import pinterest_oauth as t
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        msg = json.dumps({"refresh_token": "pinr_" + "x" * 150})
        blob = t.encrypt(msg, pub)
        self.assertNotIn("pinr_", blob)
        self.assertEqual(t.decrypt(blob, priv), msg)
        self.assertEqual(t.SCOPES, ["boards:read", "pins:read", "pins:write", "user_accounts:read"])
        self.assertEqual(pinterest_auth.REQUIRED_SCOPES, t.SCOPES)


if __name__ == "__main__":
    unittest.main()
