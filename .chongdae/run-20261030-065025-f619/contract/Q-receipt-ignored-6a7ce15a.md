## Q-receipt-ignored

The root receipt is the installer's record, not the plugin's content. For a plugin tree whose other files are identical,
`tree_fingerprint` returns the same value when `.codex-marketplace-install.json` at the tree's root is absent, present, or
present with different contents (another `revision`, another `source`, empty, not JSON).

Decided after the quibble: what is left out is the root entry of that name that the walk lists as a file — a regular file,
or a symlink to one (the installer writes a regular file). A root *directory* of that name is not a receipt: its files
still count (Q-exact-path). The test asserts one equal value across all six states: absent, present, another `revision`,
another `source`, empty, not JSON.
