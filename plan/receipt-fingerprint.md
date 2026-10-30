# Codex installer receipt and the content fingerprint

Contract for run-20261030-065025-f619. Written by the session (Claude Code, claude-opus-5-5) from the owner's delegated
task statement; the owner did not read or sign this wording.

`tree_fingerprint(root)` in `hunsu.py` is a plugin's content identity: every file's path and bytes, minus what the
interpreter, the OS and the host leave behind. `lock` records it, `check` compares it ("content differs from what the lock
recorded for version …"), the linked-source comparison and `add --take-content` use it.

Codex's marketplace installer writes `.codex-marketplace-install.json` at the root of each plugin copy it installs (keys
seen on this machine: `source_type`, `source`, `ref_name`, `sparse_paths`, `revision`). Claude Code's copy of the same
release has no such file. The released engine (hunsu 1.15.1) therefore fingerprints two identical payloads differently:
two identical temporary trees gave `350b44597669` and `350b44597669`; with the receipt added to one, `350b44597669` and
`1293bf240a52`. On this machine `check` with `AGENT_HOST=codex` reports "content differs" for exactly the three Codex
copies that carry a receipt (hunsu, mangsang, jokbo), although each matches its release file for file.

## Q-receipt-ignored

The root receipt is the installer's record, not the plugin's content. For a plugin tree whose other files are identical,
`tree_fingerprint` returns the same value when `.codex-marketplace-install.json` at the tree's root is absent, present, or
present with different contents (another `revision`, another `source`, empty, not JSON).

Decided after the quibble: what is left out is the root entry of that name that the walk lists as a file — a regular file,
or a symlink to one (the installer writes a regular file). A root *directory* of that name is not a receipt: its files
still count (Q-exact-path). The test asserts one equal value across all six states: absent, present, another `revision`,
another `source`, empty, not JSON.

## Q-cross-host

The same payload installed by both hosts is one content. In a temporary fixture: a lock written for a plugin version from
a copy with no receipt (as Claude Code installs it), then `check` against a copy of the same version and the same files plus
a root receipt (as Codex installs it), reports no "content differs" error for that plugin. The comparison of a linked
source with its installed copy treats a receipt the same way, because both go through `tree_fingerprint`.

Decided after the quibble: both halves are observed through `check` on a fake host, not only by calling
`tree_fingerprint`: (a) `lock` from a copy without a receipt, add a root receipt to the installed copy (or check a
second copy that has one), `check` has no "content differs" error; (b) a linked source with no receipt and its installed
copy under the same version with a root receipt: `check` has no "the linked source (…) differs from the installed copy"
error. Both fail against the current `hunsu.py`.

## Q-real-content

Every real content change still changes the fingerprint and still fails `check` with "content differs": a skill's
`SKILL.md`, Python code (an engine or hook `.py`), and each manifest (`plugin.json`, `.claude-plugin/plugin.json`,
`.codex-plugin/plugin.json`). The receipt being present on the copy does not hide such a change.

Decided after the quibble: one case per file — a `SKILL.md`, a `.py`, and each of the three manifests (a manifest edit
changes a field other than `version`, so the version comparison does not answer first). Each case, with the receipt
present on the changed copy, asserts both that the fingerprint changed and that `check` reports "content differs". These
hold before the build and must still hold after it.

## Q-exact-path

Only the exact root-relative path `.codex-marketplace-install.json` is left out. Still counted as content:

- a file of the same name below the root (`skills/<x>/.codex-marketplace-install.json`, `sub/.codex-marketplace-install.json`);
- any other root `.codex-*` entry (`.codex-plugin/plugin.json`, a root file such as `.codex-hooks.json` or
  `.codex-marketplace-install.json.bak`);
- other dotfiles (`.gitignore`, `.agents/…`). Dotfiles are not ignored broadly.

The existing exclusions (`__pycache__/`, `.git/`, `.in_use` as file or folder, `*.pyc`, `.DS_Store`) and the CRLF→LF
normalization stay as they are.

Decided after the quibble: this whole section — the near-misses above, a root directory named
`.codex-marketplace-install.json` with a file in it, and the existing exclusions and CRLF normalization — is decided by
tests named for Q-exact-path (the brief's "existing exclusions of Q-no-migration" was wrong; this section is the
contract). Each near-miss asserts that the fingerprint changed. These hold before the build and must still hold after it.

## Q-no-migration

A tree with no root receipt fingerprints exactly as hunsu 1.15.1 fingerprints it, so a lock written from a Claude Code copy
(or any copy without a receipt) stays valid. A lock written before this fix from a copy that carried a receipt (a lock
written on Codex) recorded a hash that included it; after the fix `check` reports "content differs" for it, honestly. The
remedy is explicit: verify the installed payload against its release, then `hunsu lock` again (or `hunsu add <plugin>
--take-content "why"` when a release was rewritten under its version). The engine does not rewrite a lock's fingerprint by
itself, does not accept a mismatch as taken, does not delete or edit a receipt, and does not report an old-hash lock as
current.

Decided after the quibble:

- The reference values are literals pinned in the test, computed for a fixture tree whose bytes the test writes itself.
  Before this build `hunsu.py`'s `tree_fingerprint` is byte-identical to hunsu 1.15.1's (checked by the session against the
  locked 1.15.1 copy), so computing them with the current source gives 1.15.1's values. Two literals: the fixture with no
  receipt (must equal the function's value before and after the build), and the fixture with a root receipt (1.15.1's
  receipt-including hash, which stands for an old lock).
- The no-receipt literal test holds before the build and after it. The old-lock test — a lock whose `fingerprint` is the
  receipt-including literal, checked against the receipt-bearing copy — fails before the build (today the receipt-bearing
  copy matches it) and must pass after it, asserting: `check` reports "content differs" for that plugin; the lock file's
  bytes are unchanged; `hunsu.json` gained no `content-taken`; the receipt's bytes are unchanged.
- The remedy is documented in `README.md` (Q-docs), by a task of its own, not in `check`'s message: the message stays as it
  is (it already names `hunsu lock` and `--take-content`). `test_hunsu.py` does not assert README wording; the docs task's
  verifier and the review read it. The tests task names this in its non-claims.

## Q-docs

`README.md` says, where the lock's fingerprint is described, that the root `.codex-marketplace-install.json` is left out of
the content fingerprint and why, that the same name elsewhere and other `.codex-*` files still count, and the relock
consequence of Q-no-migration. The `tree_fingerprint` docstring names the receipt among what the host leaves behind.

## Q-suite

`python3 test_hunsu.py` passes in full on Python 3.11 or newer (here `python3` is 3.12.15), including the unchanged
`test_the_lock_names_content_so_an_unbumped_edit_is_caught_on_every_host`. Tests use temporary fake hosts only: no real
plugin cache, installation, `~/.claude`, `~/.codex` or other project is read for its content or written.

Decided after the quibble: the tests task has no check of its own (a tests task is decided by its new tests being red
before the build); the build's check is `python3 test_hunsu.py`, the whole suite. Every new test's name carries the section
id it decides (`test_Q_receipt_ignored_…`, `test_Q_cross_host_…`, `test_Q_real_content_…`, `test_Q_exact_path_…`,
`test_Q_no_migration_…`), so one can be selected and run alone: `python3 -c "import test_hunsu; test_hunsu.<name>()"`.
Must fail before the build: Q-receipt-ignored, Q-cross-host (both halves), and the Q-no-migration old-lock test. Must pass
before and after: Q-real-content, Q-exact-path, the Q-no-migration no-receipt literal.

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

## Q-python3

The adopted Install line asks that `python3` on the host's PATH select Python 3.11 or newer; every host-facing
instruction in these docs says `python3` to match. Step 1 of `skills/compose/SKILL.md` no longer falls back to a bare
`python`; `README.md`'s reporter argv example uses `"python3"`; its Self-check line runs `python3 test_hunsu.py`. Nothing
else changes: other documents are not swept for the word.

## Q-floor-wording

Found by the fresh eyes after Q-prior-adoption: the adopted `engines` bullet says the six active products "declare
`python >=3.11` in their root, Claude and Codex manifests", in a bullet about the floor `check` reads from each installed
plugin; most of those products' releases still declare `>=3.9`. The sentence says it of their source manifests, and that
an installed copy of an earlier release may still declare `>=3.9`, which is the floor `check` reads for it. Nothing else in
the bullet or the README changes.

## Out of scope

No version bump and no change to `plugin.json`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` or marketplace
files; no release, push or installed-plugin upgrade. No removal of real receipts. The working tree carries earlier
uncommitted edits that are not this run's (the `CAPABILITY_HELP` text in `hunsu.py`; `README.md` install, Codex, host
entry point, capability, limits and versioning text; the Python floor in both `plugin.json` files; `skills/compose/SKILL.md`;
`AGENTS.md`, `CLAUDE.md`): every hand keeps them exactly as they are and changes only what this contract needs.
This bounds the receipt tasks. Q-prior-adoption, added later, commits those earlier edits as they are, under their own
provenance; Q-python3 is the one change made to them, and neither bumps a version.
