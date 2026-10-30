# Unread proposals stay unread; worker-context test fixtures

Contract for run-20261030-084516-888e. Written by the session (Claude Code, claude-opus-5-5) from the owner's statement of
two bounded follow-ups; the owner did not read or sign this wording.

**A.** mangsang's project changed its `AGENTS.md` and re-judged eight stale situations with the installed hunsu 1.15.1. Two
situations whose resolutions were the judge's proposals applied unread (`"reviewed": false`) came back with grounded
still-fits verdicts, and `judge consume` replaced `false` with `{"delegated": "judge …"}`: the unreviewed debt vanished
though no person had ever read those proposals. mangsang put `false` back as manifest maintenance and kept the raw verdicts.
The cause is in `judge_consume`: on a grounded still-fits it assigns the delegation record whatever `reviewed` held. README
(Manifest) says `"reviewed": false` marks a proposal applied without a human reading it — allowed, never hidden.

**B.** Three hook tests (`test_check_says_when_a_pinned_plugin_is_behind_a_release_available_here`,
`test_policy_lines_materialize_every_resolution_shape_and_the_session_hook_carries_them`,
`test_session_start_hook_reports_both_states`) hand the hook `dict(os.environ, …)`, so a parent that is itself a worker
(`AGENT_WORKER=1`, as every hired member's sandbox is) makes the hook stay silent, as it should, and the tests fail; from a
normal parent the whole suite passes (78). Their neighbours already remove `AGENT_WORKER` for normal-session cases and set it
for suppression cases.

## Q-unread-stays-false

When `judge consume` re-judges a situation whose findings changed, and its resolution's `reviewed` is exactly `false`, a
grounded still-fits verdict (verbatim quote, no state named) leaves `reviewed: false` as it is: the resolution stays, no
delegation record is written, and consume's output says the judge found it still fits but no person has read the proposal,
so it stays unreviewed. The judge's raw answer stays where it was written (the judge round's response files); no new
manifest field is added for it, and no other key of the resolution changes.

## Q-reviewed-continuation

Everything else keeps today's behavior. A resolution previously reviewed — by a person (`{"by": …}`, `true`), written by hand
with no `reviewed` record, or an earlier judge delegation (`{"delegated": …}`) — gets the delegation record on a grounded
still-fits, as `test_the_reviewed_gate_is_delegated_to_the_judge_and_fails_closed` asserts. does-not-fit, cannot-tell, no
verdict, a quote not verbatim in the member texts, or a quote or reason naming one state marks `reviewed: false` whatever it
held. Situations whose findings did not change are not touched. No existing record is migrated, re-signed or rewritten.

## Q-unread-visible

A resolution kept `reviewed: false` this way is still said as unreviewed everywhere it is said today: `check --findings`
reports it (`unreviewed`), `policy` shows it as the judge's proposal applied unread, and the session's policy line carries
`[unreviewed — applied from the judge's proposal]`.

## Q-tests-A

The tests for A build a judgments round as the existing gate test does (a resolution, a changed finding, a
`resolution_verdict`) and fail now for the real defect — `reviewed` turned into a delegation — not on a missing name. Guard
tests for Q-reviewed-continuation may pass before the build. Each new test's name carries the section id it decides
(`test_Q_unread_stays_false_…`, `test_Q_reviewed_continuation_…`, `test_Q_unread_visible_…`).

## Q-docs-reviewed

`README.md` (Conflicts between skills) says what consume does to `reviewed` when a re-judged situation's findings change: a
grounded still-fits replaces it with a delegation record for a resolution already reviewed (by a person or an earlier
delegation), leaves an unread proposal's `reviewed: false` as it is, and anything else marks `reviewed: false`. It no longer
says every changed situation becomes `false`.

## Q-worker-fixtures

Test-fixture maintenance, not a new feature: the three tests above build the environment they mean — a normal session
(`AGENT_WORKER` and `HUNSU_SURVEY` removed from the env they give the hook) for their normal-session assertions, and
`AGENT_WORKER=1` set explicitly for any worker-suppression assertion. No hook, no production caller and no guard changes;
nothing clears a production environment. They pass under a normal parent and under an inherited `AGENT_WORKER=1`. There is
no red-before-build for this: the existing failures under a worker parent are its evidence.

## Q-final-verification

After A and B, once each: the full `python3 test_hunsu.py` (3.11+) under a normal parent, and under an inherited
`AGENT_WORKER=1`. Builds before that run the tests their change touches, not the full suite again.

## Out of scope

`hostcall.py` stays byte-identical (`fa06077c…`). No version bump, push, release, installed-plugin or cache change; the
installed 1.15.1 keeps its consume behavior until a release. No change to raw judge answers, to records earlier runs or
people made, or to what other `reviewed` states mean. No new feature.
