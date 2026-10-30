## Q-cross-host

The same payload installed by both hosts is one content. In a temporary fixture: a lock written for a plugin version from
a copy with no receipt (as Claude Code installs it), then `check` against a copy of the same version and the same files plus
a root receipt (as Codex installs it), reports no "content differs" error for that plugin. The comparison of a linked
source with its installed copy treats a receipt the same way, because both go through `tree_fingerprint`.
