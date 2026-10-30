## Q-entrypoints

The other user-facing paths stop the same way, each naming itself, before they write project state (a command that changes
the project takes its lock first, so it stops before reading too): `compose`, `lock`, `judge request`, `judge consume`, and
`check --remote`. The mutation lock is never skipped to get past the failure.
The SessionStart hook's line, which runs `check` in a subprocess, does not say "environment matches" or "environment differs"
when `check` stopped on missing scratch space: it says `check` could not complete, with the diagnostic's reason, and no
machine path. Small targeted error handling at the scratch sites (or one shared helper they call) — not a new runner, stdin
protocol or change to how roles are rendered.
