## Q-real-content

Every real content change still changes the fingerprint and still fails `check` with "content differs": a skill's
`SKILL.md`, Python code (an engine or hook `.py`), and each manifest (`plugin.json`, `.claude-plugin/plugin.json`,
`.codex-plugin/plugin.json`). The receipt being present on the copy does not hide such a change.

Decided after the quibble: one case per file — a `SKILL.md`, a `.py`, and each of the three manifests (a manifest edit
changes a field other than `version`, so the version comparison does not answer first). Each case, with the receipt
present on the changed copy, asserts both that the fingerprint changed and that `check` reports "content differs". These
hold before the build and must still hold after it.
