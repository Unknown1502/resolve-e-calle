# Pull request draft — `awesome-phone-call-agents`

Target repo: `https://github.com/CALLE-AI/awesome-phone-call-agents`
(submission repo -- **not** `call-e-integrations`, which is the setup
repo and is not touched by this PR).

## Before opening

- [ ] Fork/clone `awesome-phone-call-agents` fresh; do not push this
      whole `resolve-e-docs-pack` repo into it.
- [ ] Copy only `skills/exception-resolution-calls/` into the fork's
      `skills/` directory.
- [ ] Read `docs/git-naming-conventions.md` in the target repo and name
      the branch accordingly (not yet checked this session -- fetch and
      confirm before naming the branch).
- [ ] Run the target repo's own `python3 scripts/validate_repository.py`
      inside the fork. `scripts/check_skill.py` in *this* repo mirrors
      the rules as documented earlier in this project, and currently
      reports `OK`, but it is a local approximation, not the
      authoritative check -- the real script is the one that decides.
- [ ] Add one line to the target repo's skills README, in their format:

  ```markdown
  - [exception-resolution-calls](skills/exception-resolution-calls/) - Resolve a blocked business workflow by phone, turning one CALL-E call task into per-recipient structured evidence and a deterministic state change.
  ```

## PR title (draft)

```text
Add exception-resolution-calls skill: identity-gated, deterministically-escalated phone follow-up
```

## PR body (draft)

```markdown
## What this adds

A skill for resolving a business workflow that is blocked on a phone
call -- an overdue PO acknowledgement, an unconfirmed delivery window,
a job a technician hasn't accepted -- where the answer must
deterministically update system state, not just get logged.

## Why it's a distinct contribution

Two things this skill teaches that a first version usually misses,
found by running a real implementation against real CALL-E calls:

1. **An authorized destination number is not an authorized person.**
   The skill shows how to add a self-reported `spoke_with` field and
   gate any outcome that would *close* a workflow on it -- a clean
   "yes" from the wrong person still can't close anything.
2. **A single `needs_human` flag can't be acted on.** Differentiating
   *why* a human is needed (routine request, dispute, commercial ask,
   or "don't call me again") lets an operator triage without reading a
   transcript, and lets "don't call me again" actually suppress future
   calls instead of just being logged.

Also documents a confidence hazard found in production use: a call
that never connected returned `completion_confidence: 0.82, "high"` --
high confidence the task did *not* complete, not evidence it did.
Treating confidence as a dampener (may withhold, never grant) is
covered explicitly, with the fixture that surfaced the issue.

## Reference implementation

Resolve-E (supplier PO-acknowledgement follow-up): one CALL-E call task
resolves several purchase orders into distinct, individually-reasoned
outcomes per recipient. [LINK: add the public repo URL once the user
decides where/whether to publish it.]

## Checklist

- [x] Scoped contribution: a skill (`skills/exception-resolution-calls/`)
- [x] Directly supports AI-agent phone-call workflows
- [x] `SKILL.md` + `references/safety.md` + `references/examples.md` present
- [x] Setup, usage, side-effect, and cancellation notes documented
      (`references/safety.md`)
- [x] Fictional/masked phone numbers only (E.164 documentation ranges)
- [x] English throughout
- [ ] Followed `docs/git-naming-conventions.md` -- **fetch and confirm
      against the live target repo before opening the PR**; not
      re-verified this session
- [ ] `python3 scripts/validate_repository.py` run inside the actual
      target repo fork -- **not yet run**; this repo's local
      `scripts/check_skill.py` passes as an approximation only
```

## What is NOT done yet

- The PR has not been opened. No fork exists yet as far as this session
  can verify.
- The target repo's actual validator has not been run against this
  skill -- only a local approximation.
- `docs/git-naming-conventions.md` from the target repo has not been
  fetched this session to confirm branch naming.
