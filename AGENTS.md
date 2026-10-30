# Working on hunsu

This is the shared source-checkout guidance for Claude Code and Codex. `CLAUDE.md`
imports this file; keep maintenance instructions here. Installing the plugin does
not automatically load this file into a user's project.

- Read [README.md](README.md) for contracts, host adapters, limits and versioning.
  The installed workflow is [compose](skills/compose/SKILL.md); keep its procedure
  shared between hosts, with enforced sequencing and gates in `hunsu.py`. The skill
  also describes how the session relays decisions and invokes workers.
- Keep Claude commands as adapters to individual engine actions. Codex uses the
  same skill and Python engine; do not duplicate the workflow in host manifests.
- Preserve project mutation locks and atomic JSON replacement. Keep declarations
  in `hunsu.json`, machine-local overlays in `hunsu.local.json`, and resolved
  snapshots in `hunsu.lock.json`. Do not turn local machine paths into public configuration.
- Preserve existing project hooks when materializing Codex hooks, and retain the
  provenance needed to replace only generated groups. Do not describe unverified
  host payloads or hook behavior as tested; the README records those limits.
- Plugins are standalone: do not import sibling plugins. `hostcall.py` is vendored;
  account for its shared copies when changing it.
- For behavior changes, run `python3 test_hunsu.py` from this repository root.
  It uses temporary fake hosts, not the user's live installation. For documentation
  changes, check links and command examples. Follow the README's versioning policy
  before publishing or installing changed source.
