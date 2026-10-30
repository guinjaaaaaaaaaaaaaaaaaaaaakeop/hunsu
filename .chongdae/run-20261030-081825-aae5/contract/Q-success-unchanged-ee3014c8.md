## Q-success-unchanged

With a writable temporary directory nothing changes: each role's prompt renders the same text, modes are read as before,
existing tests stay as they are and pass, and `python3 test_hunsu.py` passes in full on Python 3.11 or newer. Three existing
hook tests (`test_check_says_when_a_pinned_plugin_is_behind_a_release_available_here`,
`test_policy_lines_materialize_every_resolution_shape_and_the_session_hook_carries_them`,
`test_session_start_hook_reports_both_states`) fail when a worker sandbox's `AGENT_WORKER=1` is inherited: run the suite with
`AGENT_WORKER` unset, as chongdae's own check does.
