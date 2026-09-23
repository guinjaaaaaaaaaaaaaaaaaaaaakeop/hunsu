# hunsu

Locks a project's agent environment the way a package manager locks dependencies.
Which plugins, skills, hooks and runtimes a project uses is declared in `hunsu.json`, resolved
and pinned in `hunsu.lock.json`, and checked on every session start. Everyone on the project
develops in the same environment; what is not in the manifest is not part of the project.

## Install

```
claude plugin marketplace add guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu
claude plugin install hunsu@hunsu
```

## Codex

Add this repository as a Codex marketplace, then install hunsu and start a new session:

```
codex plugin marketplace add guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu
codex plugin add hunsu@hunsu
```

At session start, Codex asks you to review and trust hunsu's bundled hook, then runs `hooks/session_start.py` with `python`. The hook adds the same one-line lock status described below to the session context. Inspect or change hook trust with `/hooks`.

Codex discovers the existing `compose` skill as `hunsu:compose`. Invoke it with `$hunsu:compose`, or select it from `/skills`; you can also ask Codex to compose, check, or lock the project environment in plain language. The engine commands remain `survey`, `init`, `add`, `check`, `lock`, and `compose`.

Codex does not support plugin-defined custom slash commands, so the `/hunsu:*` commands below are available only in Claude Code. Codex also does not load plugins in its IDE extension.

## What happens

Open any project. hunsu's SessionStart hook puts one line into the session:

- no `hunsu.json` → "this project's environment is not locked" — the `compose` skill walks you through declaring it
- `hunsu.json` present → `check` runs; drift between this machine and the manifest is reported before any work starts

And on every skill call (PreToolUse on `Skill`): a skill that is not in `hunsu.lock.json`, or that `resolutions` denies or assigns to another plugin, is **refused** with the reason. An undeclared skill is an undeclared dependency — it works on your machine and breaks on everyone else's. No lock, no enforcement.

## Files in your project

| file | committed | written by |
|---|---|---|
| `hunsu.json` | yes | you (via `init`, `add`, and by hand for `engines`, `resolutions`, `local-hooks-ok`) |
| `hunsu.lock.json` | yes | `lock` — the resolved snapshot: versions, skills, hooks, engine versions found, warnings |
| `hunsu-judgments.json` | yes | the judge round's verdicts per situation, fingerprinted by the members' text — what `judge: judged` in the lock refers to; `hunsu-conflicts.md` is its rendering (`judge render`, not committed) |
| `hunsu.local.json` | no (`init` adds it to `.gitignore`) | `add`/`link` — plugins that come from a local checkout on *this* machine; by hand, `local-hooks-ok` for user-level hooks that are only yours |

## Commands

Six are also slash commands: `/hunsu:survey`, `/hunsu:init`, `/hunsu:add <plugin>`, `/hunsu:check`, `/hunsu:lock`, `/hunsu:compose`; `remove`, `judge`, `install`, `link`/`unlink` are the engine's (`python3 <plugin root>/hunsu.py …`). The `compose` skill is the same flow driven by the agent.

| command | does |
|---|---|
| `survey` | everything that runs for this project on this host: plugins (version, source), skills, hooks, SessionStart modes |
| `init` | empty manifest; `.gitignore` entry for the local file |
| `add <plugin>` | record version and source from the survey. Local marketplace → `unpublished` + link. `--take-content "why"` when a release was rewritten under its own version: the content is taken as that version's, the reason recorded in the manifest, and `check` accepts exactly that content |
| `check` | manifest vs this machine. `error`: missing/wrong-version plugin, engine below floor, runtime a hook needs is not on PATH, a skill name provided by two plugins with no resolution. `warn`: unpublished source, duplicate hook registration, user-level hook not in the manifest. `info`: links, extra plugins, machine-bound paths. Exit 1 on errors |
| `lock` | write `hunsu.lock.json`; refuses on errors, records warnings |
| `compose` | the whole flow: init → add → engines → check → lock, stopping at each human decision with exit 2 and a `decision:` line. Re-run after answering. `/hunsu:compose` runs it |
| `remove <plugin>` | drop a plugin and its link; resolutions that still name it are listed — the judged set changed, so judge again |
| `judge request` / `judge consume` | conflict judgment — see below |
| `install` | the second machine: for each plugin in the manifest that is missing here, `claude plugin marketplace add` + `claude plugin install <plugin>@<marketplace> --scope project`, then tells you to `check`. An `unpublished` plugin installs from its `hunsu link <plugin> <marketplace dir>` on this machine, or not at all. A plugin present at another version is reported as drift, not reinstalled. `--refresh <plugin>…` (or `all`) re-snapshots a *linked* plugin — Claude Code: uninstall + install at project scope; Codex: `plugin remove` + `plugin add` — so an edited or bumped source reaches the copy the host loads. (Claude Code loads a `directory` marketplace's plugins in place, so there the refresh only updates the host's record.) |
| `link` / `unlink` | this machine only |

## Conflicts between skills

Two skills conflict when following both in the same situation gives contradictory behavior or duplicated authority:
**overlap** (both claim the work — two plugins each shipping a `review` skill), **contradiction** (one instructs what the other forbids —
a standing mode's "never stall, pick a default" against a skill's human-confirmation gate), **premise** (one assumes what the other forbids).
The agent does not stop at a conflict; it improvises a compromise that differs per session. That is intent nobody defined, and the manifest is where intent lives.

hunsu does not judge meaning. It packs the question for a judge the host runs (a fresh, read-only session with a fixed answer schema) and validates the answer:

1. `judge request --out DIR` — stage 1: names + descriptions of the locked skills, the roles' worker prompts and the standing modes → the judge groups them by the **situation** they claim (`cluster-response.json`).
2. `judge request --out DIR --groups DIR/cluster-response.json` — stage 2: one packet per group with each member's full text → findings with **quotes from each member**, a reason, and a proposed resolution.
3. `judge_worker.py --request F --response F [--host claude|codex]` runs one packet.
4. `judge consume --dir DIR` — rejects findings without quotes or with unknown members, writes `hunsu-judgments.json` (each situation with a fingerprint of its members' text — a version bump that changes no text stales nothing) and `hunsu-conflicts.md` (what was found, the evidence, the proposals — for a human to read).
5. You write `resolutions[<situation>]`. `check` fails for a situation with findings and no resolution. `lock` carries the resolutions, and the SessionStart hook **materializes** them: one line per situation in the session's first context ("reviewing a change: use dwitbuk:review; never other:review", `[unreviewed]` when a proposal was applied unread). `deny` is also enforced by the sentinel at call time; `use`, `order`, `accept` act through these lines. Measured: the same request reached for a different skill with the line than without, 2/2 each way.
6. Later, a member's text changes (its SKILL.md, a worker's prompt, a mode's script or command): only the situations it is a member of go stale. A judgment is about text, so a version bump that changes no member's text stales nothing — a patch release costs no judge round. (Judgments made before this carry only a version fingerprint and stay valid by version until re-judged.) `judge request --out DIR --stale` writes packets for those alone; `consume` merges them over the old ones. A plugin that brings a skill no situation has seen needs a new cluster round (stage 1). If a re-judged situation's findings differ from what its resolution was written against, `consume` marks that resolution `reviewed: false` — it stays, and dwitbuk lists it until a human re-reads.

What the judge reads is what a session reads, never a script's name. A skill is its SKILL.md. A mode (a plugin's SessionStart hook) is the text it injects: hunsu runs the hook the way the host does — a `SessionStart` payload on stdin, `${CLAUDE_PLUGIN_ROOT}` resolved, `HUNSU_SURVEY=1` in the environment — and packs `additionalContext`; a hook that reports the project's state may answer its standing text under that variable. A role (a plugin's `roles` argv, the worker a runner starts) is the prompt that worker is sent: hunsu asks the argv itself with `--prompt-only` on a sample request, and packs the answer as member `plugin:role`. A worker inherits the project's modes, so a mode that says "ask the user" beside a role that says "ask no one" is a contradiction the judge can now quote from both sides.

A first judgment is rough by design; refine when a real conflict shows up. Modes are members of every situation, so with a mode every group is judged (cost: 1 + groups calls).

## Manifest

```json
{
  "hosts": ["claude-code"],
  "engines": {"claude-code": ">=2.1", "python": ">=3.9", "node": ">=20"},
  "plugins": {"hacheong": {"version": "1.1.0", "source": "github:guinjaaaaaaaaaaaaaaaaaaaaakeop/hacheong"}},
  "resolutions": {"review": "dwitbuk"},
  "local-hooks-ok": ["my-observer.py"]
}
```

- `engines` — floors the team requires. `lock` records what this machine actually had. A plugin may declare its own floor (`"engines": {"python": ">=3.9"}` in its plugin.json, npm's convention); `check` then requires the team's floor to cover it and this machine to meet it, so a plugin whose workers need a newer runtime fails as one line here, not as a traceback in a worker later. The five products declare `python >=3.9`. One exception above that floor: the Codex survey reads `~/.codex/config.toml` with `tomllib` (3.11+); on an older interpreter it refuses with that reason instead of a traceback.
- `roles` — the project's declaration of who does what: `{"implementer": "hacheong:build", "verifier": "dwitbuk:eyes", "planner": "session"}`. A provider is `session`, an argv (`["python3", "{plugin:NAME}/worker.py", "--request", "{request}", "--response", "{response}"]`), or `plugin:role` — the last only when that plugin's plugin.json declares the role's argv (`"roles": {"build": [...]}`, `{host}` filled with this host's name at lock). Or `native:plugin:role`: the same declared role, run by the host's own subagent at the session's hand (see chongdae's providers) — locked as `{"native": argv}`. A skill id is not a provider: a skill is text the session agent reads, a runner spawns argv. hunsu does not assign roles; `check` verifies each declaration is real, `lock` writes `roles` as argv (what a runner spawns) and `roles-declared` as written.
- `resolutions` — keyed by situation. Shorthand for a same-name overlap: `"retro": "dakdol"` or `"deny"`. Full form, for a judged situation:
  `{"use": "plugin:skill", "deny": ["plugin:skill"], "order": ["plugin:skill", "plugin:mode"], "accept": "why it is fine", "reviewed": false}`.
  `deny` and `use`-on-a-name reach the lock's `denied` list, which the PreToolUse hook enforces. `order` and `accept` are facts for the agent, not enforced. A member may also be a role's prompt (`hacheong:build`, judged as `plugin:role`): naming one is a fact for the agent too — the hook guards skill calls, not the workers a runner starts.
  `"reviewed": false` marks a proposal applied without a human reading it — allowed, never hidden.
- `judge: "skip"` — lock without a conflict judgment. Recorded in the lock as `judge: skipped`.
- `local-hooks-ok` — script names of user-level hooks that may run here without being part of the project (observers). A hook that is only yours — a terminal app's agent hooks, a personal logger — goes in `hunsu.local.json` under the same key instead: acknowledged on this machine, never committed.
- `settings` — the team's switches for its products, carried into the lock unread by hunsu (e.g. `{"dwitbuk": {"stop-eyes": true}}`).
- `reporters` — name → argv of the commands that report findings for the reviewer (`dwitbuk/findings@1`), e.g. `["python", "{plugin:chongdae}/chongdae.py", "report", "--since", "{since}"]`. Locked; dwitbuk reads them from the lock. hunsu's own is `check --findings` (drift, unread resolutions).

## Codex and the sandbox

A Codex session under `read-only` or `workspace-write` cannot start a nested `codex exec` (the workers: a judge, dakdol, the eyes) — it dies at "failed to initialize in-process app-server client: Operation not permitted". So a session that drives workers runs unsandboxed (`codex exec --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust`), and chongdae's write guard is the guard; or the workers run outside the session (a person, a runner, a native subagent — see chongdae's `native:` providers). `check` run from inside a sandboxed session says so (`session: sandboxed …`) when the lock's roles are commands. Codex snapshots a plugin at `plugin add`; `install --refresh` re-snapshots.

## Limits

- Hooks cannot be disabled per project on any host; user-level hooks are reported, not blocked. Project-level hooks are the project's and are managed here.
- The sentinel judges plugin skills only (`plugin:name`). A host built-in (`code-review`, `simplify`, `loop`) has no plugin prefix and passes — the manifest cannot describe it.
- Skill overlap is detected by name only. Whether two differently named skills do the same thing is a human judgment.
- The host CLI installs whatever version a marketplace currently has; `install` cannot pin (verified: `claude plugin install` takes no version). `check` reports the drift; a human either takes the new version into the manifest (`hunsu add <plugin>` again, then the stale situations are re-judged) or the marketplace owner pins.
- `install` has been exercised on a fresh host (`CLAUDE_CONFIG_DIR`).
- On Codex the PreToolUse hook is declared with the same `Skill` matcher; whether Codex reports skill invocations under that tool name is unverified.
- `compose` stops at every human decision (exit 2). It does not decide; it refuses to advance. Its gates: plugins → engines → check errors → conflict judgment (or `judge: skip`) → user-level hooks → lock.
- Both judge worker paths have run: Claude Code (14 calls on a 5-plugin project, 27 findings, 0 rejected) and Codex (8 + 2 calls on the same products, 7 situations, 1 finding).
- Hosts: Claude Code (`~/.claude`, or `CLAUDE_CONFIG_DIR`; `HUNSU_CLAUDE_DIR` for tests) and Codex (`~/.codex/config.toml` and its plugin cache; selected by `AGENT_HOST=codex`, `HUNSU_CODEX_DIR` for tests). On Codex, which no longer runs a plugin's own hooks, `lock` materializes the locked plugins' `hooks/codex.json` into the project's `.codex/hooks.json` (machine paths; gitignored; remade by every lock; run `codex` with hook trust or `exec --dangerously-bypass-hook-trust`). The survey reads Codex's own registry (`config.toml`: marketplaces, plugins, the user's hooks) and its plugin cache.

## Versioning

Semver, and a version names one content: every change to the source — code, skill or command text, hooks, this README — bumps
the version in all three manifests (`plugin.json`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`) and the
marketplace entries before it is used anywhere. Hosts copy a plugin at install and do not look again while the version stands,
so an unbumped edit is a copy nobody can tell from the old one. **patch**: behavior or wording, every interface unchanged.
**minor**: a new command, skill, field, hook or artifact key; what exists keeps working. **major**: an artifact type, lock or
record that other products read changes shape. hunsu's `check` fails a linked plugin whose source differs from the installed
copy under one version; `hunsu install --refresh <plugin>` recopies after the bump.

## Self-check

`python test_hunsu.py` — a fake host under a temp dir; every `check` error branch, `compose`'s gates, the sentinel's allow/deny, and both SessionStart states fire once.
