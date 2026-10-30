## Q-scratch-docs

`README.md` says, under `check` and in "Codex and the sandbox", that `check` reads the project's data but may write
temporary scratch files to render roles and modes (and that commands which change the project take their lock in a temporary
directory); that a completely read-only sandbox can lack that capability, and what the commands then do; and the supported
remedy (`TMPDIR` at a writable directory, or a sandbox that allows writing there). It does not call `check` purely read-only
and suggests no bypass.
