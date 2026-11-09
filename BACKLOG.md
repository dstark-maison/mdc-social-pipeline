# Backlog

## Open

- **28 Dec 2026 — merge the campaign cleanup PR** (`campaign-peak-2026-cleanup`): deletes
  `.github/workflows/social-campaign.yml` so no cron is left running. Until then the workflow is harmless:
  no post is due after 8 Dec, the bio link is reset on 26 Dec, and from 30 Dec `bio_decision` returns `none`
  (it does nothing). Afterwards the campaign code (`campaigns/`, `core/campaign.py`, `campaign_run.py`) can stay
  as a template or be removed in the same PR.
- **Stories are untested against the live API.** `channels/story.py` is covered by unit tests with a mocked Graph
  API only; owner approved one live test Story (2026-11-09, `action=teststory`, neutral image, deleted by hand
  afterwards). Record the result here. The first real story is 27 Nov. Time-box (owner, 2026-11-09): if Stories are not confirmed working by
  **20 Nov**, ship the two feed posts only and set `skip: true` on posts `9261127002`, `9261130003`, `9261208005`.
- Blog drafts get a pending-approvals list that now also shows campaign drafts (and vice versa). Intended.

## Done

- 2026-11-09 Campaign Black Week + Christmas 2026 (manifest, scheduled draft/publish, Story channel, bio-link schedule): see README "Campaign".
