# Working on hunsu

This is the shared source-checkout guidance for Claude Code and Codex. `CLAUDE.md`
imports this file; keep maintenance instructions here. Installing the plugin does
not automatically load this file into a user's project.

- Read [README.md](README.md) for the layout, development, tests and versioning, and
  [plugin/README.md](plugin/README.md) for contracts, host adapters and limits.
  The installed workflow is [compose](plugin/skills/compose/SKILL.md); keep its procedure
  shared between hosts, with enforced sequencing and gates in `plugin/hunsu.py`. The skill
  also describes how the session relays decisions and invokes workers.
- `plugin/` is the installed payload and nothing else: runtime files go there; tests,
  records, plans and these instructions stay at the root. The payload has no implementation
  dependency on this checkout (it imports nothing outside `plugin/`), and nothing in it is a
  symlink. Both catalogs select `./plugin`. The root `hunsu.py`, `judge_worker.py` and
  `hostcall.py` are one thin compatibility wrapper text that runs or imports the payload's
  file of the same name; keep implementation out of them.
- Keep Claude commands as adapters to individual engine actions. Codex uses the
  same skill and Python engine; do not duplicate the workflow in host manifests.
- Preserve project mutation locks and atomic JSON replacement. Keep declarations
  in `hunsu.json`, machine-local overlays in `hunsu.local.json`, and resolved
  snapshots in `hunsu.lock.json`. Do not turn local machine paths into public configuration.
- Preserve existing project hooks when materializing Codex hooks, and retain the
  provenance needed to replace only generated groups. Do not describe unverified
  host payloads or hook behavior as tested; the manual records those limits.
- Plugins are standalone: do not import sibling plugins. `plugin/hostcall.py` is vendored;
  account for its shared copies when changing it.
- Maintain this checkout with the released, installed engines its lock names; run the
  working source only from the tests or against disposable targets.
- For behavior changes, run `python3 test_hunsu.py` and `python3 -m unittest test_hostcall_env.py`
  from this repository root. They use temporary fake hosts, not the user's live installation.
  For documentation changes, check links and command examples. Follow the README's versioning
  policy before publishing or installing changed source.
