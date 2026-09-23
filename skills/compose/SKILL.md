---
name: compose
description: Use when a project has no hunsu.json, when the SessionStart line says the environment differs from hunsu.json, or when the user wants to add, remove, or lock the plugins, hooks and engines a project uses. Declares and locks the project's agent environment so everyone develops in the same one; nothing outside the manifest runs.
---

The engine is `hunsu.py` at this plugin's root. The order of steps lives in the engine, not here.

1. Run `python3 <plugin root>/hunsu.py compose --target <project>` (`python` on hosts that have no `python3`).
2. Exit 2: the last line (`decision: ...`) is a question for the user. Ask it as is. Apply the answer — `hunsu add <plugin>`, an edit to `hunsu.json` (`engines`, `resolutions`, `local-hooks-ok` for a hook the whole team runs), `local-hooks-ok` in `hunsu.local.json` for a user-level hook that is only this person's (not committed), or the judge round (`judge request` → `judge_worker.py` per packet → `judge consume`; then show the user `hunsu-conflicts.md` and propose `resolutions`, marking any they did not read `"reviewed": false`) — and run step 1 again. When writing `engines`, declare what the *project* requires, not what your interpreter happens to be: `python >=3.9` on a 3.12 machine is honest if the code needs only 3.9 — a floor copied from your own interpreter fails `check` on every machine older than yours.
3. Exit 0: locked. Commit `hunsu.json`, `hunsu.lock.json`, `hunsu-judgments.json` (the judgments the lock's `judge: judged` refers to — without them a second machine stops at the judge gate again), `.gitignore`. `hunsu.local.json` is this machine's and stays out; `hunsu-conflicts.md` is a rendering (`judge render`).
4. Never edit files under the user's home directory (`~/.claude/settings.json` and the like). Report; the user edits.
5. Calls to skills outside the lock are refused by hunsu's own PreToolUse hook. If the user wants such a skill, that is a manifest change: `hunsu add`, then lock again.
