# hunsu

Locks a project's agent environment the way a package manager locks dependencies.
Which plugins, skills, hooks and runtimes a project uses is declared in `hunsu.json`, resolved
and pinned in `hunsu.lock.json`, and checked on every session start. Everyone on the project
develops in the same environment; what is not in the manifest is not part of the project.

This repository holds two things. The plugin a host installs is the [`plugin/`](plugin/) directory, and only that; its
usage manual — commands, manifest, hooks, conflicts between skills, limits — is [plugin/README.md](plugin/README.md),
which ships with it. Everything else here is the checkout hunsu is developed in: its tests, its contracts and records,
its maintenance instructions, and its own agent environment.

## Install (using hunsu)

Python 3.11 or newer is required on both hosts. Ensure `python3` on the host's PATH selects that interpreter.

```
claude plugin marketplace add guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu
claude plugin install hunsu@hunsu
```

```
codex plugin marketplace add guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu
codex plugin add hunsu@hunsu
```

Both catalogs at this repository's root select the payload: `.claude-plugin/marketplace.json` names `"source":
"./plugin"`, `.agents/plugins/marketplace.json` names `{"source": "local", "path": "./plugin"}`. A host may clone or
snapshot this whole repository to read its catalog (Claude Code keeps a clone of a git marketplace, Codex a snapshot),
but the plugin it installs and activates is `./plugin`: the engine, the `compose` skill, the Claude Code commands, the
hooks, the manifests, the usage README and the LICENSE. The tests, records, wrappers and instructions at this root are
not part of the installed plugin. Start a new session after installing; then see [plugin/README.md](plugin/README.md).

## Develop (working on hunsu)

Clone the whole repository. [AGENTS.md](AGENTS.md) is the maintenance guidance for Claude Code and Codex alike
([CLAUDE.md](CLAUDE.md) imports it); installing the plugin never loads it into a user's project.

This checkout is itself a hunsu project: `hunsu.json` declares the plugins, roles and resolutions its sessions work with,
and `hunsu.lock.json` pins the released versions. Install hunsu as above, then the rest the lock pins
(`python3 <installed hunsu root>/hunsu.py install --target .`, then `check`), start a session, and work with the engines
the lock names — the SessionStart line prints their roots. The working source under `plugin/` is the candidate, never this checkout's tool: run it from the
tests or against a disposable target (a temporary project), and adopt a change here only after it is released and
installed.

### Layout

| path | what | installed |
|---|---|---|
| `plugin/hunsu.py` | the engine and its CLI | yes |
| `plugin/judge_worker.py`, `plugin/hostcall.py` | the judge worker and the host call it runs through (vendored: the same `hostcall.py` in mangsang, dwitbuk and hacheong) | yes |
| `plugin/skills/compose/SKILL.md` | the shared workflow for both hosts | yes |
| `plugin/commands/*.md` | Claude Code slash commands, one engine action each | yes |
| `plugin/hooks/` | `hooks.json` (Claude Code), `codex.json` (Codex, materialized by `lock`), the four hook scripts | yes |
| `plugin/plugin.json`, `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json` | the manifests | yes |
| `plugin/README.md`, `plugin/LICENSE` | the usage manual and the license | yes |
| `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` | the catalogs, selecting `./plugin` | no |
| `hunsu.py`, `judge_worker.py`, `hostcall.py` | source-checkout compatibility wrappers: run or import the payload's file of the same name | no |
| `AGENTS.md`, `CLAUDE.md`, `README.md`, `LICENSE` | this checkout's guidance and documents | no |
| `hunsu.json`, `hunsu.lock.json`, `hunsu-judgments.json` | this checkout's own agent environment | no |
| `test_hunsu.py`, `test_hostcall_env.py` | the tests | no |
| `plan/`, `mangsang/`, `reviews/`, `.chongdae/` | contracts, the concept net, review and run records | no |

The payload has no implementation dependency on this checkout: it imports nothing outside `plugin/` and needs none of
the root's files to run — it reads a user's project records and the host's settings as data, as any installed plugin
does. Nothing in it is a symlink. A runtime file goes into `plugin/`; a test, record or instruction file stays at the
root. The three root wrappers are one thin text that keeps this checkout's earlier entry points working: each puts
`plugin/` first on `sys.path` and runs the payload's file of its name as a command, or, imported, makes that file the
module itself — so `import hunsu` at the root is the module the payload's hooks import, and a monkeypatch on it reaches
the runtime. They hold no implementation; a change to hunsu's behavior is a change under `plugin/`. hunsu itself resolves a link or a `directory`
marketplace at a repository root to its `plugin/` and never to the checkout around it (see `link` in the
[manual](plugin/README.md#link--unlink)); plugins in the older root layout keep resolving.

### Entry points from the source tree

From the repository root, against a disposable target (the root wrappers take the same commands: `python3 hunsu.py …`,
`python3 judge_worker.py …`):

```
python3 plugin/hunsu.py --help
python3 plugin/hunsu.py check --target /tmp/scratch-project
AGENT_HOST=codex python3 plugin/hunsu.py survey --target /tmp/scratch-project
echo '{"cwd": "/tmp/scratch-project"}' | python3 plugin/hooks/session_start.py
python3 plugin/judge_worker.py --request REQUEST.json --response RESPONSE.json --host claude
```

Replace the placeholders with real paths; `HUNSU_CLAUDE_DIR` and `HUNSU_CODEX_DIR` point the engine at a fake host
instead of `~/.claude` and `~/.codex`, as the tests do.

### Tests

```
python3 test_hunsu.py
python3 -m unittest test_hostcall_env.py
```

Both put `plugin/` on `sys.path` explicitly and fail if `hunsu` or `hostcall` came from anywhere else. `test_hunsu.py`
builds fake hosts under temporary directories — every `check` error branch, `compose`'s gates, the sentinel's allow/deny,
both SessionStart states and the stale-session line fire once; `outdated` runs against local bare repositories standing
for the publishers, with no network. It also checks the payload itself: `plugin/` holds exactly the runtime files, the
five version values agree and both catalogs select `./plugin`; and a copy of `plugin/` alone, in a temporary directory
outside the repository, runs `--help`, `init`, `add`, `check`, `lock`, its hooks and the judge worker without reading a
file of this checkout. Fresh interpreters, without the suite's `sys.path` set-up, check the root wrappers: the same output
and exit code as the payload's files, the payload's modules on import, one module object with the hooks' import, and
monkeypatches on a root import reaching the runtime. No test reads your live `~/.claude`, `~/.codex` or plugin cache. In the umbrella checkout,
`tools/same-file.py` says when the vendored `hostcall.py` copies drift.

## Versioning and release

Semver; a version names one content, and the content is `plugin/` (see the manual's
[versioning](plugin/README.md#versioning) for patch, minor and major). Every change under `plugin/` — code, skill or
command text, hooks, the usage README — bumps the version before it is used anywhere, equal in five places:
`plugin/plugin.json`, `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`, and
`.claude-plugin/marketplace.json` (`metadata.version` and the hunsu entry's `version`). `.agents/plugins/marketplace.json`
has no version field; verify its name and `source.path`, and do not invent one. The version names the payload's content,
so the root's development records, tests, documents and wrappers change no version. A catalog change that affects what a
host installs or how it is delivered — the entry's source or name, anything a host reads to install — goes through the
same version and release review as a change under `plugin/`.

Before publication, run the tests for behavior changes; for documentation-only changes, verify links, command examples,
imports and manifest consistency, plus `git diff --check`. Publish the checked commit using this repository's existing
release convention. Publication does not update consumer installations: update the plugin through the host's supported
workflow, reconcile the consuming project's declared environment and lock if managed, and start a new host session to
apply the installation. Report publication, project adoption and live-host verification separately. Refreshing a
locally linked plugin is not an update procedure for every remote installation.
