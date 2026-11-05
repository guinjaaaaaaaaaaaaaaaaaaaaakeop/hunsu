---
description: A role's capability alternates — `add NAME@CAP --from ROLE --host HOST` writes ROLE's worker on another host into hunsu.json; `list` shows them (read-only)
argument-hint: list | add <role>@<cap>[+<cap>] --from <role> --host claude|codex [--model M] [--effort E] [--force]
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/hunsu.py" role $ARGUMENTS --target .`

Show the output above to the user as is. After an `add`, the next step is `hunsu lock` (chongdae hires from the lock); run it only when the user asks.
