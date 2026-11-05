"""Environmental/eco claim gate for generated Instagram copy (claims-policy rule 4)."""
import json
import unittest
from unittest import mock

from core import copy


class FindEnvClaim(unittest.TestCase):
    def test_flags_claims_in_de_en_fr_nl(self):
        for text in [
            "Nachhaltig geschlafen", "umweltfreundliche Bettwäsche", "Bio-Baumwolle", "klimaneutral produziert", "aus recyceltem Material",
            "a sustainable choice", "eco-friendly sheets", "organic bamboo", "linge écologique", "duurzaam beddengoed", "milieuvriendelijk",
        ]:
            self.assertIsNotNone(copy.find_env_claim(text), text)

    def test_plain_words_and_standard_names_are_fine(self):
        for text in ["Tencel aus Holzfasern", "OEKO-TEX® Standard 100 prüft Schadstoffe", "Schlaf, neu gedacht.", "economy size", "Biomasse-Heizung ist kein Thema", "ecosystem"]:
            # 'Biomasse' must not trip the bio rule (word-boundary), 'economy'/'ecosystem' must not trip eco
            self.assertIsNone(copy.find_env_claim(text), text)

    def test_link_in_bio_phrases_are_exempt(self):
        for text in ["Mehr dazu im Artikel. Link in bio.", "Link in Bio", "Link in der Bio", "Den Artikel findest du über den Bio-Link.", "Alles Wichtige – Link in der bio!"]:
            self.assertIsNone(copy.find_env_claim(text), text)
        post = {"caption": "Steppung oder Kassetten? Link in bio.", "hashtags": ["Daunendecke", "Schlafkomfort"]}
        self.assertEqual(copy.env_claims_in_post(post), [])

    def test_real_bio_claims_still_block_even_next_to_link_in_bio(self):
        for text in ["Bio-Baumwolle", "Bio-Qualität", "biologisch angebaut", "aus Bio-Baumwolle. Link in bio.", "Link in Bio-Baumwolle",
                     "Bio Baumwolle für dich, Link in bio", "organic cotton – link in bio"]:
            self.assertIsNotNone(copy.find_env_claim(text), text)
        self.assertEqual(copy.env_claims_in_post({"caption": "Link in bio.", "hashtags": ["bio"]}), [("hashtag #bio", "bio")])

    def test_hashtags_are_checked(self):
        hits = copy.env_claims_in_post({"caption": "Sanft und ruhig.", "hashtags": ["Bettwäsche", "nachhaltig", "Schlafzimmer"]})
        self.assertEqual(hits, [("hashtag #nachhaltig", "nachhaltig")])


class GenerateRetry(unittest.TestCase):
    ARTICLE = {"title": "Tencel erklärt", "summary": "Die Faser wird aus Holz gewonnen."}

    def test_retry_then_ok(self):
        bad = json.dumps({"caption": "Nachhaltig weich.", "hashtags": ["tencel"]})
        good = json.dumps({"caption": "Aus Holzfasern gewonnen.", "hashtags": ["tencel"]})
        with mock.patch.object(copy, "_call_claude", side_effect=[bad, good]) as m:
            out = copy.generate_instagram_caption(self.ARTICLE, "model")
        self.assertEqual(out["caption"], "Aus Holzfasern gewonnen.")
        self.assertEqual(m.call_count, 2)
        self.assertIn("nachhaltig", m.call_args_list[1][0][1].lower())  # the retry names the offending term

    def test_second_violation_raises(self):
        bad = json.dumps({"caption": "Wunderbar umweltfreundlich.", "hashtags": []})
        with mock.patch.object(copy, "_call_claude", side_effect=[bad, bad]):
            with self.assertRaises(ValueError):
                copy.generate_instagram_caption(self.ARTICLE, "model")


if __name__ == "__main__":
    unittest.main()
