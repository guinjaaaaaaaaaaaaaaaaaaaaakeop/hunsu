# Temporary scratch space on hunsu's check paths

Contract for run-20261030-081825-aae5. Written by the session (Claude Code, claude-opus-5-5) from the owner's bounded
defect statement; the owner did not read or sign this wording.

The root ran this source's `hunsu.py check` against six project environments from a disposable Codex worker in the
`read-only` sandbox. The receipt drift was gone, but every check died with an unhandled `FileNotFoundError` ("No usable
temporary directory found in [...]", listing this machine's temporary directories) from `tempfile.mkdtemp(prefix="hunsu-role-")`
in `role_prompts`: that sandbox gives no writable temporary directory. The same checks on Claude passed (errors 0,
warnings 0). `check` reads the project's data, but to judge it renders each declared role's worker prompt from a scratch
request file (`role_prompts`), runs a changed mode's hook in an empty scratch project (`mode_text`), and every command that
changes the project first takes a lock file in a temporary directory (the mutation lock). `check --remote` also clones into a
scratch directory (`outdated`). This is a temporary-file capability boundary, not a regression of the receipt or worker
identity fixes.

## Q-scratch-diagnostic

When no writable temporary directory can be had — `tempfile` finds none (`FileNotFoundError`, "No usable temporary
directory found…"), or creating the scratch directory or the lock file there fails (`PermissionError` or another `OSError`)
— `check` stops with a diagnostic and a nonzero exit, not a traceback. The diagnostic:

- contains the exact phrase `no writable temporary directory` and names the command (`check`, `compose`, …);
- says what needed the scratch space (rendering role prompts, a mode's text, the mutation lock, `outdated`'s clone);
- suggests a supported writable scratch context — `TMPDIR` pointing at a writable directory, or a sandbox that allows
  writing there (Codex `workspace-write`) — and never a sandbox, approval or hook-trust bypass;
- carries no absolute machine path: not the temporary directories Python lists, not the home directory, not the project's
  or a plugin's path.

## Q-no-false-pass

On that failure nothing is passed off as done. `check` prints no summary line (`errors N · warnings N · info N`), never says
the environment matches, never treats a role as absent or a judgment as fresh or stale because its text could not be
rendered; no project file is written (`hunsu.json`, `hunsu.lock.json`, `hunsu-judgments.json` keep their bytes). `check
--findings` (dwitbuk's reporter) prints no findings document — a reporter that failed is not a clean one — and exits nonzero
with the diagnostic on stderr. A failed attempt leaves no cached partial state: in the same process, once a temporary
directory is available again, `check` renders the roles and reports exactly as it would have.

## Q-entrypoints

The other user-facing paths stop the same way, each naming itself, before they write project state (a command that changes
the project takes its lock first, so it stops before reading too): `compose`, `lock`, `judge request`, `judge consume`, and
`check --remote`. The mutation lock is never skipped to get past the failure.
The SessionStart hook's line, which runs `check` in a subprocess, does not say "environment matches" or "environment differs"
when `check` stopped on missing scratch space: it says `check` could not complete, with the diagnostic's reason, and no
machine path. Small targeted error handling at the scratch sites (or one shared helper they call) — not a new runner, stdin
protocol or change to how roles are rendered.

## Q-success-unchanged

With a writable temporary directory nothing changes: each role's prompt renders the same text, modes are read as before,
existing tests stay as they are and pass, and `python3 test_hunsu.py` passes in full on Python 3.11 or newer. Three existing
hook tests (`test_check_says_when_a_pinned_plugin_is_behind_a_release_available_here`,
`test_policy_lines_materialize_every_resolution_shape_and_the_session_hook_carries_them`,
`test_session_start_hook_reports_both_states`) fail when a worker sandbox's `AGENT_WORKER=1` is inherited: run the suite with
`AGENT_WORKER` unset, as chongdae's own check does.

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

## Q-scratch-docs

`README.md` says, under `check` and in "Codex and the sandbox", that `check` reads the project's data but may write
temporary scratch files to render roles and modes (and that commands which change the project take their lock in a temporary
directory); that a completely read-only sandbox can lack that capability, and what the commands then do; and the supported
remedy (`TMPDIR` at a writable directory, or a sandbox that allows writing there). It does not call `check` purely read-only
and suggests no bypass.

## Out of scope

`hostcall.py` stays byte-identical (SHA256 `fa06077c00e1aa8514be54689567bad7abcb5c5f21794e1b5f551ebb1e07873b`). No version
bump; no change to sandbox, trust or permission settings, installed plugins or caches; no change to what is judged or how a
role's prompt is asked. The released engines that orchestrate this run are not modified.
