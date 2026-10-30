## Q-no-migration

A tree with no root receipt fingerprints exactly as hunsu 1.15.1 fingerprints it, so a lock written from a Claude Code copy
(or any copy without a receipt) stays valid. A lock written before this fix from a copy that carried a receipt (a lock
written on Codex) recorded a hash that included it; after the fix `check` reports "content differs" for it, honestly. The
remedy is explicit: verify the installed payload against its release, then `hunsu lock` again (or `hunsu add <plugin>
--take-content "why"` when a release was rewritten under its version). The engine does not rewrite a lock's fingerprint by
itself, does not accept a mismatch as taken, does not delete or edit a receipt, and does not report an old-hash lock as
current.
