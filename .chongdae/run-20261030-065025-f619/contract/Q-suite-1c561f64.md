## Q-suite

`python3 test_hunsu.py` passes in full on Python 3.11 or newer (here `python3` is 3.12.15), including the unchanged
`test_the_lock_names_content_so_an_unbumped_edit_is_caught_on_every_host`. Tests use temporary fake hosts only: no real
plugin cache, installation, `~/.claude`, `~/.codex` or other project is read for its content or written.

Decided after the quibble: the tests task has no check of its own (a tests task is decided by its new tests being red
before the build); the build's check is `python3 test_hunsu.py`, the whole suite. Every new test's name carries the section
id it decides (`test_Q_receipt_ignored_…`, `test_Q_cross_host_…`, `test_Q_real_content_…`, `test_Q_exact_path_…`,
`test_Q_no_migration_…`), so one can be selected and run alone: `python3 -c "import test_hunsu; test_hunsu.<name>()"`.
Must fail before the build: Q-receipt-ignored, Q-cross-host (both halves), and the Q-no-migration old-lock test. Must pass
before and after: Q-real-content, Q-exact-path, the Q-no-migration no-receipt literal.
