## Q-prior-adoption

Added later in this run, on the owner's request. The working tree carries edits made before this run started, by the
owner's earlier work and authorized by the owner for release: the outer self-host baseline `hunsu.patch` (sha256
`16246c308243…`, taken at `29b6e8e` before this run) with its metadata holding the untracked `AGENTS.md` and `CLAUDE.md`.
This run did not author them. They are adopted here after review, exactly as the baseline has them:

- `AGENTS.md` (new): the shared source-checkout guidance for Claude Code and Codex; `CLAUDE.md` (new): `@AGENTS.md`.
- `README.md`: the reading-order paragraph and the Python 3.11 line under Install; the Codex section (`lock`
  materializing `hooks/codex.json`, `/hunsu:*` as Claude adapters); the section "Shared instructions and host entry
  points"; the `role add` and `capability` wording (`SANDBOX_LACKS` a fixed routing assumption, `capability --host`
  writing `roles`, a lock before new providers are used); the manifest example and the `engines` bullet at `>=3.11`;
  "Codex and the sandbox"; the first Limits bullet; the Versioning sentence and the subsection "Release and adoption".
- `hunsu.py`: the `CAPABILITY_HELP` text only.
- `skills/compose/SKILL.md`: step 2's sentence on declaring `engines` (the `python >=3.11` example).
- `plugin.json`, `.claude-plugin/plugin.json`: `"python": ">=3.9"` → `">=3.11"` (`.codex-plugin/plugin.json` already
  declared `>=3.11` at `29b6e8e`).

Acceptance: what is committed for them is the baseline's text, nothing else riding along; every relative link and anchor
they add resolves; what they say matches the code (`SANDBOX_LACKS`, `capability --alternates --host` writing `roles`,
`lock` materializing `hooks/codex.json`, `AGENT_HOST=codex`, `tomllib` for 3.11+) and the manifests (`>=3.11` in all
three plugin manifests and in `hunsu.json` `engines`; the version fields "Release and adoption" names); `python3
test_hunsu.py` passes with `CAPABILITY_HELP` as adopted. `README.md` and `hunsu.py` also hold this run's committed receipt
hunks (the `lock` paragraph, `tree_fingerprint`): the adopted hunks are read beside them and against Q-receipt-ignored …
Q-suite and `test_hunsu.py`'s `test_Q_*` tests — an adopted hunk that contradicts the receipt rule, or that would change a
receipt hunk, is a finding. Not claimed: that a host integration (Codex hooks, a sandbox) was exercised live; that the
other five products' *released* manifests declare `>=3.11` — their source trees do, most of their releases do not yet.
