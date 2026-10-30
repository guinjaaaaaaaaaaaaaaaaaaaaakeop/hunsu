## Q-tests-A

The tests for A build a judgments round as the existing gate test does (a resolution, a changed finding, a
`resolution_verdict`) and fail now for the real defect — `reviewed` turned into a delegation — not on a missing name. Guard
tests for Q-reviewed-continuation may pass before the build. Each new test's name carries the section id it decides
(`test_Q_unread_stays_false_…`, `test_Q_reviewed_continuation_…`, `test_Q_unread_visible_…`).
