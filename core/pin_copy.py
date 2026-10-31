"""German Pinterest copy: pin title (<=100), keyword-led description (<=500), alt text."""

import json
import re

from core import copy

TITLE_MAX, DESCRIPTION_MAX, ALT_MAX = 100, 500, 500


def fit(text, limit):
    """Trim to `limit` characters at a sentence end if possible, else a word boundary (never mid-word)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end >= limit * 0.6:
        return cut[:sentence_end + 1].strip()
    cut = cut[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
    return cut + "…"


def strip_html(html, limit=3000):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()[:limit]


def generate(article, de, model):
    """de: German texts from Shopify translations ({} if unavailable). Returns {title, description, alt_text}."""
    de_title = (de or {}).get("title")
    de_body = strip_html((de or {}).get("body_html") or (de or {}).get("summary_html"))
    system_prompt = f"""Du schreibst Pinterest-Pins für Maison de Cocon, eine deutsche Marke für ruhige,
hochwertige Schlaf- und Bettwäsche (Ton: zurückhaltend, redaktionell, nie reißerisch).

Antworte NUR mit gültigem JSON, ohne Markdown, ohne Vorrede:
{{"title": "...", "description": "...", "alt_text": "..."}}

Regeln:
- Alles auf Deutsch.
- title: höchstens {TITLE_MAX} Zeichen. {"Verwende genau diesen deutschen Artikeltitel und kürze ihn nur, wenn er länger als 100 Zeichen ist: " + de_title if de_title else "Das wichtigste Suchthema zuerst, klar und sachlich."}
- description: höchstens {DESCRIPTION_MAX} Zeichen, beginnt mit dem Haupt-Suchbegriff / dem Problem, das der Leser lösen will
  (keyword-led, zum Beispiel "Schlafen schwitzen: ..."), dann 1-2 konkrete Aussagen aus dem Artikel. Keine Hashtags,
  kein "Link in Bio", keine Emojis.
- alt_text: höchstens {ALT_MAX} Zeichen, beschreibt sachlich ein Schlafzimmer-/Bettwäsche-Motiv passend zum Thema,
  ohne Details zu erfinden.
- Nur Fakten aus dem Artikel. Niemals Gesundheits- oder Allergieversprechen (hypoallergen, antibakteriell ...),
  Zertifikate (OEKO-TEX, GOTS, Down Pass ...) oder Aussagen, dass wir ein Produkt führen, das nicht im Artikel steht.
"""
    user_prompt = (
        f"Artikeltitel (Englisch, Quelle): {article['title']}\n"
        + (f"Deutscher Titel: {de_title}\n" if de_title else "")
        + f"\nInhalt:\n{de_body or article.get('summary') or strip_html(article.get('body'))}"
    )
    out = json.loads(copy._call_claude(system_prompt, user_prompt, model, max_tokens=900))
    return {"title": fit(out.get("title") or de_title or article["title"], TITLE_MAX),
            "description": fit(out["description"], DESCRIPTION_MAX),
            "alt_text": fit(out.get("alt_text") or out["title"], ALT_MAX)}
