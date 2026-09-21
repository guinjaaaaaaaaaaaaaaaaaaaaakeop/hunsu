---
description: Declare and lock this project's agent environment, stopping at each decision that is yours
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/hunsu.py" compose --target .`

If the output ends with a `decision:` line, that line is a question for the user — ask it as is, apply the answer (`hunsu add`, or an edit to hunsu.json), and run `/hunsu:compose` again. If it ends with `locked`, tell the user to commit hunsu.json, hunsu.lock.json and .gitignore. Never edit files under the user's home directory.
