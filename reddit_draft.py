"""
Maison de Cocon: weekly Reddit drafts for r/MaisonDeCocon (Monday cron + manual run).

Inputs (all three; a missing one is reported, never replaced by old data):
  1. Search Console, a LIVE pull of the last 7 days: question-style queries (DE/EN) with
     impressions but low CTR or position 5-20, mapped to the article the page belongs to
  2. the findquestions.com list you paste into the workflow's `questions` input (the site has
     no API or export) plus unused questions carried over from earlier weeks
  3. the FAQ blocks of our blog articles
Held articles (config.HOLD_ARTICLE_IDS) are never a link target or fact source. Questions
already in the ledger (drafted, approved, published or rejected) are skipped (hash match).

Tops the open list up to config.REDDIT_MAX_OPEN (5) unposted drafts (pending + approved-but-not-posted); if 5
are already open the run is skipped with a short email. Picks the missing questions with distinct articles: up to 3 from Search Console, up to 2 pasted, the
rest from FAQ blocks (shortfalls are filled in that priority order). One draft per question,
one review email per draft (Approve / Reject).

PRIVACY (public repo): Search Console data, the pasted list and the drafts are NEVER committed
and never printed in logs. They live in encrypted artifacts (core/reddit_store.py); the ledger
gets only status + a short question hash. Publishing is manual (Reddit's API is closed): see
publish.py (approve -> copy-ready email; URL submission -> published).
"""

import os
import random
import sys
from datetime import datetime, timezone

from core import approval, config, gsc, http, ledger, questions, reddit_articles as ra, reddit_copy, reddit_store

BASE_ENV = [
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SHOPIFY_BLOG_HANDLE", "ANTHROPIC_API_KEY",
    "RESEND_API_KEY", "RESEND_FROM", "RESEND_TO", "GITHUB_REPOSITORY", "GITHUB_REF_NAME", "REDDIT_ARTIFACT_KEY",
]


def as_question(text):
    t = text.strip()
    return (t[:1].upper() + t[1:]) if t.endswith("?") else (t[:1].upper() + t[1:] + "?")


def seen_hashes(ledger_data):
    return {(chs.get("reddit") or {}).get("qhash") for chs in ledger_data.values()
            if isinstance(chs, dict) and (chs.get("reddit") or {}).get("qhash")}


def candidate(text, source, article, signals=None, score=0.0):
    return {"text": as_question(text), "lang": questions.language(text), "source": source, "article_id": article["id"],
            "signals": signals or {}, "score": score}


def from_gsc(rows, index, hold):
    """-> (candidates, stats). One per article (its best query); held articles and unmatched pages are counted, not used."""
    stats = {"rows": len(rows), "held": 0, "unmatched": 0}
    best = {}
    for r in rows:
        art = ra.match_page(r["page"], index) or ra.match_text(r["query"], ra.eligible(index, hold))
        if art is None:
            stats["unmatched"] += 1
        elif art["id"] in hold or not art["published"]:
            stats["held"] += 1
        elif art["id"] not in best:  # rows arrive best-first (impressions)
            best[art["id"]] = candidate(r["query"], "gsc", art, {k: r[k] for k in ("impressions", "clicks", "ctr", "position")},
                                        r["impressions"])
    return list(best.values()), stats


def from_pasted(texts, index, hold):
    """-> (candidates, unmatched count). Matched by distinctive-token overlap with article titles/handles."""
    eligible, cands, unmatched = ra.eligible(index, hold), [], 0
    for t in texts:
        art = ra.match_text(t, eligible)
        if art is None:
            unmatched += 1
        else:
            cands.append(candidate(t, "findquestions", art))
    return cands, unmatched


def from_faq(index, hold, seed):
    """One FAQ question per article, in a seeded (weekly-varying, reproducible) order."""
    rng = random.Random(seed)
    out = []
    for art in ra.eligible(index, hold):
        pairs = ra.faq_pairs(art["body"])
        if pairs:
            q, _ = rng.choice(pairs)
            out.append(candidate(q, "faq", art))
    rng.shuffle(out)
    return out


def select(gsc_c, pasted_c, faq_c, seen, n=config.REDDIT_DRAFTS_PER_WEEK):
    """Distinct questions AND distinct articles; the slot rule is in the module docstring."""
    chosen, used_articles, used_hashes = [], set(), set()

    def take(cands, limit):
        taken = 0
        for c in cands:
            if len(chosen) >= n or taken >= limit:
                return
            h = questions.qhash(c["text"])
            if h in seen or h in used_hashes or c["article_id"] in used_articles:
                continue
            chosen.append(c)
            used_hashes.add(h)
            used_articles.add(c["article_id"])
            taken += 1

    take(gsc_c, config.REDDIT_MAX_FROM_GSC)
    take(pasted_c, config.REDDIT_MAX_FROM_PASTED)
    take(gsc_c, n)       # shortfalls: GSC first, then pasted, then FAQ
    take(pasted_c, n)
    take(faq_c, n)
    return chosen


def build_draft(cand, index_by_id, draft_id, model):
    art = index_by_id[cand["article_id"]]
    link = ra.article_url(art, cand["lang"], ra.reddit_utm(art))
    out = reddit_copy.generate(cand["text"], cand["lang"], ra.source_text(art), link, model)
    return {"id": draft_id, "question": cand["text"], "lang": cand["lang"], "source": cand["source"],
            "signals": cand["signals"], "article_id": art["id"], "article_handle": art["handle"],
            "article_title": art["title"], "link": link, "title": out["title"], "body": out["body"],
            "flags": out["flags"], "created": ledger.now()}


def open_drafts(ledger_data):
    """Unposted Reddit drafts: (pending ids, approved-but-not-posted ids). Rejected and published do not count."""
    pending, approved = [], []
    for key, chs in sorted(ledger_data.items()):
        e = chs.get("reddit") if isinstance(chs, dict) else None
        if e and e.get("status") == ledger.DRAFT:
            pending.append(key)
        elif e and e.get("status") == ledger.APPROVED:
            approved.append(key)
    return pending, approved


def redraft_many(ids, model):
    """Re-draft pending drafts of ONE batch with the current prompt. Same ids, questions, articles and links
    (the existing approval links keep working and now approve the new version). Drafts that are not in `ids`
    (e.g. approved ones) are left exactly as they are. One new email per re-drafted draft."""
    if not ids:
        raise RuntimeError("No pending Reddit drafts to re-draft")
    data = ledger.load()
    for did in ids:
        entry = ledger.get(data, did, "reddit")
        if not entry:
            raise RuntimeError(f"No Reddit draft with id {did}")
        if entry["status"] != ledger.DRAFT:
            raise RuntimeError(f"Draft {did} is `{entry['status']}`: only drafts that are still pending can be re-drafted")
    batches = {reddit_store.batch_id_of(i) for i in ids}
    if len(batches) != 1:
        raise RuntimeError(f"Re-draft one batch at a time (got batches {sorted(batches)})")
    batch = batches.pop()
    payload = reddit_store.load_batch(batch)
    if payload is None:
        raise RuntimeError(f"The encrypted artifact of batch {batch} is gone (90-day retention)")
    by_id = {d["id"]: d for d in payload["drafts"]}
    index = {a["id"]: a for a in ra.fetch_index(os.environ["SHOPIFY_BLOG_HANDLE"])}
    done, failed = {}, []
    for did in ids:
        old = by_id.get(did)
        art = index.get(old["article_id"]) if old else None
        try:
            if old is None:
                raise RuntimeError("not in the batch artifact")
            if art is None or not art["published"] or art["id"] in config.HOLD_ARTICLE_IDS:
                raise RuntimeError("the matching article is no longer an eligible fact source (unpublished or held)")
            out = reddit_copy.generate(old["question"], old["lang"], ra.source_text(art), old["link"], model)
            done[did] = {**old, "title": out["title"], "body": out["body"], "flags": out["flags"], "redrafted": ledger.now()}
        except Exception as exc:
            failed.append((did, http.redact(exc)))
            print(f"  re-draft of {did} failed: {type(exc).__name__}")
    if done:
        reddit_store.write("batch", {"batch": batch, "drafts": [done.get(d["id"], d) for d in payload["drafts"]]})
        write_output("batch", batch)
        for did in ids:
            if did in done:
                approval.send_reddit_draft_email(done[did], redraft=True)
    print(f"Re-drafted {len(done)} of {len(ids)} draft(s) in batch {batch}; the other {len(payload['drafts']) - len(done)} "
          f"draft(s) in the batch are untouched; {sum(bool(d['flags']) for d in done.values())} with flags.")
    if failed:
        raise RuntimeError("Re-draft failed for: " + "; ".join(f"{i} ({m})" for i, m in failed))


def write_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main(today=None):
    config.require_env(BASE_ENV)
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    redraft_id = os.environ.get("REDRAFT_ID", "").strip()
    if redraft_id:
        try:
            if redraft_id.lower() == "pending":  # every draft that is still waiting for approval
                ids = open_drafts(ledger.load())[0]
            else:
                ids = [x.strip() for x in redraft_id.replace(";", ",").split(",") if x.strip()]
            redraft_many(ids, model)
        except Exception as exc:
            print(f"Re-draft failed: {type(exc).__name__}")
            approval.send_failure_email(f"Reddit re-draft failed: {redraft_id}", http.redact(exc))
            sys.exit(1)
        return
    today = today or datetime.now(timezone.utc).date()
    batch = today.strftime("%Y%m%d")
    hold = config.HOLD_ARTICLE_IDS
    pasted_text = os.environ.get("QUESTIONS", "")

    # Top up to REDDIT_MAX_OPEN unposted drafts instead of always drafting 5.
    pending, approved = open_drafts(ledger.load())
    capacity = config.REDDIT_MAX_OPEN - len(pending) - len(approved)
    if capacity <= 0:
        print(f"Skipped: {len(pending) + len(approved)} unposted drafts already open "
              f"({len(pending)} pending, {len(approved)} approved, not posted yet).")
        approval.send_reddit_skip_email(pending, approved, config.REDDIT_MAX_OPEN)
        return
    print(f"Open drafts: {len(pending)} pending + {len(approved)} approved-not-posted; drafting up to {capacity}.")

    index = ra.fetch_index(os.environ["SHOPIFY_BLOG_HANDLE"])
    index_by_id = {a["id"]: a for a in index}

    # 1. Search Console: live or nothing. Never older data.
    gsc_c, gsc_note = [], None
    try:
        pull = gsc.pull(today, config.REDDIT_MIN_IMPRESSIONS)
        gsc_c, stats = from_gsc(pull["candidates"], index, hold)
        gsc_note = f"{pull['rows']} rows, {len(pull['candidates'])} question queries, {len(gsc_c)} usable articles"
    except (gsc.GscError, http.ApiError) as exc:
        print(f"Search Console unavailable: {type(exc).__name__}")
        approval.send_failure_email("Reddit drafts: Search Console unavailable (continuing without it)", http.redact(exc))

    # 2. pasted findquestions + carry-over
    try:
        pool = reddit_store.load_pool()
    except Exception as exc:  # first run / artifact expired / no access: not fatal
        print(f"No pool artifact ({type(exc).__name__})")
        pool = {}
    pasted = questions.parse_pasted(pasted_text) + [q for q in pool.get("questions", [])
                                                     if questions.normalize(q) not in
                                                     {questions.normalize(x) for x in questions.parse_pasted(pasted_text)}]
    pasted_c, unmatched = from_pasted(pasted, index, hold)

    # 3. FAQ blocks
    faq_c = from_faq(index, hold, seed=batch)

    data = ledger.load()
    seen = seen_hashes(data)
    print(f"Sources: GSC {gsc_note or 'none'}; pasted {len(pasted)} ({len(pasted_c)} matched, {unmatched} unmatched); "
          f"FAQ {len(faq_c)}; questions already in the ledger: {len(seen)}")

    existing = None
    try:
        existing = reddit_store.load_batch(batch)  # a second run on the same day appends to the same batch
    except Exception:
        pass
    drafts = list((existing or {}).get("drafts", []))
    in_ledger = [int(k[8:]) for k in data if k.startswith(batch) and len(k) == 9 and k[8:].isdigit()]
    next_n = max(len(drafts), *in_ledger, 0) + 1
    skip = set(seen)
    failures = []
    for _ in range(3):  # replacement rounds if a draft cannot be generated
        wanted = min(capacity, config.REDDIT_DRAFTS_PER_WEEK) - (len(drafts) - len((existing or {}).get("drafts", [])))
        if wanted <= 0 or next_n > 9:
            break
        chosen = select(gsc_c, pasted_c, faq_c, skip, wanted)
        if not chosen:
            break
        for cand in chosen:
            skip.add(questions.qhash(cand["text"]))
            try:
                drafts.append(build_draft(cand, index_by_id, f"{batch}{next_n}", model))
                next_n += 1
            except Exception as exc:
                failures.append(type(exc).__name__)
                print(f"  draft failed ({cand['source']}): {type(exc).__name__}")
    new = drafts[len((existing or {}).get("drafts", [])):]

    # leftovers of the pasted list (matched, not used) stay available for later weeks
    used = {questions.normalize(d["question"]) for d in new}
    leftovers = [c["text"] for c in pasted_c if questions.normalize(c["text"]) not in used
                 and questions.qhash(c["text"]) not in seen][:config.REDDIT_POOL_MAX]
    reddit_store.write("pool", {"questions": leftovers, "updated": ledger.now()})
    if not new:
        print("No new Reddit drafts this week.")
        approval.send_failure_email("Reddit drafts: nothing to draft", "No eligible question this week "
                                    f"(failures: {failures or 'none'}).")
        return

    reddit_store.write("batch", {"batch": batch, "drafts": drafts})
    write_output("batch", batch)

    def mutate(d):
        for dr in new:
            ledger.set_entry(d, dr["id"], "reddit", status=ledger.DRAFT, qhash=questions.qhash(dr["question"]),
                             batch=batch, timestamp=ledger.now())

    ledger.update(mutate, f"Reddit drafts {batch}: {len(new)} [skip ci]")
    for dr in new:
        approval.send_reddit_draft_email(dr)
    print(f"Reddit batch {batch}: {len(new)} draft email(s) sent "
          f"({sum(d['source'] == 'gsc' for d in new)} GSC, {sum(d['source'] == 'findquestions' for d in new)} pasted, "
          f"{sum(d['source'] == 'faq' for d in new)} FAQ); {sum(bool(d['flags']) for d in new)} with flags.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
