## Q-receipt-ignored

The root receipt is the installer's record, not the plugin's content. For a plugin tree whose other files are identical,
`tree_fingerprint` returns the same value when `.codex-marketplace-install.json` at the tree's root is absent, present, or
present with different contents (another `revision`, another `source`, empty, not JSON).
