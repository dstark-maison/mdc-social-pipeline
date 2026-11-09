# Backlog

## Open

- **Stories are untested against the live API.** `channels/story.py` is covered by unit tests with a mocked Graph
  API only. The first real story is 27 Nov. Time-box (owner, 2026-11-09): if Stories are not confirmed working by
  **20 Nov**, ship the two feed posts only and set `skip: true` on posts `9261127002`, `9261130003`, `9261208005`.
- Blog drafts get a pending-approvals list that now also shows campaign drafts (and vice versa). Intended.

## Done

- 2026-12-28 Campaign workflow `social-campaign.yml` removed (no cron left). The campaign code and manifest stay as a template.
- 2026-11-09 Campaign Black Week + Christmas 2026 (manifest, scheduled draft/publish, Story channel, bio-link schedule): see README "Campaign".
