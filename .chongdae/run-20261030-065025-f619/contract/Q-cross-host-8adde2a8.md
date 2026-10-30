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
