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
