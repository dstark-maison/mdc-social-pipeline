# MDC social pipeline (`dstark-maison/mdc-social-pipeline`, formerly `mdc-instagram-pipeline`)

Blog post → social copy → **one Resend approval email** → publish → verified in a ledger.
Instagram and Facebook (German copy for both) ship together from one approval; Pinterest and Reddit follow the same three steps.

## Flow

1. **Draft** (`social-draft.yml`, twice daily, `draft.py`): for each published article
   without a ledger entry, Claude writes the copy, the featured image is cropped to 1:1 and
   committed under `generated/` (public raw URL), the drafts are saved to
   `state/drafts/{article_id}/ig.json` and `fb.json`, and one email goes out. **Nothing is created on Instagram or Facebook.**
2. **Approve**: the email shows the Instagram and the Facebook preview and has the buttons
   **Approve both** / **IG only** / **FB only** / **Reject** (titles `approve:{id}:ig+fb`, `:ig`, `:fb`, `reject:{id}:ig+fb`).
   Each channel has its own draft file and ledger entry, so "IG only" leaves FB as `draft` and it can be approved later.
   Each button opens a prefilled GitHub issue (label `approval`). Click **Submit new issue**.
3. **Publish** (`social-publish.yml`, `publish.py`): per approved channel, in order IG then FB: posts it, reads the
   post back by id (permalink/link + caption must match) and writes `state/ledger.json`. Instagram also repoints
   the bio redirect and reads that back too. The result is commented on the issue and the issue is closed.
   *Reject* marks the draft `rejected` and publishes nothing.

Any error → failure email with the platform's raw error text. A step is never marked done
without its read-back.

## Ledger (`state/ledger.json`)

`{article_id: {channel: {status, handle, post_id, url, timestamp}}}` — the key is the **numeric Shopify article id** (handles get renamed; ids do not). The handle is stored for display only.

| status | meaning |
|---|---|
| `draft` | copy saved, nothing on the platform |
| `publishing` | publish was called but not verified; **blocks automatic retries** (could double-post). The failure email contains the exact entry to paste for both outcomes (post live / not live) |
| `published` | posted and read back; re-running is a no-op |
| `rejected` | rejected at the approval step |

## Facebook

- `channels/facebook.py`: `POST /{page-id}/photos` with the image URL and `message`, then reads the photo back by id
  (link present, caption equal to the draft) before the ledger says `published`.
- The **Page token** is fetched at runtime (`core/meta.py: page_credentials`, from `META_SYSTEM_USER_TOKEN`: the Page linked
  to `IG_USER_ID`), cached for the process only, redacted everywhere, never stored or logged.
- Copy: the Instagram caption (German) with the "Link in bio" call to action replaced by `Zum Artikel: <article url>?utm_source=facebook&utm_medium=social&utm_campaign={handle}`
  and at most 4 hashtags (`core/copy.py: facebook_message`).
- Partial failure: if IG publishes and FB fails, FB goes back to `draft` and only FB is retried (the failure email says how);
  if FB's outcome is unknown it stays `publishing` and the email contains the exact ledger entry to fix.

## Reddit (r/MaisonDeCocon): drafts, manual posting

Reddit closed new API access (31 Oct 2026) and ends the public API in March 2027, so there is **no Reddit API**: the pipeline drafts and
tracks, you post by hand.

- **Weekly draft** (`social-reddit-draft.yml`, Monday 06:47 UTC + manual, `reddit_draft.py`): picks **5 questions** with distinct
  articles from three inputs and sends **one review email per draft** (Approve / Reject), flags first:
  1. **Search Console**, a live pull of the last 7 days (same window as `seo-weekly-report`: end = today-3), question-style queries
     (DE/EN: wie, was, welche, warum, how, what, which, why, ...) with >= 3 impressions and CTR < 2% **or** position 5-20.
     Needs the secret `GOOGLE_SERVICE_ACCOUNT_JSON`. If the pull fails you get a failure email and the run continues without it;
     **old data is never used**.
  2. **findquestions.com**: it has no API or export (it emails 40 ideas), so paste the week's list into the workflow input
     `questions` (one per line, or separated by `|` or `?`). Questions not used are carried over (encrypted pool).
  3. **Blog FAQ blocks** (the FAQPage JSON-LD of every article).
  Slots: up to 3 from Search Console, up to 2 pasted, the rest from FAQ (shortfalls filled in that order). Held articles
  (`HOLD_ARTICLE_IDS`) are never a link target or fact source. Questions already in the ledger (any status) are skipped.
- **The post**: the full answer in the post itself, practical, no sales tone, no disclosure line in our own subreddit (kept for posts in other subreddits), at most ONE link to the
  matching article (`utm_source=reddit&utm_medium=social&utm_campaign={handle}`; German pages for German questions). English by
  default, German when the question is German. The model sees only the matched article as its fact source; the email lists
  everything that cannot be traced to it (unknown numbers, claim wording, model-reported gaps, no matching article).
- **Publish (manual)**: *Approve* -> a copy-ready email (title + body) with a suggested posting day (one post per day). You post it,
  then *Actions -> Social Publish -> Run workflow* with `article` = the draft id, `channel` = `reddit`, `url` = the post URL.
  The ledger then marks it `published` (recorded from your URL; it cannot be read back from Reddit).
  The URL may be a full post URL with or without title slug, trailing slash or query string, on www/old/new/np/m.reddit.com, or a
  Reddit share link (`/r/MaisonDeCocon/s/<id>`, resolved by following its redirect, not via the API). If it cannot be read, the
  failure email says "Re-submit with the full URL" and lists the exact form inputs. One short "Published on r/MaisonDeCocon"
  email follows a successful submit; Reddit approval issues get no comment (only closed).
- **Privacy** (this repo is public): Search Console data, the pasted list and the drafts are never committed or logged. They live in
  **encrypted** Actions artifacts (90 days; key = secret `REDDIT_ARTIFACT_KEY`, because artifacts of a public repo are downloadable by
  any GitHub user). The ledger holds only `status`, a 16-character hash of the question (for dedupe), the batch date and, later,
  the post id / url. A draft can be approved for 90 days; after that its artifact is gone and the question must be re-drafted.
  Note: a workflow_dispatch input is shown on the public run page.

## Drafts stay approvable indefinitely

- A draft is keyed by numeric article id. A newer cron/manual run never overwrites, expires or rejects the
  draft of a *different* article, and the Instagram container is created only at publish time (no 24 h expiry).
- Re-drafting the *same* article replaces its draft content; the existing approval link (same id) publishes the latest version.
- `/pages/latest-post` always points to the article of the **most recent successful IG publish** (by publish time):
  every successful publish repoints it, including an older article approved later. The pointer is kept in the ledger
  under `_bio_link`; it is only refused to a run whose publish time is older than the current holder's (two simultaneous
  approvals), never by article date.
- Every draft email ends with **Pending approvals**: all other drafts still in `draft` status, each with its Approve link.
- Image URLs are never stored; they are built from the current repo name when an email is sent or a post is published.

## Meta token

Secret `META_SYSTEM_USER_TOKEN` (system user, never expires). At the start of every draft and publish run
`core/meta.py` calls `debug_token`; an invalid token or a missing scope stops the run and the failure email names it.
Required for Instagram: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`.
`pages_manage_posts` is required only for runs that include Facebook (every draft run, and any approval with `fb`). *Actions → Social Token Check* runs the same check by hand.
Tokens are sent as `Authorization` headers and redacted from every log, email and issue comment.

## Which articles get drafted

- **Cron** (`social-draft.yml`): only articles *published after* `DRAFT_CUTOFF` (`core/config.py`, set to the
  merge time) with no ledger entry. Older articles are never drafted automatically.
- **On request**: *Actions → Social Draft → Run workflow* with `article_id` (numeric). Works for any age, but
  refuses held articles and anything already `published` / `publishing`.
- **Hold list** (`HOLD_ARTICLE_IDS` in `core/config.py`): articles blocked for both paths until their claims are
  fixed. Remove the entry to release one.
- `state/legacy-ig-drafted-articles.json` is read-only history of the old pipeline; nothing reads it.
- Legacy posts that were live on Instagram were seeded into the ledger as `published` (`seeded_from` says how)
  by `tools/ig_media_audit.py`, a read-only image-hash match of the account's media against `generated/`.

## Approval security

- Only issues with the `approval` label from an `OWNER` or `COLLABORATOR` author run anything.
  Add the account you are signed in as (e.g. `drostark`) as a collaborator.
- The title is passed via `env:` and must fully match
  `^(approve|reject):[0-9]+:(ig|fb|ig\+fb|pin|reddit)$` (`core/approval.py: ACTION_RE`).
  Anything else fails. It is never interpolated into a shell line.

## Manual run / failure test

*Actions → Social Publish → Run workflow*: `article` (numeric id), `channel`. Tick **test_bad_token** to run with an
invalid Meta token and confirm the failure email arrives (the ledger stays `draft`).

## Layout

```
core/       blog, copy, image, approval (email + buttons), ledger (state + git), http (error bodies, redaction)
channels/   instagram.py   (facebook / pinterest / reddit in later phases)
draft.py    publish.py
tests/      python -m unittest discover -s tests -t .
```

`ig_publish_test.py` is the standalone Graph API smoke test used for Meta App Review; not part of the pipeline.

## Setup

Secrets (repo → Settings → Secrets → Actions): `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_CLIENT_ID`,
`SHOPIFY_CLIENT_SECRET`, `SHOPIFY_BLOG_HANDLE`, `STOREFRONT_DOMAIN`, `ANTHROPIC_API_KEY`,
`META_SYSTEM_USER_TOKEN`, `IG_USER_ID`, `RESEND_API_KEY`, `RESEND_FROM_IG`, `RESEND_TO`, `BIO_LINK_PATH`.
(Names unchanged from the old pipeline.) The Shopify app needs `read_content` and
`write_online_store_navigation`. Instagram bio link: set once, by hand, to `https://www.maisondecocon.com{BIO_LINK_PATH}`.
Optional `brand-voice.md` in the repo root is added to the copy prompt.

The repo is public: drafts and the ledger are readable before approval, by design.
Reddit inputs (Search Console data, findquestions lists) must never be committed (`reddit/inbox/` is ignored).
