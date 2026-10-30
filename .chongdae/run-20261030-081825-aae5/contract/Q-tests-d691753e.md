## Q-tests

The tests simulate the boundary only around the command under test, after the fixture is built: `tempfile.gettempdir`
raising `FileNotFoundError(errno.ENOENT, "No usable temporary directory found in [...]")` with machine-like paths in the
message, and `tempfile.mkdtemp` raising `PermissionError`. They observe what a person sees: the exit code, the output and
stderr (at least one runs the engine in a subprocess, e.g. with a `sitecustomize.py` on `PYTHONPATH` that patches
`tempfile`, and finds no `Traceback`), and the project files' bytes. They fail now for the real defect — the unhandled
exception, the traceback, a missing diagnostic — never because they import a name the fix has not added yet. A project
reaches `role_prompts` when its manifest declares roles a locked plugin provides and judgments exist (see the existing
`test_role_prompts_are_asked_until_answered_and_no_machine_path_reaches_the_judge` and
`test_the_judge_reads_a_roles_prompt_and_a_modes_injected_text_not_their_scripts`). Every new test's name carries the
section id it decides (`test_Q_scratch_diagnostic_…`, `test_Q_no_false_pass_…`, `test_Q_entrypoints_…`,
`test_Q_success_unchanged_…`).
