"""Copy generation via Claude."""

import json
import os
import re
from pathlib import Path

from core import http

BRAND_VOICE_PATH = Path("brand-voice.md")


def _call_claude(system_prompt, user_prompt, model, max_tokens=1000):
    resp = http.request(
        "POST", "https://api.anthropic.com/v1/messages", "anthropic",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
    )
    content_blocks = resp.json()["content"]
    # content[0] isn't reliably the text block — claude-sonnet-5 includes a
    # leading "thinking" block by default (confirmed live).
    text_blocks = [b["text"] for b in content_blocks if b.get("type") == "text"]
    if not text_blocks:
        raise RuntimeError(f"No text block in Claude response: {content_blocks}")
    text = text_blocks[0].strip()
    return re.sub(r"^```json\s*|\s*```$", "", text)


# --- Environmental / eco claims (claims-policy rule 4, EU 2024/825) --------------------------------------------------
# The prompt forbids them, but a prompt is not a gate: this check runs on every generated caption and hashtag.
ENV_CLAIM_RE = re.compile(
    r"nachhaltig|umweltfreundlich|umweltschonend|umweltbewusst|ressourcenschonend|\böko(?!-?tex)|ökologisch|klimaneutral|recycel|recycling|biologisch abbaubar|\bbio(?:-\w+)?\b|"
    r"sustainab|eco[- ]?friendl|eco[- ]?conscious|eco[- ]?responsible|environmentally[- ](?:friendly|conscious|responsible)|\bgreener\b|\beco\b|\borganic|\brecycl|biodegrad|carbon[- ]neutral|"
    r"écologi|écoresponsable|durabilit|\bduurzaam|milieuvriendelijk",
    re.I,
)
_STANDARD_RE = re.compile(r"oeko[- ]?tex|öko[- ]?tex", re.I)
# The required Instagram CTA ("Link in der Bio", "Link in bio", "Bio-Link") is the profile bio, not "organic":
# it must not trip the \bbio\b rule, or every caption fails (incident 2026-11-07).
_BIO_LINK_RE = re.compile(
    r"\blink\s+(?:in|im)\s+(?:der\s+|unserer\s+|meiner\s+|our\s+|the\s+)?(?:profil-?)?bio\b|\b(?:profil-?)?bio[- ]?link\b",
    re.I,
)


def find_env_claim(text):
    """First environmental claim term in `text` (OEKO-TEX name and link-in-bio CTA ignored), or None."""
    m = ENV_CLAIM_RE.search(_BIO_LINK_RE.sub(" ", _STANDARD_RE.sub(" ", text or "")))
    return m.group(0) if m else None


def env_claims_in_post(post):
    """Offending terms in a generated {caption, hashtags} post."""
    hits = []
    for field, text in [("caption", post.get("caption", ""))] + [("hashtag #" + h, h) for h in post.get("hashtags", [])]:
        term = find_env_claim(text)
        if term:
            hits.append((field, term))
    return hits


def generate_instagram_caption(article, model):
    """Draft a caption + hashtags from the blog article."""
    brand_voice = BRAND_VOICE_PATH.read_text() if BRAND_VOICE_PATH.exists() else ""

    system_prompt = f"""You write Instagram captions for Maison de Cocon, a
quiet-luxury German sleep and bedding brand. Voice: restrained, editorial,
premium — never hype-y, never emoji-stuffed. Tagline: "Sleep, reconsidered."
/ German: "Schlaf, neu gedacht."

{('Brand voice guide:' + brand_voice) if brand_voice else ''}

You will be given a blog post's title and content. Write the caption in
German. Write a companion Instagram caption that teases the post and drives readers to read more via
the bio link (captions cannot contain clickable links). End with a plain
"Link in bio" call to action (in German, matching the brand's primary
market) — do not include the URL itself in the caption.

Never write health or allergy claims (hypoallergenic, allergikerfreundlich,
antibakteriell …), environmental or eco claims (nachhaltig, umweltfreundlich,
sustainable, eco-friendly, organic/bio, recycled, klimaneutral …; also not as
hashtags), certification claims (OEKO-TEX, GOTS, Down Pass …), or
statements that we stock or sell a product that is not part of the post's
content. Stay with what the post itself states.

Respond ONLY with valid JSON, no markdown fences, no preamble:
{{"caption": "...", "hashtags": ["...", "..."]}}

Caption must be under 2200 characters. Provide 8-15 relevant hashtags,
without the # symbol (added later), mixing brand/niche/broad tags."""

    user_prompt = (
        f"Title: {article['title']}\n\nContent:\n"
        f"{article.get('summary') or article.get('body', '')[:3000]}"
    )
    post = json.loads(_call_claude(system_prompt, user_prompt, model))
    hits = env_claims_in_post(post)
    if hits:  # one retry that names the offending terms, then fail loudly: a draft with an eco claim must never reach approval
        terms = ", ".join(f"{t!r} in {f}" for f, t in hits)
        post = json.loads(_call_claude(
            system_prompt,
            user_prompt + chr(10) * 2 + f"Your previous draft used environmental claim wording ({terms}). Rewrite without any environmental or eco claim, including hashtags.",
            model,
        ))
        hits = env_claims_in_post(post)
        if hits:
            raise ValueError("Instagram draft contains environmental claim wording: " + ", ".join(f"{t!r} in {f}" for f, t in hits))
    return post


# --- Facebook ---------------------------------------------------------------
# The Facebook message is derived from the Instagram caption (same German core,
# no second model call, nothing new to hallucinate): the "Link in bio" call to
# action is replaced by the clickable article link, with fewer hashtags.
_LINK_IN_BIO_RE = re.compile(r"[^.!?\n]*\blink\s+in\s+(?:der\s+)?bio\b[^.!?\n]*[.!?]?", re.I)
FACEBOOK_LINK_LINE = "Zum Artikel:"
FACEBOOK_MAX_HASHTAGS = 4


def facebook_message(caption, hashtags, article_url_with_utm, max_hashtags=FACEBOOK_MAX_HASHTAGS):
    body = _LINK_IN_BIO_RE.sub("", caption)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    parts = [body, f"{FACEBOOK_LINK_LINE} {article_url_with_utm}"]
    tags = " ".join(f"#{h}" for h in hashtags[:max_hashtags])
    if tags:
        parts.append(tags)
    return "\n\n".join(p for p in parts if p)
