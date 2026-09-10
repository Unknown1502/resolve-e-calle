# Submission packet — index

Everything here is **prepared, not published**. No PR has been opened,
no video uploaded, no Devpost form submitted. Each file below states
exactly what remains for the user to do.

| File | What it is |
|---|---|
| `devpost-copy.md` | Draft text for the Devpost submission form |
| `pr-checklist.md` | Draft PR title/body/checklist for `awesome-phone-call-agents` |
| `calle-feedback.md` | Sanitized feedback for the CALL-E feedback survey (eligible for the "Most Valuable Feedback" track) |
| `../docs/verification-report.md` | What was checked this session, and how |
| `../docs/decision-log.md` | Why each documented conflict (A-F) was resolved the way it was |
| `../docs/demo.md` | 3-minute video script and shot list, matching the actual current build |

## Before recording or submitting

1. Decide on the stale running deployment (`docker compose ps` shows
   containers ~18-20h old, predating this session's code, plus a public
   tunnel). See `docs/verification-report.md` → "Deployment status".
2. Decide whether to place one more approved, live smoke test call to
   get a fresh answered-call transcript for the video's **[LIVE INSERT]**
   marker, or record entirely on the simulator (which is itself an
   honest, clearly-labelled demonstration -- the UI states "Simulated
   calls" plainly).
3. Set `BUYER_COMPANY` in `.env` to your organisation's real name before
   any live call -- the agent will refuse to dial without it.
4. `git init` has been run in an earlier session but **nothing has ever
   been committed**. Commit before opening a PR.
