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
