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
