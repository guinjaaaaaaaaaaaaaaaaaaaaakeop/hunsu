## Q-real-content

Every real content change still changes the fingerprint and still fails `check` with "content differs": a skill's
`SKILL.md`, Python code (an engine or hook `.py`), and each manifest (`plugin.json`, `.claude-plugin/plugin.json`,
`.codex-plugin/plugin.json`). The receipt being present on the copy does not hide such a change.
