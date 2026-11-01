"""Phase 4: Reddit drafts (GSC + pasted findquestions + FAQ), manual publishing, privacy."""

import base64
import contextlib
import html
import io
import json
import os
import shutil
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import publish
import reddit_draft
import yaml
from core import approval, config, copy, gsc, http, ledger, questions, reddit_articles as ra, reddit_copy, reddit_store
from tests import test_pipeline as tp

HELD_ID = next(iter(config.HOLD_ARTICLE_IDS))


def faq_json(pairs):
    return ('<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs]}) + "</script>")


def art(i, handle, de, title, body_text, faq, aid=None, published=True):
    return {"id": aid or f"90000000000{i}", "handle": handle, "de_handle": de, "title": title, "de_title": None,
            "published": published, "published_at": "2026-10-01T10:00:00Z",
            "body": f"<p>{body_text}</p>" + faq_json(faq)}


INDEX = [
    art(1, "flannel-vs-flannelette-bedding", "winterbettwaesche-flanell-biber", "Winter Bedding: Flannel vs. Flannelette",
        "Flannel is woven cotton brushed on both sides. Flannelette is lighter. A typical flannel duvet cover weighs about 180 g/m2. " * 3,
        [("Is flannel warmer than flannelette?", "Flannel is usually warmer because it is heavier.")]),
    art(2, "duvet-sizes-for-every-bed", "bettdecken-groessen-tabelle", "Duvet Sizes: Which Size Fits Which Bed?",
        "In Germany a double bed usually takes a 200 x 200 cm duvet. A single duvet is 135 x 200 cm. " * 3,
        [("Which duvet size fits a 140 x 200 cm bed?", "A 135 x 200 cm duvet is the usual choice.")]),
    art(3, "how-to-buy-a-mattress", "matratze-kaufen", "Buying a Mattress: What Really Matters",
        "Firmness, material, size and build quality are the criteria that matter when you buy a mattress. " * 4,
        [("How long does a mattress last?", "That depends on the material and care.")]),
    art(4, "bamboo-tencel-down-pillow", "bambus-tencel-daune", "Bamboo, Tencel or Down Pillows", "Down pillow text. " * 20,
        [("Are down pillows warm?", "Yes.")], aid=HELD_ID),
    art(5, "muslin-bedding-trend", "musselin-bettdecke", "Muslin Bedding: What's Behind the Trend?",
        "Muslin is a loosely woven cotton that softens with every wash and feels airy. " * 4,
        [("Does muslin bedding shrink?", "It can shrink a little in the first wash, so allow for it.")]),
    art(6, "best-throw-blanket-material", "decke-material-vergleich", "Throw Blankets in Wool, Cashmere or Cotton",
        "Wool is warm and durable, cashmere is soft and light, cotton is easy to wash. " * 4,
        [("Which throw blanket material is easiest to care for?", "Cotton is the easiest to wash.")]),
    art(7, "washing-new-bed-linen", "neue-bettwaesche-waschen", "Washing New Bed Linen: Why the First Wash Matters",
        "Wash new bed linen once before first use to remove finishing agents. " * 4,
        [("Should I wash new sheets before using them?", "Yes, once at the temperature on the care label.")]),
]


def gsc_row(query, page, impressions, position=8.0, clicks=0):
    return {"query": query, "impressions": impressions, "clicks": clicks, "ctr": clicks / impressions, "position": position, "page": page}


def claude_json(question="Q"):
    body = ("Short answer: it depends on the weight of the fabric. " * 6 + "\n\nA typical flannel duvet cover weighs about "
            "180 g/m2 and a 135 x 200 cm duvet is the usual choice for one person.\n\n[[LINK]]")
    return json.dumps({"title": question.rstrip("?") + "?", "body": body, "unsupported": []})


PASTED = "Why does new bed linen need a first wash before use? | Which throw blanket material is best for winter?"


class QuestionHelpers(unittest.TestCase):
    def test_question_detection_and_language(self):
        for q in ("wie oft bettwäsche waschen", "how often to wash sheets", "Was ist Percale", "welche decke im winter",
                  "bettwäsche waschen?", "which duvet is warmest", "warum riecht neue bettwäsche"):
            self.assertTrue(questions.is_question(q), q)
        for q in ("bettwäsche 135x200", "bamboo bedding", "flanell bettwäsche kaufen"):
            self.assertFalse(questions.is_question(q), q)
        self.assertEqual([questions.language(q) for q in ("wie oft bettwäsche waschen", "how often to wash sheets",
                                                          "welche decke im winter", "is tencel good for summer")],
                         ["de", "en", "de", "en"])

    def test_dedupe_hash_ignores_case_punctuation_and_trailing_question_mark(self):
        self.assertEqual(questions.qhash("Wie oft Bettwäsche waschen?"), questions.qhash("wie oft  bettwäsche waschen"))
        self.assertNotEqual(questions.qhash("wie oft bettwäsche waschen"), questions.qhash("wie oft decke waschen"))
        self.assertEqual(len(questions.qhash("x")), 16)

    def test_pasted_list_is_split_even_if_newlines_collapse(self):
        got = questions.parse_pasted("1. How often should I wash sheets? What is percale? | Why does my duvet clump;\n- Welche Bettwäsche im Sommer?\nxx")
        self.assertEqual(got, ["How often should I wash sheets?", "What is percale?", "Why does my duvet clump",
                               "Welche Bettwäsche im Sommer?"])
        self.assertEqual(questions.parse_pasted(""), [])
        self.assertEqual(len(questions.parse_pasted("What is percale?\nwhat is percale")), 1)


class SearchConsole(unittest.TestCase):
    def test_window_matches_the_weekly_seo_report(self):
        start, end = gsc.window(date(2026, 11, 2))
        self.assertEqual((start, end), (date(2026, 10, 24), date(2026, 10, 30)))
        self.assertEqual((end - start).days, 6)
        self.assertEqual(end, date(2026, 11, 2) - timedelta(days=3))

    def test_credentials_raw_base64_and_missing(self):
        info = {"client_email": "x@y", "private_key": "k"}
        for raw in (json.dumps(info), base64.b64encode(json.dumps(info).encode()).decode()):
            with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": raw}):
                self.assertEqual(gsc.credentials_info(), info)
        with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            with self.assertRaises(gsc.GscError):
                gsc.credentials_info()
        with mock.patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": "not json or base64!!"}):
            with self.assertRaises(gsc.GscError):
                gsc.credentials_info()

    def test_aggregation_and_filter(self):
        rows = [{"keys": ["wie oft bettwäsche waschen", "https://x/blogs/blog/a"], "clicks": 0, "impressions": 40, "position": 8.0},
                {"keys": ["wie oft bettwäsche waschen", "https://x/blogs/blog/b"], "clicks": 1, "impressions": 10, "position": 12.0},
                {"keys": ["bettwäsche kaufen", "https://x/c"], "clicks": 0, "impressions": 99, "position": 3.0},      # not a question
                {"keys": ["what is percale", "https://x/p"], "clicks": 5, "impressions": 50, "position": 2.0},       # good CTR + position
                {"keys": ["how to wash a duvet", "https://x/d"], "clicks": 0, "impressions": 2, "position": 9.0},    # too few impressions
                {"keys": ["why is my duvet flat", "https://x/e"], "clicks": 0, "impressions": 30, "position": 40.0},  # low CTR, far down
                {"keys": ["which pillow is best", "https://x/f"], "clicks": 20, "impressions": 100, "position": 6.0}]  # pos 5-20 (ctr high)
        agg = gsc.aggregate(rows)
        top = {a["query"]: a for a in agg}["wie oft bettwäsche waschen"]
        self.assertEqual((top["impressions"], top["clicks"], top["page"]), (50, 1, "https://x/blogs/blog/a"))
        self.assertAlmostEqual(top["position"], (8 * 40 + 12 * 10) / 50)
        got = [a["query"] for a in gsc.question_queries(agg, 3)]
        self.assertEqual(got, ["which pillow is best", "wie oft bettwäsche waschen", "why is my duvet flat"])

    def test_pagination_and_no_query_text_is_printed(self):
        pages = [[{"keys": [f"how q{i}", "p"], "impressions": 5, "clicks": 0, "position": 9} for i in range(3)],
                 [{"keys": ["how last", "p"], "impressions": 5, "clicks": 0, "position": 9}]]
        calls = []

        def fake(method, url, label, **kw):
            calls.append(kw["json"]["startRow"])
            return mock.Mock(json=lambda: {"rows": pages[len(calls) - 1]})
        out = io.StringIO()
        with mock.patch.object(gsc, "ROW_LIMIT", 3), mock.patch.object(http, "request", fake), contextlib.redirect_stdout(out):
            rows = gsc.fetch_rows("tok", date(2026, 10, 1), date(2026, 10, 7))
        self.assertEqual((len(rows), calls), (4, [0, 3]))
        self.assertNotIn("how last", out.getvalue())


class Articles(unittest.TestCase):
    def test_faq_from_json_ld_and_accordion_fallback(self):
        pairs = ra.faq_pairs(INDEX[0]["body"])
        self.assertEqual(pairs[0][0], "Is flannel warmer than flannelette?")
        accordion = ('<details><summary><div class="t"><span>Which size?</span><span>x</span></div></summary>'
                     '<div class="accordion__content"><div class="prose">The 135 one.</div></div></details>')
        self.assertEqual(ra.faq_pairs(accordion), [("Which size?", "The 135 one.")])
        self.assertEqual(ra.faq_pairs("<p>no faq</p>"), [])

    def test_source_text_has_no_markup_placeholders_or_script(self):
        t = ra.source_text({"body": "<p>Hello <b>world</b>.</p><!-- PHOTO PLACEHOLDER x -->[[mdc-products]]" + faq_json([("Q1?", "A1.")])})
        self.assertIn("Hello world.", t)
        self.assertNotIn("<", t)
        self.assertNotIn("PLACEHOLDER", t)
        self.assertNotIn("mdc-products", t)
        self.assertNotIn("@context", t)
        self.assertIn("Q: Q1? A: A1.", t)

    def test_page_matching_by_english_and_german_handle_and_locale_paths(self):
        for url in ("https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber",
                    "https://www.maisondecocon.com/en/blogs/blog/flannel-vs-flannelette-bedding?x=1",
                    "https://www.maisondecocon.com/de/blogs/blog/Winterbettwaesche-Flanell-Biber"):
            self.assertEqual(ra.match_page(url, INDEX)["id"], INDEX[0]["id"], url)
        self.assertIsNone(ra.match_page("https://www.maisondecocon.com/collections/beds", INDEX))
        self.assertIsNone(ra.match_page("https://www.maisondecocon.com/blogs/blog/unknown-handle", INDEX))

    def test_text_matching_needs_two_distinctive_tokens(self):
        self.assertEqual(ra.match_text("which duvet size for a double bed", INDEX)["id"], INDEX[1]["id"])
        self.assertEqual(ra.match_text("what really matters when buying a mattress", INDEX)["id"], INDEX[2]["id"])
        self.assertIsNone(ra.match_text("how long does a mattress last", INDEX))   # only "mattress" is distinctive
        self.assertIsNone(ra.match_text("what is the weather", INDEX))
        self.assertIsNone(ra.match_text("duvet", INDEX))                      # one token is not enough

    def test_held_and_unpublished_are_not_eligible(self):
        ids = {a["id"] for a in ra.eligible(INDEX)}
        self.assertNotIn(HELD_ID, ids)
        draft_art = {**INDEX[0], "id": "123", "published": False}
        self.assertNotIn("123", {a["id"] for a in ra.eligible(INDEX + [draft_art])})

    def test_links_follow_the_question_language_with_utm(self):
        env = {"STOREFRONT_DOMAIN": "www.maisondecocon.com", "SHOPIFY_BLOG_HANDLE": "blog"}
        with mock.patch.dict(os.environ, env):
            a = INDEX[0]
            utm = ra.reddit_utm(a)
            self.assertEqual(ra.article_url(a, "en", utm),
                             "https://www.maisondecocon.com/en/blogs/blog/flannel-vs-flannelette-bedding"
                             "?utm_source=reddit&utm_medium=social&utm_campaign=flannel-vs-flannelette-bedding")
            self.assertEqual(ra.article_url(a, "de", utm),
                             "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber"
                             "?utm_source=reddit&utm_medium=social&utm_campaign=flannel-vs-flannelette-bedding")
            self.assertTrue(ra.article_url({**a, "de_handle": None}, "de", utm).startswith("https://www.maisondecocon.com/en/blogs/"))


class RedditCopy(unittest.TestCase):
    LINK = "https://www.maisondecocon.com/en/blogs/blog/x?utm_source=reddit&utm_medium=social&utm_campaign=x"
    SRC = "Flannel is brushed on both sides. A typical duvet cover weighs about 180 g/m2."

    def raw(self, body, **kw):
        return {"title": "What is flannel?", "body": body, "unsupported": [], **kw}

    def test_no_disclosure_in_our_own_subreddit_and_at_most_one_link_ours(self):
        for lang in ("en", "de"):
            d = reddit_copy.finalize(self.raw("Answer.\n\n[[LINK]]"), lang, self.LINK)
            self.assertNotIn("Disclosure", d["body"])
            self.assertNotIn("Offenlegung", d["body"])
            self.assertEqual(d["body"].count("http"), 1)
            self.assertIn(self.LINK, d["body"])
            self.assertTrue(d["body"].rstrip().endswith(self.LINK))                     # the link line is last
            self.assertNotIn("[[LINK]]", d["body"])

    def test_model_written_urls_are_removed_and_flagged(self):
        d = reddit_copy.finalize(self.raw("See https://evil.example/x and www.maisondecocon.com.\n\n[[LINK]]"), "en", self.LINK)
        self.assertNotIn("evil.example", d["body"])
        self.assertEqual(d["body"].count("http"), 1)
        self.assertTrue(d["link_dropped"])
        self.assertTrue(any("own URL" in f for f in reddit_copy.flags_for({**d, "body": d["body"] * 2}, self.SRC)))

    def test_no_link_means_none_in_the_post(self):
        d = reddit_copy.finalize(self.raw("Answer."), "en", None)
        self.assertNotIn("http", d["body"])

    def test_flags_cover_numbers_claims_and_missing_source(self):
        long_body = "Flannel is brushed. " * 30
        ok = reddit_copy.flags_for({"body": long_body + "It weighs about 180 g/m2.", "unsupported": []}, self.SRC)
        self.assertEqual(ok, [])
        f = reddit_copy.flags_for({"body": long_body + "It weighs 250 g/m2 and is hypoallergenic and OEKO-TEX certified.",
                                   "unsupported": ["it is hypoallergenic"]}, self.SRC)
        text = " | ".join(f)
        self.assertIn("250", text)
        self.assertIn("hypoallergen", text)
        self.assertIn("oeko-tex", text)
        self.assertIn("not in the article", text)
        self.assertTrue(any("NO MATCHING ARTICLE" in x for x in reddit_copy.flags_for({"body": long_body, "unsupported": []}, "", False)))
        self.assertTrue(any("Short answer" in x for x in reddit_copy.flags_for({"body": "tiny", "unsupported": []}, self.SRC)))
        self.assertEqual(reddit_copy.flags_for({"body": long_body + "wir verkaufen", "unsupported": []}, self.SRC and "wir verkaufen das"), [])

    def test_prompt_states_the_rules(self):
        s = reddit_copy._system("en")
        for needle in ("ONLY the facts given below", "no sales tone", "Do NOT write any URL", "[[LINK]]", "English", "unsupported",
                       "NEVER mention or allude to where the facts come from", "LEAVE THAT PART OUT", "it depends"):
            self.assertIn(needle, s)
        self.assertNotIn("SOURCE", s)                                    # the word is not even used with the model
        self.assertIn("German", reddit_copy._system("de"))
        seen = {}
        with mock.patch.object(copy, "_call_claude", side_effect=lambda s, u, m, max_tokens=0: seen.update(u=u) or claude_json()):
            reddit_copy.generate("Is flannel warm?", "en", "SOURCE-FACTS-HERE", self.LINK, "m")
        self.assertIn("SOURCE-FACTS-HERE", seen["u"])


class Selection(unittest.TestCase):
    def c(self, text, source, aid, score=1):
        return {"text": text, "lang": "en", "source": source, "article_id": aid, "signals": {}, "score": score}

    def test_slots_gsc_then_pasted_then_faq_with_distinct_articles(self):
        g = [self.c(f"How gsc {i}?", "gsc", str(i)) for i in range(1, 6)]
        p = [self.c(f"What pasted {i}?", "findquestions", str(10 + i)) for i in range(1, 5)]
        f = [self.c(f"Which faq {i}?", "faq", str(20 + i)) for i in range(1, 5)]
        got = reddit_draft.select(g, p, f, set())
        self.assertEqual([c["source"] for c in got], ["gsc"] * 3 + ["findquestions"] * 2)
        self.assertEqual(len({c["article_id"] for c in got}), 5)

    def test_shortfalls_are_filled_gsc_then_pasted_then_faq(self):
        g = [self.c("How gsc 1?", "gsc", "1")]
        p = [self.c("What pasted 1?", "findquestions", "11")]
        f = [self.c(f"Which faq {i}?", "faq", str(20 + i)) for i in range(1, 6)]
        got = reddit_draft.select(g, p, f, set())
        self.assertEqual([c["source"] for c in got], ["gsc", "findquestions", "faq", "faq", "faq"])

    def test_one_question_per_article_and_ledger_dedupe(self):
        g = [self.c("How flannel one?", "gsc", "1"), self.c("How flannel two?", "gsc", "1"), self.c("How duvet?", "gsc", "2")]
        seen = {questions.qhash("How duvet?")}
        got = reddit_draft.select(g, [], [], seen)
        self.assertEqual([c["text"] for c in got], ["How flannel one?"])

    def test_gsc_rows_best_per_article_held_and_unmatched_are_not_used(self):
        rows = [gsc_row("was ist besser flanell oder biber", "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber", 90),
                gsc_row("ist flanell das gleiche wie biber", "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber", 70),
                gsc_row("are down pillows warm", "https://www.maisondecocon.com/en/blogs/blog/bamboo-tencel-down-pillow", 60),
                gsc_row("what is the weather", "https://www.maisondecocon.com/collections/x", 50)]
        cands, stats = reddit_draft.from_gsc(rows, INDEX, config.HOLD_ARTICLE_IDS)
        self.assertEqual([(c["text"], c["lang"], c["source"]) for c in cands], [("Was ist besser flanell oder biber?", "de", "gsc")])
        self.assertEqual((stats["held"], stats["unmatched"]), (1, 1))

    def test_faq_candidates_are_one_per_article_and_never_from_held(self):
        cands = reddit_draft.from_faq(INDEX, config.HOLD_ARTICLE_IDS, "20261102")
        ids = [c["article_id"] for c in cands]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(HELD_ID, ids)
        self.assertEqual([c["article_id"] for c in cands], [c["article_id"] for c in reddit_draft.from_faq(INDEX, config.HOLD_ARTICLE_IDS, "20261102")])


class WeeklyBase(tp.InTmp):
    """Shared fixture: a faked workflow environment (artifacts = files in ./uploaded)."""

    def setUp(self):
        super().setUp()
        from cryptography.fernet import Fernet
        self.env = mock.patch.dict(os.environ, {
            **{k: "x" * 12 for k in reddit_draft.BASE_ENV}, "REDDIT_ARTIFACT_KEY": Fernet.generate_key().decode(),
            "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main", "SHOPIFY_BLOG_HANDLE": "blog",
            "STOREFRONT_DOMAIN": "www.maisondecocon.com", "RESEND_FROM": "a@b.c", "RESEND_TO": "d@e.f", "QUESTIONS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sent, self.uploaded = [], Path("uploaded")
        self.uploaded.mkdir()
        self.gsc_candidates = [gsc_row("was ist besser flanell oder biber", "https://www.maisondecocon.com/blogs/blog/winterbettwaesche-flanell-biber", 90),
                               gsc_row("which duvet size fits a double bed", "https://www.maisondecocon.com/en/blogs/blog/duvet-sizes-for-every-bed", 70),
                               gsc_row("how to choose a mattress", "https://www.maisondecocon.com/en/blogs/blog/how-to-buy-a-mattress", 60)]

        def fake_read(name, filename):  # what the workflow uploaded, as the next run/approval would download it
            f = self.uploaded / f"{name}.enc"
            return reddit_store.decrypt(f.read_bytes()) if f.exists() else None
        patches = [mock.patch.object(reddit_store, "read", fake_read),
                   mock.patch.object(approval, "_send", side_effect=lambda s, b: self.sent.append((s, b))),
                   mock.patch.object(ra, "fetch_index", return_value=INDEX),
                   mock.patch.object(copy, "_call_claude", side_effect=lambda s, u, m, max_tokens=0: claude_json(u.split("\n")[0][len("Question: "):]))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_week(self, today, pasted="", gsc_ok=True):
        pull = {"rows": 100, "queries": 90, "candidates": self.gsc_candidates, "window": (today, today)}
        out = io.StringIO()
        before = len(self.sent)
        with mock.patch.dict(os.environ, {"QUESTIONS": pasted}), \
                mock.patch.object(gsc, "pull", return_value=pull) if gsc_ok else mock.patch.object(gsc, "pull", side_effect=gsc.GscError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")), \
                contextlib.redirect_stdout(out):
            try:
                reddit_draft.main(today)
            except SystemExit:
                pass
        batch = today.strftime("%Y%m%d")
        for name, src in ((f"reddit-batch-{batch}", "reddit-out/batch.enc"), ("reddit-pool", "reddit-out/pool.enc")):
            if Path(src).exists():
                shutil.copy(src, self.uploaded / f"{name}.enc")
        return self.sent[before:], out.getvalue()


class WeeklyRun(WeeklyBase):
    def close_open_drafts(self, status="published"):
        data = ledger.load()
        for chs in data.values():
            chs["reddit"]["status"] = status
        ledger.save(data)

    def test_monday_run_sends_five_draft_emails(self):
        sent, log = self.run_week(date(2026, 11, 2), PASTED)
        drafts = [(s, b) for s, b in sent if s.startswith("Reddit draft")]
        self.assertEqual(len(drafts), 5)
        data = ledger.load()
        ids = sorted(k for k in data)
        self.assertEqual(ids, [f"2026110 2".replace(" ", "") + str(n) for n in range(1, 6)] if False else [f"20261102{n}" for n in range(1, 6)])
        for aid in ids:
            e = data[aid]["reddit"]
            self.assertEqual(set(e), {"status", "qhash", "batch", "timestamp"})   # nothing but status + hash
            self.assertEqual((e["status"], e["batch"]), ("draft", "20261102"))
        for (subject, body), aid in zip(drafts, ids):
            self.assertEqual(body.count("<a href"), 2)                                 # Approve / Reject only
            self.assertIn(f"title=approve%3A{aid}%3Areddit", body)
            self.assertIn(f"title=reject%3A{aid}%3Areddit", body)
        text = html.unescape(" ".join(b for _, b in drafts))
        self.assertNotIn("Disclosure:", text)
        self.assertNotIn("Offenlegung:", text)                                       # no disclosure line in r/MaisonDeCocon
        self.assertIn("Mehr dazu in unserem Blog:", text)                            # the German question is answered in German
        self.assertIn("utm_source=reddit&utm_medium=social&utm_campaign=", text)
        self.assertEqual(text.count("Search Console (last 7 days)"), 3)               # 3 from GSC
        self.assertEqual(text.count("findquestions list pasted by you"), 2)          # 2 pasted
        self.assertIn("findquestions list pasted by you", text)
        self.assertIn("Check before approving", text)                                # flags are shown prominently

    def test_nothing_private_is_committed_logged_or_stored_in_plaintext(self):
        sent, log = self.run_week(date(2026, 11, 2), PASTED)
        secrets_ = ["flanell oder biber", "duvet size fits a double bed", "first wash before use", "throw blanket material is best", "Short answer"]
        ledger_text = Path("state/ledger.json").read_text(encoding="utf-8")
        for s in secrets_:
            self.assertNotIn(s, ledger_text)
            self.assertNotIn(s.lower(), log.lower())
        for f in Path("reddit-out").glob("*.enc"):
            raw = f.read_bytes()
            for s in secrets_:
                self.assertNotIn(s.encode(), raw)                                    # encrypted at rest
        self.assertTrue(list(Path("reddit-out").glob("batch.enc")))
        self.assertFalse(list(Path("state").rglob("*reddit*")) )                     # no draft/inbox file in state/
        gi = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("reddit-out/", gi)
        self.assertIn("reddit/", gi)

    def test_decrypts_only_with_the_right_key(self):
        self.run_week(date(2026, 11, 2))
        blob = Path("reddit-out/batch.enc").read_bytes()
        self.assertEqual(len(reddit_store.decrypt(blob)["drafts"]), 5)
        from cryptography.fernet import Fernet
        with mock.patch.dict(os.environ, {"REDDIT_ARTIFACT_KEY": Fernet.generate_key().decode()}):
            with self.assertRaises(reddit_store.StoreError):
                reddit_store.decrypt(blob)

    def test_next_weeks_run_skips_questions_already_used(self):
        pasted = PASTED
        self.run_week(date(2026, 11, 2), pasted)
        first = {d["question"] for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"]}
        self.close_open_drafts()                                                     # all 5 posted: slots are free again
        sent, _ = self.run_week(date(2026, 11, 9), pasted)                           # same inputs a week later
        second = {d["question"] for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"]}
        self.assertTrue(second)
        self.assertEqual(first & second, set())                                      # nothing is drafted twice
        self.assertEqual({q for q in ledger.load() if q.startswith("20261109")} & {q for q in ledger.load() if q.startswith("20261102")}, set())
        hashes = [e["reddit"]["qhash"] for e in ledger.load().values()]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_unused_pasted_questions_are_carried_over_in_the_encrypted_pool(self):
        pasted = PASTED + " | What is the story behind the muslin bedding trend?"      # a third match: one slot too many
        self.run_week(date(2026, 11, 2), pasted)
        pool = reddit_store.decrypt(Path("reddit-out/pool.enc").read_bytes())
        self.assertGreaterEqual(len(pool["questions"]), 1)
        self.close_open_drafts()
        sent, _ = self.run_week(date(2026, 11, 9), "")                               # nothing pasted this week: pool is used
        self.assertTrue(any("findquestions list pasted by you" in html.unescape(b) for _, b in sent))

    def test_search_console_failure_is_reported_and_never_replaced_by_old_data(self):
        sent, log = self.run_week(date(2026, 11, 2), gsc_ok=False)
        self.assertTrue(any("Search Console unavailable" in s for s, _ in sent))
        drafts = [b for s, b in sent if s.startswith("Reddit draft")]
        self.assertTrue(drafts)                                                       # FAQ source still produces drafts
        self.assertNotIn("Search Console (last 7 days)", html.unescape(" ".join(drafts)))

    def test_held_article_is_never_a_link_target_or_source(self):
        self.gsc_candidates = [gsc_row("are down pillows warm", "https://www.maisondecocon.com/en/blogs/blog/bamboo-tencel-down-pillow", 99)]
        sent, _ = self.run_week(date(2026, 11, 2), "Which down pillow is warm? | Is a bamboo down pillow good?")
        text = html.unescape(" ".join(b for s, b in sent if s.startswith("Reddit draft")))
        self.assertNotIn("bamboo-tencel-down-pillow", text)
        self.assertNotIn("Bamboo, Tencel or Down Pillows", text)


class ManualPublishing(WeeklyBase):
    def setUp(self):
        super().setUp()
        self.run_week(date(2026, 11, 2))
        self.ids = sorted(ledger.load())
        self.base_env = {**tp.MainFlow.env, "RESULT_FILE": "result.md", "GITHUB_TOKEN": "gh-token-xxxx", "REDDIT_URL": ""}

    def act(self, title, url="", today=date(2026, 11, 3)):
        env = {**self.base_env, "TITLE": title, "REDDIT_URL": url}
        before = len(self.sent)
        with mock.patch.dict(os.environ, env), mock.patch.object(publish, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(today.year, today.month, today.day, 9, 0, tzinfo=__import__("datetime").timezone.utc)
            try:
                publish.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, self.sent[before:], Path("result.md").read_text(encoding="utf-8")

    def test_approve_sends_the_copy_ready_email_with_a_suggested_day(self):
        qid = self.ids[0]
        code, sent, result = self.act(f"approve:{qid}:reddit")
        self.assertEqual(code, 0)
        e = ledger.load()[qid]["reddit"]
        self.assertEqual((e["status"], e["post_on"]), ("approved", "2026-11-04"))     # tomorrow
        subject, body = sent[-1]
        self.assertTrue(subject.startswith("Ready to post (Wednesday 2026-11-04)"))
        draft = next(d for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"] if d["id"] == qid)
        text = html.unescape(body)
        self.assertIn(draft["title"], text)
        self.assertIn(draft["body"].split("\n")[0], text)
        self.assertIn("Wednesday 2026-11-04", text)
        self.assertIn(f"<code>{qid}</code>", body)
        self.assertIn("channel</code> = <code>reddit", body)
        self.assertIn("/actions/workflows/social-publish.yml", body)
        # the public issue comment / result file holds no draft text
        self.assertNotIn(draft["title"], result)
        self.assertNotIn(draft["body"][:40], result)
        self.assertIn("approved", result)

    def test_one_post_per_day_suggestions(self):
        days = []
        for qid in self.ids[:3]:
            self.act(f"approve:{qid}:reddit")
            days.append(ledger.load()[qid]["reddit"]["post_on"])
        self.assertEqual(days, ["2026-11-04", "2026-11-05", "2026-11-06"])

    def test_approving_twice_does_nothing(self):
        qid = self.ids[0]
        self.act(f"approve:{qid}:reddit")
        n, before = len(self.sent), ledger.load()
        code, sent, result = self.act(f"approve:{qid}:reddit")
        self.assertEqual((code, len(self.sent), ledger.load()), (0, n, before))
        self.assertIn("already approved", result)

    def test_submitting_the_url_marks_it_published(self):
        qid = self.ids[0]
        self.act(f"approve:{qid}:reddit")
        env = {k: v for k, v in self.base_env.items() if k not in ("GITHUB_TOKEN",)}
        url = "https://old.reddit.com/r/MaisonDeCocon/comments/1ab2cd/how_do_i_wash_a_muslin/?utm_source=share#c"
        code, sent, result = self.act(f"approve:{qid}:reddit", url=url, today=date(2026, 11, 4))
        e = ledger.load()[qid]["reddit"]
        self.assertEqual(code, 0)
        self.assertEqual((e["status"], e["post_id"], e["url"], e["published_on"]),
                         ("published", "1ab2cd", "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/", ledger.now()[:10]))
        self.assertIn("recorded from the URL you gave", result)
        self.assertEqual(set(e), {"status", "qhash", "batch", "timestamp", "post_on", "post_id", "url", "published_on"})
        code, _, result = self.act(f"approve:{qid}:reddit", url=url)                  # re-submit: no-op
        self.assertEqual(code, 0)
        self.assertIn("already published", result)

    def test_url_must_be_a_post_in_our_subreddit(self):
        qid = self.ids[0]
        self.act(f"approve:{qid}:reddit")
        for bad in ("https://www.reddit.com/r/bedding/comments/abc123/x/", "https://redd.it/abc123", "http://www.reddit.com/r/MaisonDeCocon/comments/abc123/",
                    "https://evil.example/r/MaisonDeCocon/comments/abc123/", "https://www.reddit.com/r/MaisonDeCocon/", "javascript:alert(1)",
                    "https://www.reddit.com/r/MaisonDeCocon/comments/abc123/x/ https://evil"):
            code, _, result = self.act(f"approve:{qid}:reddit", url=bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(ledger.load()[qid]["reddit"]["status"], "approved", bad)
        self.assertIsNone(publish.reddit_post_id("https://www.reddit.com/r/maisoncocon2/comments/abc123/"))
        self.assertEqual(publish.reddit_post_id("https://www.reddit.com/r/maisondecocon/comments/ABC123/Title"), "abc123")

    def test_url_cannot_be_submitted_before_approval(self):
        qid = self.ids[1]
        code, _, result = self.act(f"approve:{qid}:reddit", url="https://www.reddit.com/r/MaisonDeCocon/comments/abc123/")
        self.assertEqual((code, ledger.load()[qid]["reddit"]["status"]), (1, "draft"))
        self.assertIn("approve it first", result)

    def test_reject_works_for_drafts_and_approved(self):
        a, b = self.ids[0], self.ids[1]
        self.act(f"approve:{a}:reddit")
        self.act(f"reject:{a}:reddit")
        self.act(f"reject:{b}:reddit")
        self.assertEqual([ledger.load()[x]["reddit"]["status"] for x in (a, b)], ["rejected", "rejected"])
        code, _, result = self.act(f"approve:{a}:reddit")
        self.assertEqual(code, 1)

    def test_expired_or_missing_artifact_blocks_approval_clearly(self):
        qid = self.ids[0]
        (self.uploaded / "reddit-batch-20261102.enc").unlink()
        code, sent, result = self.act(f"approve:{qid}:reddit")
        self.assertEqual((code, ledger.load()[qid]["reddit"]["status"]), (1, "draft"))
        self.assertIn("90-day", result)
        self.assertTrue(any(s.startswith("[FAILED]") for s, _ in sent))

    def test_unknown_id_and_title_rules(self):
        code, _, result = self.act("approve:202611029:reddit")
        self.assertEqual(code, 1)
        self.assertEqual(publish.parse_title("approve:202611021:reddit"), ("approve", "202611021", ["reddit"]))
        with self.assertRaises(ValueError):
            publish.parse_title("approve:flannel-vs-flannelette:reddit")


class TopUp(WeeklyBase):
    """The weekly run tops up to 5 UNPOSTED drafts (pending + approved-not-posted)."""

    def set_status(self, statuses):
        data = ledger.load()
        for key, st in zip(sorted(data), statuses):
            data[key]["reddit"]["status"] = st
        ledger.save(data)

    def count_new(self, before):
        return len([k for k in ledger.load() if k not in before])

    def test_open_count_is_pending_plus_approved_only(self):
        data = {f"2026110{n}": {"reddit": {"status": st}} for n, st in
                enumerate(["draft", "draft", "approved", "published", "rejected"], 1)}
        data["900000000001"] = {"ig": {"status": "draft"}}                          # other channels never count
        self.assertEqual(reddit_draft.open_drafts(data), (["20261101", "20261102"], ["20261103"]))

    def test_five_open_means_the_run_is_skipped_with_a_short_email(self):
        self.run_week(date(2026, 11, 2))
        before = ledger.load()
        self.set_status(["draft", "draft", "draft", "approved", "approved"])        # 3 pending + 2 approved-not-posted
        n_mails = len(self.sent)
        calls = []
        with mock.patch.object(gsc, "pull", side_effect=lambda *a: calls.append("gsc")), \
                mock.patch.object(copy, "_call_claude", side_effect=lambda *a, **k: calls.append("claude")):
            sent, log = self.run_week(date(2026, 11, 9))
        self.assertEqual(calls, [])                                                  # nothing expensive ran
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertEqual(subject, "Reddit weekly run skipped: 5 of 5 drafts still open")
        text = html.unescape(body)
        self.assertIn("3 pending approval", text)
        self.assertIn("2 approved but not posted yet", text)
        self.assertLess(len(text), 1600)                                              # short
        self.assertEqual(sorted(ledger.load()), sorted(before))                       # no new drafts
        self.assertIn("Skipped", log)

    def test_rejected_and_published_free_slots_and_the_run_tops_up_exactly_the_gap(self):
        self.run_week(date(2026, 11, 2))
        first = set(ledger.load())
        self.set_status(["published", "rejected", "draft", "approved", "draft"])     # 3 open -> 2 free
        sent, _ = self.run_week(date(2026, 11, 9))
        drafts = [s for s, _ in sent if s.startswith("Reddit draft")]
        self.assertEqual(len(drafts), 2)
        self.assertEqual(self.count_new(first), 2)
        self.assertEqual(len(reddit_draft.open_drafts(ledger.load())[0]) + len(reddit_draft.open_drafts(ledger.load())[1]), 5)

    def test_one_free_slot_gives_one_draft_and_nothing_open_gives_up_to_five(self):
        self.run_week(date(2026, 11, 2))
        first = set(ledger.load())
        self.set_status(["published", "draft", "draft", "draft", "draft"])
        sent, _ = self.run_week(date(2026, 11, 9))
        self.assertEqual(self.count_new(first), 1)
        self.set_status(["published"] * 6)                                              # nothing open: full capacity...
        before = set(ledger.load())
        sent, _ = self.run_week(date(2026, 11, 16))
        made = len([s for s, _ in sent if s.startswith("Reddit draft")])
        self.assertEqual(made, self.count_new(before))
        self.assertTrue(1 <= made <= 5)                                                  # ...but never more than the questions left (used ones are skipped)

    def test_manual_runs_follow_the_same_rule(self):
        self.run_week(date(2026, 11, 2))
        n = len(ledger.load())
        sent, _ = self.run_week(date(2026, 11, 2), "Which throw blanket material is best for winter?")   # same day, manual
        self.assertEqual(len(ledger.load()), n)                                       # still 5 open: skipped
        self.assertTrue(sent[0][0].startswith("Reddit weekly run skipped"))

    def test_skip_email_never_contains_draft_text(self):
        self.run_week(date(2026, 11, 2))
        sent, _ = self.run_week(date(2026, 11, 9))
        body = html.unescape(sent[0][1])
        for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"]:
            self.assertNotIn(d["question"], body)
            self.assertNotIn(d["title"], body)


class Redraft(WeeklyBase):
    def setUp(self):
        super().setUp()
        self.run_week(date(2026, 11, 2))
        self.batch_before = reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())
        self.uploaded_before = (self.uploaded / "reddit-batch-20261102.enc").read_bytes()
        self.ids = sorted(ledger.load())

    def run_redraft(self, draft_id, claude=None):
        before = len(self.sent)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"REDRAFT_ID": draft_id}), contextlib.redirect_stdout(out), \
                mock.patch.object(gsc, "pull", side_effect=AssertionError("a re-draft must not pull Search Console")):
            if claude:
                with mock.patch.object(copy, "_call_claude", side_effect=claude):
                    code = self._main()
            else:
                code = self._main()
        if Path("reddit-out/batch.enc").exists():
            shutil.copy("reddit-out/batch.enc", self.uploaded / "reddit-batch-20261102.enc")
        return code, self.sent[before:], out.getvalue()

    def _main(self):
        try:
            reddit_draft.main(date(2026, 11, 9))
            return 0
        except SystemExit as e:
            return e.code

    @staticmethod
    def clean_claude(s, u, m, max_tokens=0):
        body = ("Flannel and Biber are both brushed cotton, so the difference is mostly the name.\n\n"
                "A typical flannel duvet cover weighs about 180 g/m2 and a 135 x 200 cm duvet is the usual choice for one person. " * 3
                + "\n\n[[LINK]]")
        return json.dumps({"title": "Neuer Titel?", "body": body, "unsupported": []})

    def test_only_the_chosen_draft_is_replaced_and_its_new_email_is_sent(self):
        target = self.ids[0]
        old = next(d for d in self.batch_before["drafts"] if d["id"] == target)
        code, sent, log = self.run_redraft(target, self.clean_claude)
        self.assertEqual(code, 0)
        after = reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())
        self.assertEqual([d["id"] for d in after["drafts"]], [d["id"] for d in self.batch_before["drafts"]])
        for before_d, after_d in zip(self.batch_before["drafts"], after["drafts"]):
            if before_d["id"] == target:
                self.assertNotEqual(after_d["body"], before_d["body"])
                self.assertEqual(after_d["title"], "Neuer Titel?")
                for key in ("question", "lang", "source", "signals", "article_id", "link"):
                    self.assertEqual(after_d[key], old[key])                          # same question, article and link
                self.assertIn("redrafted", after_d)
            else:
                self.assertEqual(after_d, before_d)                                   # #2-#5 byte-identical
        self.assertEqual(len(sent), 1)                                                # exactly one new email
        subject, body = sent[0]
        self.assertTrue(subject.startswith("Reddit RE-DRAFT"))
        text = html.unescape(body)
        self.assertIn("Re-draft</strong> with the updated prompt", text)
        self.assertIn("Neuer Titel?", text)
        self.assertIn(f"title=approve%3A{target}%3Areddit", body)                    # the same approval link
        self.assertIn(f"title=reject%3A{target}%3Areddit", body)

    def test_ledger_is_untouched_by_a_redraft_and_nothing_private_is_logged(self):
        before = ledger.load()
        code, sent, log = self.run_redraft(self.ids[0], self.clean_claude)
        self.assertEqual(ledger.load(), before)
        for needle in ("Neuer Titel", "Flannel and Biber", self.batch_before["drafts"][0]["question"]):
            self.assertNotIn(needle.lower(), log.lower())

    def test_only_pending_drafts_can_be_redrafted(self):
        data = ledger.load()
        data[self.ids[1]]["reddit"]["status"] = "approved"
        data[self.ids[2]]["reddit"]["status"] = "published"
        ledger.save(data)
        for bad in (self.ids[1], self.ids[2], "202611029"):
            code, sent, _ = self.run_redraft(bad, self.clean_claude)
            self.assertEqual(code, 1, bad)
            self.assertTrue(sent[-1][0].startswith("[FAILED] Reddit re-draft failed"), bad)
        self.assertEqual((self.uploaded / "reddit-batch-20261102.enc").read_bytes(), self.uploaded_before)  # artifact untouched

    def test_a_held_or_unpublished_article_blocks_the_redraft(self):
        victim = next(d for d in self.batch_before["drafts"] if d["id"] == self.ids[0])
        held_index = [dict(a, published=False) if a["id"] == victim["article_id"] else a for a in INDEX]
        with mock.patch.object(ra, "fetch_index", return_value=held_index):
            code, sent, _ = self.run_redraft(self.ids[0], self.clean_claude)
        self.assertEqual(code, 1)

    def test_the_old_approval_link_publishes_the_new_version(self):
        target = self.ids[0]
        self.run_redraft(target, self.clean_claude)
        base_env = {**tp.MainFlow.env, "RESULT_FILE": "result.md", "GITHUB_TOKEN": "x", "REDDIT_URL": "", "TITLE": f"approve:{target}:reddit"}
        with mock.patch.dict(os.environ, base_env):
            publish.main()
        subject, body = self.sent[-1]
        self.assertTrue(subject.startswith("Ready to post"))
        self.assertIn("Neuer Titel?", html.unescape(body))


class NoSourceTalk(unittest.TestCase):
    CLEAN = {"title": "Is flannel warm?", "body": "Flannel is brushed cotton and holds warm air near the skin. " * 8 + "\n\n[[LINK]]", "unsupported": []}
    DIRTY = {**CLEAN, "body": CLEAN["body"] + "\nZur Optik kann ich hier nichts sagen, das steht nicht in den Quellen, auf die ich mich stütze."}
    LINK = "https://www.maisondecocon.com/en/blogs/blog/x?utm_source=reddit&utm_medium=social&utm_campaign=x"

    def test_detector_catches_source_talk_in_both_languages_and_allows_plain_it_depends(self):
        for text in ("my sources say so", "According to our article, flannel is warm.", "I can't say anything about that.",
                     "That isn't covered in the article.", "Laut dem Artikel hilft das.", "Das steht nicht in den Quellen.",
                     "Dazu kann ich hier nichts sagen.", "the information I have is limited", "auf die ich mich stütze"):
            self.assertTrue(reddit_copy.meta_hits(text), text)
        for text in ("That depends on the fabric weight.", "Das ist hier nicht abgedeckt.", "That isn't covered here.",
                     "Es kommt auf das Gewicht an.", "More detail on our blog: https://x", "Disclosure: I work for Maison de Cocon, a German bedding brand."):
            self.assertEqual(reddit_copy.meta_hits(text), [], text)

    def test_a_dirty_draft_is_retried_once_with_the_offending_phrases_named(self):
        prompts = []
        replies = [json.dumps(self.DIRTY), json.dumps(self.CLEAN)]
        with mock.patch.object(copy, "_call_claude", side_effect=lambda s, u, m, max_tokens=0: prompts.append(u) or replies.pop(0)):
            d = reddit_copy.generate("Is flannel warm?", "en", "FACTS", self.LINK, "m")
        self.assertEqual(len(prompts), 2)
        self.assertIn("WITHOUT any such wording", prompts[1])
        self.assertIn("quellen", prompts[1])
        self.assertEqual(reddit_copy.meta_hits(d["body"]), [])
        self.assertFalse(any("sources" in f for f in d["flags"]))

    def test_a_clean_draft_is_not_retried(self):
        with mock.patch.object(copy, "_call_claude", return_value=json.dumps(self.CLEAN)) as call:
            reddit_copy.generate("Is flannel warm?", "en", "FACTS", self.LINK, "m")
        self.assertEqual(call.call_count, 1)

    def test_if_the_retry_is_still_dirty_it_is_flagged_for_the_owner(self):
        with mock.patch.object(copy, "_call_claude", return_value=json.dumps(self.DIRTY)) as call:
            d = reddit_copy.generate("Is flannel warm?", "en", "FACTS", self.LINK, "m")
        self.assertEqual(call.call_count, 2)                                           # one retry, no loop
        self.assertTrue(any("talks about its sources" in f for f in d["flags"]))

    def test_the_prompt_never_uses_the_word_source_with_the_model(self):
        self.assertNotIn("SOURCE", reddit_copy.build_prompt("q", "text"))
        self.assertIn("FACTS", reddit_copy.build_prompt("q", "text"))

    def test_the_link_lead_and_disclosure_added_by_code_are_not_source_talk(self):
        d = reddit_copy.finalize(self.CLEAN, "en", self.LINK)
        self.assertEqual(reddit_copy.meta_hits(d["body"]), [])
        d = reddit_copy.finalize(self.CLEAN, "de", self.LINK)
        self.assertEqual(reddit_copy.meta_hits(d["body"]), [])


class Disclosure(unittest.TestCase):
    def test_only_for_other_subreddits(self):
        self.assertFalse(reddit_copy.needs_disclosure())
        self.assertFalse(reddit_copy.needs_disclosure("MaisonDeCocon"))
        self.assertFalse(reddit_copy.needs_disclosure("maisondecocon"))
        self.assertTrue(reddit_copy.needs_disclosure("Bedding"))
        with mock.patch.object(config, "REDDIT_SUBREDDIT", "Bedding"):
            for lang, line in (("en", "Disclosure: I work for Maison de Cocon"), ("de", "Offenlegung: Ich arbeite für Maison de Cocon")):
                d = reddit_copy.finalize({"title": "t", "body": "Answer.\n\n[[LINK]]", "unsupported": []}, lang, "https://x/y")
                self.assertEqual(d["body"].count(line), 1)
                self.assertTrue(d["body"].rstrip().endswith("."))

    def test_strip_removes_the_line_in_both_languages_and_tidies_blank_lines(self):
        old = "Answer one.\n\nMore detail on our blog: https://x\n\nDisclosure: I work for Maison de Cocon, a German bedding brand."
        self.assertEqual(reddit_copy.strip_disclosure(old), "Answer one.\n\nMore detail on our blog: https://x")
        de = "Antwort.\n\nOffenlegung: Ich arbeite für Maison de Cocon, eine deutsche Bettwäsche-Marke."
        self.assertEqual(reddit_copy.strip_disclosure(de), "Antwort.")
        self.assertEqual(reddit_copy.strip_disclosure("No disclosure here."), "No disclosure here.")

    def test_the_prompt_no_longer_asks_for_or_adds_a_disclosure(self):
        self.assertIn("Do not write any disclosure or affiliation line", reddit_copy._system("en"))
        self.assertNotIn("it is added for you", reddit_copy._system("en"))


class UrlForms(unittest.TestCase):
    ID = "1ab2cd"
    GOOD = [
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/how_do_i_wash_a_muslin/",
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/how_do_i_wash_a_muslin",        # no trailing slash
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/",                              # no slug
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd",
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/x/?utm_source=share&utm_medium=web3x&utm_name=y",
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/#c",
        "https://old.reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/",
        "https://new.reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/",
        "https://np.reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/",
        "https://m.reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/",
        "https://reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/",
        "https://www.reddit.com/r/maisondecocon/comments/1AB2CD/Slug/",                         # case
        "  https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/  ",                     # pasted with blanks
        "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/slug/ofabcde/",                 # a comment permalink: same post
    ]

    def test_every_post_url_shape_resolves_to_the_same_post_id(self):
        for url in self.GOOD:
            self.assertEqual(publish.reddit_post_id(url), self.ID, url)

    def test_things_that_are_not_posts_in_our_subreddit_are_rejected(self):
        for bad in ("https://www.reddit.com/r/bedding/comments/1ab2cd/x/", "https://www.reddit.com/r/MaisonDeCocon/",
                    "https://www.reddit.com/r/MaisonDeCocon/comments/", "https://redd.it/1ab2cd",
                    "http://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/", "https://evil.example/r/MaisonDeCocon/comments/1ab2cd/",
                    "https://www.reddit.com.evil.example/r/MaisonDeCocon/comments/1ab2cd/", "https://www.reddit.com/user/x/comments/1ab2cd/",
                    "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/x https://evil", "javascript:alert(1)", ""):
            self.assertIsNone(publish.reddit_post_id(bad), bad)

    def test_share_links_are_recognised(self):
        for url in ("https://www.reddit.com/r/MaisonDeCocon/s/AbC123xyZ", "https://www.reddit.com/r/MaisonDeCocon/s/AbC123xyZ/",
                    "https://old.reddit.com/r/MaisonDeCocon/s/AbC123xyZ?utm_source=share"):
            self.assertEqual(publish.reddit_share_id(url), "AbC123xyZ", url)
            self.assertIsNone(publish.reddit_post_id(url))
        self.assertIsNone(publish.reddit_share_id("https://www.reddit.com/r/bedding/s/AbC123xyZ"))
        self.assertIsNone(publish.reddit_share_id("https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/x/"))

    @staticmethod
    def resp(status, location=None):
        return mock.Mock(status_code=status, headers={"Location": location} if location else {})

    def test_share_link_resolves_through_redirects_and_never_leaves_reddit(self):
        share = "https://www.reddit.com/r/MaisonDeCocon/s/AbC123xyZ"
        canon = "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/how_do_i/?share_id=abc"
        calls = []
        with mock.patch.object(publish.requests, "get", side_effect=lambda u, **k: calls.append((u, k)) or self.resp(301, canon)):
            self.assertEqual(publish.resolve_share(share), "1ab2cd")
        self.assertEqual(calls[0][0], share)
        self.assertFalse(calls[0][1]["allow_redirects"])                                   # hops are checked by hand
        hops = [self.resp(302, "/r/MaisonDeCocon/s/Zz9"), self.resp(302, canon)]          # relative hop, then the post
        with mock.patch.object(publish.requests, "get", side_effect=lambda u, **k: hops.pop(0)):
            self.assertEqual(publish.resolve_share(share), "1ab2cd")

    def test_share_resolution_failures(self):
        share = "https://www.reddit.com/r/MaisonDeCocon/s/AbC123xyZ"
        for case in (self.resp(403), self.resp(429), self.resp(200), self.resp(302, "https://evil.example/x")):
            with mock.patch.object(publish.requests, "get", return_value=case):
                with self.assertRaises(publish.ShareResolveError):
                    publish.resolve_share(share)
        with mock.patch.object(publish.requests, "get", side_effect=publish.requests.ConnectionError("boom")):
            with self.assertRaises(publish.ShareResolveError):
                publish.resolve_share(share)
        loop = self.resp(302, "/r/MaisonDeCocon/s/Next1")
        with mock.patch.object(publish.requests, "get", return_value=loop) as g:
            with self.assertRaises(publish.ShareResolveError):
                publish.resolve_share(share, max_hops=3)
        self.assertEqual(g.call_count, 3)
        with mock.patch.object(publish.requests, "get") as g:
            with self.assertRaises(publish.ShareResolveError):
                publish.resolve_share("https://evil.example/s/x")
        g.assert_not_called()                                                                 # never contacted


class UrlSubmission(ManualPublishing):
    """Approved draft + URL input: accepted shapes, share links, failure text, ONE confirmation."""

    def approve_first(self, n=0):
        qid = self.ids[n]
        self.act(f"approve:{qid}:reddit")
        return qid

    def test_the_accepted_url_shapes_all_publish_with_the_canonical_url(self):
        for i, url in enumerate(UrlForms.GOOD[:6] + [UrlForms.GOOD[6], UrlForms.GOOD[10]]):
            qid = self.ids[i % 5] if i < 5 else self.ids[i - 5]
            if ledger.load()[qid]["reddit"]["status"] != "approved":
                self.act(f"approve:{qid}:reddit")
            if ledger.load()[qid]["reddit"]["status"] == "published":
                continue
            code, sent, result = self.act(f"approve:{qid}:reddit", url=url)
            self.assertEqual(code, 0, url)
            self.assertEqual(ledger.load()[qid]["reddit"]["url"], "https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/", url)

    def test_a_share_link_is_resolved_to_the_canonical_post_url(self):
        qid = self.approve_first()
        with mock.patch.object(publish, "resolve_share", return_value="9zz8yy") as res:
            code, sent, result = self.act(f"approve:{qid}:reddit", url="https://www.reddit.com/r/MaisonDeCocon/s/AbC123xyZ")
        self.assertEqual(code, 0)
        res.assert_called_once()
        e = ledger.load()[qid]["reddit"]
        self.assertEqual((e["status"], e["post_id"], e["url"]),
                         ("published", "9zz8yy", "https://www.reddit.com/r/MaisonDeCocon/comments/9zz8yy/"))

    def assert_resubmit_text(self, text, qid):
        self.assertIn("Re-submit with the full URL.", text)
        self.assertIn("browser's address bar", text)
        for line in (f"article: {qid}", "channel: reddit", "url: <the full address-bar URL of the post>", "test_bad_token: leave unchecked"):
            self.assertIn(line, text)
        self.assertIn("Actions -> Social Publish -> Run workflow", text)
        self.assertIn("/actions/workflows/social-publish.yml", text)

    def test_a_share_link_that_cannot_be_resolved_asks_for_the_address_bar_url(self):
        qid = self.approve_first()
        with mock.patch.object(publish, "resolve_share", side_effect=publish.ShareResolveError("Reddit answered HTTP 403 without a redirect")):
            code, sent, result = self.act(f"approve:{qid}:reddit", url="https://www.reddit.com/r/MaisonDeCocon/s/AbC123xyZ")
        self.assertEqual((code, ledger.load()[qid]["reddit"]["status"]), (1, "approved"))   # stays approved, waiting for a URL
        failed = [(s, b) for s, b in sent if s.startswith("[FAILED]")]
        self.assertEqual(len(failed), 1)
        text = html.unescape(failed[0][1])
        self.assert_resubmit_text(text, qid)
        self.assertIn("share link", text)
        self.assertIn("address-bar URL of the post instead of the share link", text)
        self.assertIn("HTTP 403", text)

    def test_any_unusable_url_gets_the_same_resubmit_text(self):
        qid = self.approve_first()
        code, sent, result = self.act(f"approve:{qid}:reddit", url="https://www.reddit.com/r/MaisonDeCocon/comments/")
        self.assertEqual(code, 1)
        self.assert_resubmit_text(html.unescape(sent[-1][1]), qid)
        self.assert_resubmit_text(result, qid)                                                 # also in the run result
        self.assertEqual(ledger.load()[qid]["reddit"]["status"], "approved")

    def test_exactly_one_short_confirmation_after_the_url(self):
        qid = self.approve_first()
        code, sent, result = self.act(f"approve:{qid}:reddit", url=UrlForms.GOOD[0])
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertEqual(subject, "Published on r/MaisonDeCocon")
        text = html.unescape(body)
        self.assertIn("https://www.reddit.com/r/MaisonDeCocon/comments/1ab2cd/", text)
        draft = next(d for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"] if d["id"] == qid)
        self.assertIn(draft["article_title"], text)                                           # the article
        self.assertLess(len(text), 900)                                                       # short
        self.assertNotIn(draft["title"], text)                                                # no draft text
        self.assertNotIn(draft["body"][:40], text)

    def test_a_resubmit_sends_nothing_and_never_resolves_anything(self):
        qid = self.approve_first()
        self.act(f"approve:{qid}:reddit", url=UrlForms.GOOD[0])
        with mock.patch.object(publish, "resolve_share", side_effect=AssertionError("must not resolve")), \
                mock.patch.object(publish.requests, "get", side_effect=AssertionError("no network")):
            code, sent, result = self.act(f"approve:{qid}:reddit", url="https://www.reddit.com/r/MaisonDeCocon/s/Whatever1")
        self.assertEqual((code, sent), (0, []))
        self.assertIn("already published", result)

    def test_approving_sends_the_ready_email_but_no_confirmation(self):
        code, sent, _ = self.act(f"approve:{self.ids[0]}:reddit")
        self.assertEqual([s.split(" (")[0] for s, _ in sent], ["Ready to post"])

    def test_a_failing_confirmation_email_does_not_undo_the_publish(self):
        qid = self.approve_first()
        with mock.patch.object(approval, "send_reddit_published_email", side_effect=RuntimeError("resend down")):
            code, sent, result = self.act(f"approve:{qid}:reddit", url=UrlForms.GOOD[0])
        self.assertEqual((code, ledger.load()[qid]["reddit"]["status"]), (0, "published"))

    def test_old_approved_drafts_lose_the_disclosure_line_in_a_resent_ready_email(self):
        qid = self.ids[0]
        old = next(d for d in reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())["drafts"] if d["id"] == qid)
        old = {**old, "body": old["body"] + "\n\nDisclosure: I work for Maison de Cocon, a German bedding brand."}
        approval.send_reddit_ready_email(old, "2026-11-04", "Wednesday")
        text = html.unescape(self.sent[-1][1])
        self.assertNotIn("Disclosure:", text)
        self.assertIn(old["title"], text)
        with mock.patch.object(config, "REDDIT_SUBREDDIT", "Bedding"):
            approval.send_reddit_ready_email(old, "2026-11-04", "Wednesday")
        self.assertIn("Disclosure:", html.unescape(self.sent[-1][1]))                         # kept for other subreddits


class RedraftMany(WeeklyBase):
    def setUp(self):
        super().setUp()
        self.run_week(date(2026, 11, 2))
        self.ids = sorted(ledger.load())
        data = ledger.load()
        data[self.ids[4]]["reddit"]["status"] = "approved"                                      # one approved draft
        ledger.save(data)
        self.before = reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())

    @staticmethod
    def claude(s, u, m, max_tokens=0):
        return json.dumps({"title": "Neu?", "body": "Brushed cotton holds warm air near the skin. " * 12 + "\n\n[[LINK]]", "unsupported": []})

    def run_redraft(self, value):
        before = len(self.sent)
        with mock.patch.dict(os.environ, {"REDRAFT_ID": value}), mock.patch.object(copy, "_call_claude", side_effect=self.claude), \
                mock.patch.object(gsc, "pull", side_effect=AssertionError("no GSC")), contextlib.redirect_stdout(io.StringIO()):
            try:
                reddit_draft.main(date(2026, 11, 9))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, self.sent[before:]

    def test_pending_redrafts_every_unapproved_draft_and_leaves_the_approved_one_as_is(self):
        code, sent = self.run_redraft("pending")
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 4)                                                            # one new email per re-drafted draft
        self.assertTrue(all(s.startswith("Reddit RE-DRAFT") for s, _ in sent))
        after = reddit_store.decrypt(Path("reddit-out/batch.enc").read_bytes())
        for b, a in zip(self.before["drafts"], after["drafts"]):
            if b["id"] == self.ids[4]:
                self.assertEqual(a, b)                                                            # approved: text untouched
            else:
                self.assertEqual((a["id"], a["question"], a["link"]), (b["id"], b["question"], b["link"]))
                self.assertEqual(a["title"], "Neu?")
                self.assertNotIn("Disclosure", a["body"])
        for (subject, body), did in zip(sent, self.ids[:4]):
            self.assertIn(f"title=approve%3A{did}%3Areddit", body)
        self.assertEqual({k: v["reddit"]["status"] for k, v in ledger.load().items()},
                         {**{i: "draft" for i in self.ids[:4]}, self.ids[4]: "approved"})

    def test_comma_separated_ids_and_one_bad_id_does_not_block_the_others(self):
        code, sent = self.run_redraft(f"{self.ids[0]}, {self.ids[1]}")
        self.assertEqual((code, len(sent)), (0, 2))
        code, sent = self.run_redraft(f"{self.ids[2]},{self.ids[4]}")                             # the second is approved
        self.assertEqual(code, 1)
        self.assertTrue(sent[-1][0].startswith("[FAILED]"))
        code, sent = self.run_redraft(f"{self.ids[2]},202511019")                                 # another batch / unknown
        self.assertEqual(code, 1)

    def test_pending_with_nothing_pending_is_a_clear_failure(self):
        data = ledger.load()
        for v in data.values():
            v["reddit"]["status"] = "approved"
        ledger.save(data)
        code, sent = self.run_redraft("pending")
        self.assertEqual(code, 1)


class ModelJson(unittest.TestCase):
    OK = {"title": "T?", "body": "Brushed cotton holds warm air. " * 15 + "\n\n[[LINK]]", "unsupported": []}
    LINK = "https://www.maisondecocon.com/en/blogs/blog/x?utm_source=reddit&utm_medium=social&utm_campaign=x"

    def test_tolerant_parsing(self):
        good = json.dumps(self.OK)
        self.assertEqual(reddit_copy.parse_model_json(good)["title"], "T?")
        self.assertEqual(reddit_copy.parse_model_json("```json\n" + good + "\n```")["title"], "T?")
        self.assertEqual(reddit_copy.parse_model_json("Here is the post:\n" + good + "\nHope this helps.")["title"], "T?")
        raw_newline = '{"title": "T?", "body": "line one\nline two", "unsupported": []}'     # a raw newline inside a string
        with self.assertRaises(ValueError):
            json.loads(raw_newline)                                                          # strict JSON rejects it...
        self.assertEqual(reddit_copy.parse_model_json(raw_newline)["body"], "line one\nline two")   # ...we accept it

    def test_unusable_replies_raise_without_leaking_content(self):
        for bad in ("", "no json at all", '{"title": "cut off", "body": "text that never ends', '{"title": "x"}', "[1, 2]"):
            with self.assertRaises(ValueError) as cm:
                reddit_copy.parse_model_json(bad)
            self.assertNotIn("never ends", str(cm.exception))
            self.assertNotIn("cut off", str(cm.exception))

    def test_a_bad_reply_is_retried_once_with_a_strict_reminder_then_succeeds(self):
        replies = ['{"title": "cut off", "body": "text that never ends', json.dumps(self.OK)]
        prompts = []
        with mock.patch.object(copy, "_call_claude", side_effect=lambda s, u, m, max_tokens=0: prompts.append((u, max_tokens)) or replies.pop(0)):
            d = reddit_copy.generate("Is flannel warm?", "en", "FACTS", self.LINK, "m")
        self.assertEqual(len(prompts), 2)
        self.assertIn("ONLY one valid JSON object", prompts[1][0])
        self.assertTrue(all(t == reddit_copy.MAX_TOKENS for _, t in prompts))
        self.assertGreaterEqual(reddit_copy.MAX_TOKENS, 3000)                                  # no more cut-off replies
        self.assertIn(self.LINK, d["body"])

    def test_two_bad_replies_fail_with_a_clear_error(self):
        with mock.patch.object(copy, "_call_claude", return_value="not json"):
            with self.assertRaises(ValueError):
                reddit_copy.generate("Is flannel warm?", "en", "FACTS", self.LINK, "m")

    def test_source_talk_retry_still_works_with_the_new_parsing(self):
        dirty = {**self.OK, "body": self.OK["body"] + "\nDas steht nicht in den Quellen."}
        replies = [json.dumps(dirty), json.dumps(self.OK)]
        with mock.patch.object(copy, "_call_claude", side_effect=lambda *a, **k: replies.pop(0)) as call:
            d = reddit_copy.generate("Is flannel warm?", "en", "FACTS", self.LINK, "m")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(reddit_copy.meta_hits(d["body"]), [])


class Workflows(unittest.TestCase):
    def load(self, name):
        return yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / name).read_text(encoding="utf-8"))

    def test_reddit_draft_workflow_is_weekly_monday_with_a_questions_input_and_90_day_encrypted_artifacts(self):
        wf = self.load("social-reddit-draft.yml")
        on = wf.get(True) or wf.get("on")
        self.assertEqual(on["schedule"][0]["cron"].split()[4], "1")                  # Monday
        self.assertIn("questions", on["workflow_dispatch"]["inputs"])
        steps = wf["jobs"]["draft"]["steps"]
        uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
        self.assertEqual({u["with"]["path"] for u in uploads}, {"reddit-out/batch.enc", "reddit-out/pool.enc"})
        self.assertTrue(all(u["with"]["retention-days"] == 90 for u in uploads))
        run = next(s for s in steps if s.get("id") == "draft")
        self.assertEqual(run["env"]["QUESTIONS"], "${{ inputs.questions }}")        # via env, never in a shell line
        self.assertEqual(run["env"]["REDRAFT_ID"], "${{ inputs.redraft }}")
        self.assertIn("redraft", on["workflow_dispatch"]["inputs"])
        self.assertNotIn("inputs.", run["run"])
        self.assertEqual(wf["permissions"]["actions"], "read")

    def test_reddit_approval_issues_get_no_comment(self):
        steps = self.load("social-publish.yml")["jobs"]["publish"]["steps"]
        comment = next(s for s in steps if s.get("name") == "Comment result and close the issue")
        close_only = next(s for s in steps if s.get("name", "").startswith("Close the issue (Reddit"))
        self.assertIn("!contains(github.event.issue.title, ':reddit')", comment["if"])      # others unchanged
        self.assertIn("contains(github.event.issue.title, ':reddit')", close_only["if"])
        self.assertNotIn("comment", close_only["run"])                                        # no `gh issue comment`
        self.assertIn("gh issue close", close_only["run"])

    def test_redraft_input_accepts_one_several_or_pending(self):
        on = self.load("social-reddit-draft.yml")
        desc = (on.get(True) or on.get("on"))["workflow_dispatch"]["inputs"]["redraft"]["description"]
        self.assertIn("pending", desc)

    def test_publish_workflow_takes_the_reddit_url_via_env(self):
        wf = self.load("social-publish.yml")
        on = wf.get(True) or wf.get("on")
        self.assertIn("url", on["workflow_dispatch"]["inputs"])
        self.assertIn("reddit", on["workflow_dispatch"]["inputs"]["channel"]["options"])
        step = next(s for s in wf["jobs"]["publish"]["steps"] if s.get("id") == "run")
        self.assertEqual(step["env"]["REDDIT_URL"], "${{ inputs.url }}")
        self.assertNotIn("inputs.", step["run"])
        self.assertEqual(wf["permissions"]["actions"], "read")


if __name__ == "__main__":
    unittest.main()
