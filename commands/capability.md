---
description: The project's answer, once — do hired hands need a local port (loopback) or the network? `--session` (the session does such work) or `--alternates --host claude` (a `<role>@<cap>` alternate added for every role that lacks it)
argument-hint: loopback|network.. --session | --alternates [--host claude [--model M] [--effort E]]
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/hunsu.py" capability $ARGUMENTS --target .`

Show the output above to the user as is.
