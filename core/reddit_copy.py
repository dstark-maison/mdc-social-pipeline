"""Reddit post drafts: a full, practical answer in the post itself.

Rules (owner's brief): no sales tone; brand affiliation stated plainly; at most ONE link, to our
matching article, with utm_source=reddit&utm_medium=social&utm_campaign={handle}; English by default,
German when the question is German; FACTS ONLY from our blog article (the only source the model
sees). Anything the code cannot trace to that article is flagged for the owner in the email.
"""

import json
import re

from core import config, copy

LINK_TOKEN = "[[LINK]]"
DISCLOSURE = {
    "en": "Disclosure: I work for Maison de Cocon, a German bedding brand.",
    "de": "Offenlegung: Ich arbeite für Maison de Cocon, eine deutsche Bettwäsche-Marke.",
}
LINK_LEAD = {"en": "More detail on our blog:", "de": "Mehr dazu in unserem Blog:"}

# r/MaisonDeCocon is the brand's OWN community, so posts there carry no disclosure line. The text is kept for
# the day we post in someone else's subreddit (config.REDDIT_SUBREDDIT != our own).
OWN_SUBREDDITS = {"maisondecocon"}


def needs_disclosure(subreddit=None):
    return (subreddit or config.REDDIT_SUBREDDIT).lower() not in OWN_SUBREDDITS


def strip_disclosure(body):
    """Remove disclosure lines (either language) from an older draft, e.g. when its ready-to-post email is resent."""
    lines = [l for l in (body or "").split("\n") if not l.strip().startswith(("Disclosure:", "Offenlegung:"))]
    out = "\n".join(lines).rstrip()
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out
TITLE_MAX = 300
MIN_BODY_CHARS = 400

# Words that must not appear unless the source article itself says them.
RISKY_TERMS = ["hypoallergen", "allerg", "antibakteri", "antibacterial", "oeko-tex", "öko-tex", "gots", "down pass",
               "zertifiziert", "certified", "klinisch", "clinically", "we sell", "we stock", "wir verkaufen",
               "wir führen", "discount", "rabatt", "coupon", "gutschein", "% off", "free shipping", "kostenloser versand"]
_URL_RE = re.compile(r"https?://\S+")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _system(lang):
    language = "German" if lang == "de" else "English"
    return f"""You write a Reddit post for r/MaisonDeCocon on behalf of Maison de Cocon, a German bedding brand.
The post answers ONE question from a reader. Write the whole post in {language}.

Output ONLY valid JSON, no markdown fences, no preamble:
{{"title": "...", "body": "...", "unsupported": ["..."]}}

Rules:
- title: the reader's question phrased naturally for a Reddit title, at most {TITLE_MAX} characters, no brand name, no clickbait.
- body: the FULL answer in the post itself, practical and concrete (what to do, what to look for, simple numbers),
  roughly 120-300 words, short paragraphs or a short list. Calm, helpful, no sales tone, no superlatives, no call to buy,
  no discount, no emojis, no hashtags. Do not tell people to click anything for the answer.
- Use ONLY the facts given below under FACTS. Do not add outside knowledge, do not invent numbers, standards,
  certifications, health/allergy effects, or statements about which products we stock or sell.
- NEVER mention or allude to where the facts come from or to what you can or cannot see, know or verify: no "my sources",
  "the sources", "the article", "the text", "the information I have", "according to ...", and no sentences such as
  "I can't say anything about X because it isn't covered". Write as a person who simply knows this topic.
- If part of the question is not covered by the facts, LEAVE THAT PART OUT. Where it genuinely helps the reader you may say
  plainly that it depends (on the fabric, the product, the person) or that it isn't covered here, without giving a reason.
- Put the literal token {LINK_TOKEN} on its own line at the very end of the body, once. Do NOT write any URL yourself.
- Do not write any disclosure or affiliation line (the post goes to the brand's own subreddit).
- unsupported: a list for the owner only (never part of the post): every statement in your body that is NOT directly
  supported by the FACTS (empty list if none).
"""


def build_prompt(question, source):
    return f"Question: {question}\n\nFACTS (the only allowed facts):\n{source}"


# Meta-commentary about sources or about what the writer can/cannot see. Never wanted in a post.
META_PATTERNS = [
    r"\b(?:my|our|the|these|those|your)\s+sources?\b", r"\bsources?\s+(?:i|we)\s+(?:use|rely|have|used)\b",
    r"\baccording\s+to\s+(?:the|our|this)\s+(?:article|text|blog|information|facts)\b",
    r"\b(?:in|from)\s+(?:the|this|our)\s+(?:article|text|facts|information provided)\b",
    r"\b(?:i|we)\s+(?:can(?:no|')t|cannot|couldn't|don't|do not)\s+(?:see|say|tell|verify|know|comment)\b",
    r"\bthe\s+(?:information|facts|text)\s+(?:i|we)\s+(?:have|was given|were given)\b",
    r"\bquellen?\b", r"\blaut\s+(?:dem|unserem|diesem)\s+(?:artikel|text|blog)\b", r"\b(?:im|aus dem)\s+(?:artikel|text)\b",
    r"\bkann\s+ich\s+(?:hier\s+)?(?:nichts|keine)\b", r"\bsteht\s+nicht\s+in\b", r"\bauf\s+die\s+ich\s+mich\s+stütze\b",
]
_META_RE = re.compile("|".join(META_PATTERNS), re.I)


def meta_hits(text):
    """Phrases in the post that talk about sources or about what the writer can/cannot see."""
    return sorted({m.group(0).strip().lower() for m in _META_RE.finditer(text or "")})


def _numbers(text):
    return {n.replace(",", ".") for n in _NUM_RE.findall(text or "")}


def flags_for(draft, source, has_article=True):
    """Human-readable reasons the owner must check this draft (an empty list = nothing to flag)."""
    body, src = draft["body"], (source or "").lower()
    flags = []
    if not has_article:
        flags.append("NO MATCHING ARTICLE: there is no source text, so every statement is unverified.")
    if draft.get("unsupported"):
        flags.append("The model itself marked these statements as not in the article: " + "; ".join(draft["unsupported"]))
    unknown = sorted(n for n in _numbers(body) if n not in _numbers(source))
    if unknown:
        flags.append("Numbers that do not appear in the article: " + ", ".join(unknown))
    risky = [t for t in RISKY_TERMS if t in body.lower() and t not in src]
    if risky:
        flags.append("Claim wording the article does not use: " + ", ".join(risky))
    if len(body) < MIN_BODY_CHARS:
        flags.append(f"Short answer ({len(body)} characters): check that it is complete.")
    if draft.get("link_dropped"):
        flags.append("The model wrote its own URL; it was removed (only our one UTM link is allowed).")
    hits = meta_hits(body)
    if hits:
        flags.append("The post talks about its sources or what it can/cannot see (remove before posting): " + "; ".join(hits))
    return flags


def finalize(raw, lang, link):
    """Turn model JSON into the post: exactly one disclosure line and at most one link (ours)."""
    title = re.sub(r"\s+", " ", (raw.get("title") or "").strip())[:TITLE_MAX]
    body = (raw.get("body") or "").strip()
    link_dropped = bool(_URL_RE.search(body.replace(LINK_TOKEN, "")))
    body = _URL_RE.sub("", body.replace(LINK_TOKEN, "\u0000")).replace("\u0000", LINK_TOKEN)  # model URLs out
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = body.replace(LINK_TOKEN, "").rstrip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    parts = [body]
    if link:
        parts.append(f"{LINK_LEAD[lang]} {link}")
    if needs_disclosure():
        parts.append(DISCLOSURE[lang])
    return {"title": title, "body": "\n\n".join(p for p in parts if p), "unsupported": raw.get("unsupported") or [],
            "link_dropped": link_dropped}


def generate(question, lang, source, link, model):
    """-> {title, body, unsupported, link_dropped, flags}"""
    prompt = build_prompt(question, source)
    draft = finalize(json.loads(copy._call_claude(_system(lang), prompt, model, max_tokens=1400)), lang, link)
    hits = meta_hits(draft["body"])
    if hits:  # one corrective retry; anything still left is flagged for the owner
        retry = (prompt + "\n\nYour previous draft talked about its sources or about what you can/cannot see "
                 f"({'; '.join(hits)}). Rewrite the post WITHOUT any such wording: leave uncovered parts out, "
                 "or say plainly that it depends. Same JSON format.")
        draft = finalize(json.loads(copy._call_claude(_system(lang), retry, model, max_tokens=1400)), lang, link)
    draft["flags"] = flags_for(draft, source, has_article=bool(source))
    return draft
