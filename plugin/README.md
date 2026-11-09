# hunsu

Locks a project's agent environment the way a package manager locks dependencies.
Which plugins, skills, hooks and runtimes a project uses is declared in `hunsu.json`, resolved
and pinned in `hunsu.lock.json`, and checked on every session start. Everyone on the project
develops in the same environment; what is not in the manifest is not part of the project.

Commands that change a project serialize their reads and writes across local sessions. JSON files are replaced only
after the complete document has been written, so an interrupted write leaves the previous document readable. This
protects each file; host installs and changes spanning multiple files are not an all-or-nothing transaction.

This is the usage manual that ships with the plugin. Start with [installation](#install) and
[host entry points](#host-entry-points). See [commands](#commands) for the CLI, [manifest](#manifest) for configuration,
[limits](#limits) for integration caveats, and [versioning](#versioning) for what a version means.

## Install

Python 3.11 or newer is required on both hosts. Ensure `python3` on the host's PATH selects that interpreter.

```
claude plugin marketplace add guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu
claude plugin install hunsu@hunsu
```

What a host installs is this directory alone — the engine (`hunsu.py`), the judge worker (`judge_worker.py` and its
`hostcall.py`), the `compose` skill, the Claude Code commands, the hooks, the three manifests, this README and the
LICENSE. The marketplace's catalogs select it (`./plugin` in the source repository). A host may clone or snapshot the
whole repository to read its catalog, but the repository's tests, development records and maintenance instructions are
not part of the installed plugin.

## Codex

Add the hunsu repository as a Codex marketplace, then install hunsu and start a new session:

```
codex plugin marketplace add guinjaaaaaaaaaaaaaaaaaaaaakeop/hunsu
codex plugin add hunsu@hunsu
```

For this adapter, `lock` on Codex and `upgrade` on either host materialize the plugins' `hooks/codex.json` into the
project's `.codex/hooks.json`. Review and trust those project hooks in Codex. The declared SessionStart hook runs
`hooks/session_start.py` with `python3` to report lock status; installation alone does not materialize it. The file is
this machine's and not committed, so a new clone or worktree has none: `check` says the installed hooks are not active
there, and `hunsu upgrade --hooks-only` writes them from the installed copies, keeping the project's own groups.

The generated groups name no versioned copy. Codex keeps one copy of a plugin in its cache and replaces it when it takes
a release, by itself, so a group naming `~/.codex/plugins/cache/<market>/<name>/<version>/…` fails on every event after
the next release (on 11-06, a minute after a push). A plugin hook whose command uses `${PLUGIN_ROOT}` and is one simple
command runs instead through the project's `.codex/hunsu-dispatch.py`, which `lock` and `upgrade` write beside the hooks
file: at each hook it finds the copy Codex has installed for that plugin from the declared marketplace and runs the same
script from it, with the hook's stdin, output and exit code. When that copy is not the version `hunsu.lock.json` pins,
it says so on stderr and runs it; when the plugin is not installed for Codex or its copy lacks the script, it stops the
hook with exit 2 — a guard is never skipped. Groups generated before 2.4.0 still name versioned copies (`check` warns):
`hunsu upgrade --hooks-only` on either host, or `hunsu lock` on Codex, rewrites them and keeps the project's own groups.
The host integration limits below distinguish declared hooks from verified behavior.

Codex discovers the existing `compose` skill as `hunsu:compose`. Invoke it with `$hunsu:compose`, or select it from
`/skills`; you can also ask Codex to compose, check, or lock the project environment in plain language. The engine
commands remain `survey`, `init`, `add`, `upgrade`, `check`, `lock`, and `compose`.

The `/hunsu:*` command files below are this plugin's Claude Code adapters. On Codex, use the shared
skill or the Python engine.

## Host entry points

Both hosts share [skills/compose/SKILL.md](skills/compose/SKILL.md) and `hunsu.py`.
The `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` manifests expose host-specific packaging;
`commands/` contains Claude entry points for individual engine actions, not a second compose workflow.
Either host can call `python3 <plugin-root>/hunsu.py <command> --target <project>` directly;
replace the angle-bracket placeholders with actual values and quote paths containing spaces.
For Codex, set `AGENT_HOST=codex` when calling the engine from a shell so it selects the Codex registry.
The installation instructions above and the limits below describe the host differences.

## What happens

Open any project. hunsu's SessionStart hook puts one line into the session:

- no `hunsu.json` → "this project's environment is not locked" — the `compose` skill walks you through declaring it
- `hunsu.json` present → `check` runs; drift between this machine and the manifest is reported before any work starts
- `hunsu.lock.json` present → also where the lock's plugins are on this machine, one root each — the place to call an
  engine from by hand
- a worker session (`AGENT_WORKER=1`: a judge, a builder, the eyes) → nothing; the environment is the hiring session's

It fires at startup, on resume, after `/clear` and after a compaction (the summary keeps none of it).

And at every prompt (UserPromptSubmit), when this session runs plugins other than the ones the lock pins: a host loads
a session's plugins once, at start — an update mid-session says "restart to apply", and `/clear` does not reload them —
while `check` compares the lock with what is *installed*, not with what this session loaded. hunsu's own hook knows the
version it runs from (its plugin root); when that differs from the lock's hunsu, or from what is installed for this
project, one line goes into the prompt's context, and into the SessionStart line too:

    hunsu: this session runs hunsu 1.9.0 while hunsu.lock.json pins 1.10.0 — the plugins updated since this session started are not loaded: restart the session (a /clear does not reload plugins)

Nothing when they agree, nothing in a worker session or under hunsu's own probe (`HUNSU_SURVEY`), nothing while hunsu is
in development here (`hunsu dev`). No check runs per prompt: the lock, the host's install record, a plugin.json. It is
said from hunsu's version alone — the one plugin whose running copy hunsu can see — and a session behind on hunsu was
started before the update, so the rest it runs are as old.

And on every skill call (PreToolUse on `Skill`): a skill that is not in `hunsu.lock.json`, or that `resolutions`
denies or assigns to another plugin, is **refused** with the reason. An undeclared skill is an undeclared dependency —
it works on your machine and breaks on everyone else's. No lock, no enforcement.

And on every shell command (PreToolUse on `Bash`): a command that runs a locked plugin's engine from another copy — another
version in the plugin cache, another marketplace's copy, or a glob in the version's place (`$C/mangsang/*/*/mangsang.py`,
`ls -d …/dwitbuk/* | tail -1`) — is **refused**, naming the root the lock pins. The plugin cache keeps every version this
machine installed and a glob picks by name order (1.9.1 after 1.12.0), so a session calling an engine by hand ran old
copies for days while its hooks ran the locked ones. Only what a command runs is read — the script an interpreter is
given, an engine file run as the command, a script run after `cd` into a copy, `$D/…` after `D=` took a copy — so
listing, reading, comparing old copies, or passing one to the project's own script, passes. When what is installed is
not the version the lock pins, no copy is named or refused (`check` reports it). A guard that cannot read its input
allows. No lock, no enforcement.

## Files in your project

| file | committed | written by |
|---|---|---|
| `hunsu.json` | yes | you (via `init`, `add`, and by hand for `engines`, `resolutions`, `local-hooks-ok`) |
| `hunsu.lock.json` | yes | `lock` — the resolved snapshot: versions, skills, hooks, engine versions found, warnings |
| `hunsu-judgments.json` | yes | the judge round's verdicts per situation, fingerprinted by the members' text — the file the judge gate reads (`compose` stops without it; the lock's `judge` line only records the outcome); `hunsu-conflicts.md` is its rendering (`judge render`, not committed) |
| `.claude/settings.local.json` | no (`init` and `dev` add it to `.gitignore`; `check` fails if it is committed) | `dev` — the working sources this machine runs instead of the manifest's copies |
| `.codex/hooks.json` | no (`lock` and `upgrade` add it to `.gitignore`) | `lock` on Codex and `upgrade` on either host — the hook groups generated from the copies installed for Codex (this machine's paths), beside the project's own groups, which they keep |
| `.codex/hunsu-dispatch.py` | no (`lock` and `upgrade` add it to `.gitignore`) | `lock` on Codex and `upgrade` on either host, whenever a generated group runs through it — a byte copy of this payload's `hooks/dispatch.py`, the version-independent entry the generated groups call (standalone: it imports nothing of hunsu, so replacing any copy in the cache leaves it working), written before the groups |
| `hunsu.local.json` | no (`init` adds it to `.gitignore`) | `add`/`link` — development sources or installed host copies on *this* machine; `upgrade` moves installed-copy links when it takes a newer release; by hand, `local-hooks-ok` for user-level hooks that are only yours, and `hosts` when this machine runs one host |

## Commands

Eleven are also slash commands: `/hunsu:survey`, `/hunsu:init`, `/hunsu:add <plugin>`, `/hunsu:upgrade [plugin…] [--hooks-only]`, `/hunsu:check`, `/hunsu:outdated`,
`/hunsu:policy`, `/hunsu:role`, `/hunsu:capability`, `/hunsu:lock`, `/hunsu:compose`; `remove`, `judge`, `install`, `link`/`unlink` are the engine's (`python3 <plugin root>/hunsu.py …`).
The `compose` skill is the same flow driven by the agent.

### `survey`

everything that runs for this project on this host: plugins (version, source), skills, hooks, SessionStart modes

### `init`

empty manifest; `.gitignore` entry for the local file

### `add <plugin>`

record version and source from the survey. Local marketplace → `unpublished` + link. `--take-content "why"` when a
release was rewritten under its own version: the content is taken as that version's, the reason recorded in the
manifest, and `check` accepts exactly that content

### `upgrade`

`hunsu upgrade [PLUGIN ...] [--hooks-only] [--target DIR]` takes the releases installed for this project on the
host it runs on into hunsu.json. With names, only those plugins; without names, all declared plugins. It takes only
a release newer than the pin, recording its version, shareable source and marketplace as `add` does. It never takes
a marketplace's uninstalled version, an older version, a development-source link or development plugin, a local source, or a copy
this host has not installed. It says which it leaves; non-release versions are left for `add` to decide.
`--hooks-only` takes no pins.

A link to an installed host copy is compared by version like an unlinked plugin, including "at the pin". When taking
a newer release, `upgrade` moves the link to that version's copy on the same host as the link. If that host lacks the
copy, it leaves the pin and link and asks you to install that version there or `hunsu unlink PLUGIN`; refreshing the
old copy is not an update. A development-source link is left as before.

For the hosts this project runs here, it rewrites the generated Codex groups from the copies installed for Codex,
from either host, keeping the project's own groups, their order and description. It says when that installed
copy differs from the pin, and names each manifest plugin not installed for Codex (no groups for it).
When no copy installed for Codex declares a hook and the project has no `.codex/hooks.json`, nothing is written for
Codex: no empty file and no `.gitignore` line. An existing file is still regenerated, even to no generated group.
Claude Code runs hooks from the installed copy's hooks/hooks.json; nothing is generated for it. When Codex is not a
host here, nothing is written for Codex. When a generated group runs through the dispatcher, it writes
`.codex/hunsu-dispatch.py` before `.codex/hooks.json`. This is also the migration of groups generated before 2.4.0:
their versioned paths become dispatched commands, the project's own groups, their order and description stay, and a
second `upgrade --hooks-only` changes no byte of either file.

An unknown plugin name refuses. Before writing either pins or hooks it also refuses an unreadable project hooks
file, a Codex registry that cannot be read when Codex is in use, or a generated group naming a file absent on this
machine — reinstall that copy. It installs nothing, runs no host command, and never changes host registries,
settings or caches, hunsu.lock.json, hunsu-judgments.json, or another project's files.

It lists what remains, in order: stale situations by label and their judge request/consume commands, unclustered
skills needing a cluster round, a session restart when pins changed, and `hunsu lock` with the records to commit
inside a run. With `judge: "skip"` it lists no judge step. (11-06: six projects pinned releases older than the ones
installed while the Codex cache held only the newest copies, so every generated group named a copy that was gone and
every Claude Code `lock` was refused; taking the releases was an `add` per plugin, a Codex `lock` and judge rounds in
each project, by hand.)

### `check`

manifest vs this machine. `error`: missing/wrong-version plugin, engine below floor, runtime a hook needs is not on
PATH, a skill name provided by two plugins with no resolution *when the project locks without a judge* (`"judge":
"skip"`) — otherwise a shared name is the judge's question: calling is never ambiguous (`plugin:skill`), and a name is a
weak proxy for a shared situation (`design:review` and `code:review` share a name and nothing else); a fresh judgment
that found no conflict between them needs no resolution, and one that did asks for its situation's. `warn`: unpublished source, duplicate hook
registration, user-level hook not in the manifest (one line per script however many events it is on, with the file it
runs on this machine), a user skill (`~/.claude/skills/<name>`) not in `local-skills-ok`, a plugin enabled here but not in
the manifest whose hooks run for this project (`unmanaged plugin`; its skills are refused, its hooks are not), worker
roles (an argv with `{request}`) on a host whose sandbox lacks a capability nobody decided — neither in `capabilities` nor
covered by a `<role>@<cap>` alternate — as one line naming the roles and both answers as commands (read from `hunsu.json`
alone, so it shows even while plugins are missing; a warning, so `lock` is not blocked; the SessionStart line counts it), `hunsu.local.json` not ignored by git (an `error` when it is committed). `info`: a pinned plugin behind a release available here (`plugin X is behind: pinned 1.12.2; the
marketplace M lists 1.13.0 (local copy)`, or a linked checkout's version — no network: a marketplace copy is as fresh as
its last `claude plugin marketplace update`; the session's first line counts them), links, extra
plugins, machine-bound paths, roles declared with chongdae locked and no `reviewer` (every run then ends with its
record unread — chongdae records it as each run's non-claim; here it is said once, where it is decided). Exit 1 on errors. `--remote` runs `outdated` after it.

A link to an installed host copy keeps the ordinary manifest/host drift error and update steps through `hunsu upgrade`.
When its version differs from the copy installed for this project, a warning names both and gives `hunsu link X PATH`
to move it (or `hunsu unlink X`); if the link's host lacks that version, it asks you to install it there first. No content
comparison is made across versions. At the same version an info line says "an installed copy, not a development source",
and content is compared with the copy this host loads, ignoring the install receipt as below. A difference is an error
naming both copies: compare each with the release and reinstall the one that differs; `hunsu unlink X` takes the link
out of the comparison. It never advises a refresh or a version bump for this difference. Such a link offers no
release of its own; marketplace offers use the ordinary update steps. Development-source links keep their source
comparison and `install --refresh` advice.

**Hosts and the roles' workers.** By default this machine runs every host `engines` names (`claude-code`, `codex`): the
shared mixed configuration, where a session on one host may hire workers on the other. A machine that runs one host says
so in `hunsu.local.json` — `"hosts": ["codex"]` (never committed) — and the other host is then not probed at all: neither
`check` nor `lock` runs its `--version`, `check` says so in one `info` line, and the lock's `engines` keeps its key as
`null` (not probed, not missing). A host a plain role's worker runs on, or that a plugin's own `engines` names, is probed
whatever the profile says. A profile that does not read — not a list, an empty list, or an entry
that is not `claude-code` or `codex` (a misspelling, a nested list, an object) — is an `error` and `lock` refuses; until it
reads, every host `engines` names is checked, so a broken profile never skips a probe. The overlay never hides a host the
project needs. A role's worker host is read from the argv a runner
would spawn: an argv as written, `plugin:role` resolved first to what the plugin declares (`{host}` is this host; a fixed
`--host` stays); `session` and `native:` have none. A **plain role** is hired for every task of its kind, so its host is
required: left out by `hosts`, or its executable not found here, is an `error` naming the roles, and that host's floor is
checked whatever `hosts` says; a host the plain roles run on that `engines` does not name asks for a floor (`warn`). A
`<role>@<cap>` **alternate** is hired only for a task that requires those capabilities: its host left out or not found is a
`warn`, never an error, and does not make that host's engine required — a task that requires those capabilities cannot run
on this machine until the project is reconfigured explicitly. hunsu moves no role to another host, model or provider, and
removes no alternate (without one, chongdae would send such a task to the plain role). An argv in hunsu.json whose
`{plugin:NAME}/<file>` the enabled copy lacks is an `error` for a plain role, a `warn` for an alternate. Without `hosts`, a host engine not
found here says how to write it when no plain role runs there.

**Codex hooks.** Where Codex runs anything here (a host of this machine, a plain role's worker host, an alternate's host
`hosts` does not leave out, or the host `check` runs on), `check` reads the project's `.codex/hooks.json` from either host
and names each hook whose program or script is gone. The command's words are taken apart as a shell would split them, never
run: an absolute first word or script path must exist (a quoted path with spaces is one path; only a leading `~`, `$HOME`
or `${HOME}` is expanded), while a word with any other variable, a glob, a substitution or a separator, the code after
`-c`, and a command whose quotes do not close are left alone. In a group `lock` generated (its provenance says so) it is an
`error`, and what a Codex `lock` would do about it is decided from the groups it would write next, rendered from the
copies Codex has installed: a path that render no longer names, while every path it names is here, is repaired by
`hunsu lock` on Codex (`AGENT_HOST=codex` from a shell), which regenerates hunsu's groups, drops obsolete ones and keeps the
project's own — `lock` and `compose` on Codex do not stop for exactly those findings, and a Claude Code `lock` refuses with
that command. A path the installed plugin still generates (an absolute interpreter or an external script this machine
lacks, or a file its copy lacks: reinstall) blocks every lock until it is here, whatever newer copies the cache holds; so
does a repair whose render names a missing path itself, and on Codex any path that render names and this machine lacks,
with or without a hooks file yet — a first Codex `lock` never writes a hook that fails. A file of the wrong shape (or an
unreadable provenance) is an error on Claude Code and an exit-1 message on Codex, never a traceback. In any other group it is a `warn`: the project's own group,
which `lock` keeps — one naming a copy in the Codex plugin cache is likely a group generated before hunsu recorded which
groups it wrote (1.15.1), to remove by hand. (guin-site, 11-01: generated groups still named a removed 1.x copy and Codex
prompts were blocked until a Codex `lock` regenerated them; nothing had said so.)

**Dispatched hooks.** A group that runs `.codex/hunsu-dispatch.py` is judged as the dispatcher will run it: its
`NAME@MARKET` is resolved with the dispatcher's own function against Codex's registry and cache, from either host.
`dispatches to NAME@MARKET, which is not installed for Codex on this machine (why)` and `dispatches to NAME VERSION's
REL, which that copy lacks` say a hook the dispatcher stops (exit 2) on every event — an `error` in a generated group,
a `warn` in the project's own. While hunsu.json declares the plugin, the repair is to install it for Codex
(`codex plugin add NAME@MARKET`), or to reinstall a copy whose `hooks/codex.json` still names the missing script, and no
lock repairs it; when a regeneration would no longer write the group (the plugin left hunsu.json, or a release dropped
the hook) a Codex `lock` passes and drops it. `codex hook dispatcher: …` is one `error` when the dispatcher a group runs
is missing — a Codex `lock` passes it only where it writes that file, this project's own with a generated group
dispatched — and one `warn` when it is not the one this hunsu writes; `hunsu upgrade --hooks-only` rewrites it. On Claude
Code, a dispatched generated group whose copy is not the version hunsu.lock.json pins gives one `warn`, `codex hooks: the
dispatched groups run the copies installed for Codex — …`; on Codex the drift error says it.

**Versioned groups.** A generated group still naming an existing copy in the Codex plugin cache (written before 2.4.0)
is one `warn` for all of them, `codex hooks: N generated group(s) name versioned copies in the Codex plugin cache — …`,
naming each plugin, version and event: they fail once Codex replaces that copy, and `hunsu upgrade --hooks-only` (either
host) or `hunsu lock` on Codex rewrites them through `.codex/hunsu-dispatch.py`; a command that is not one simple command
is counted as staying versioned. Such a group is active — the not-active comparison counts it present — and a Claude
Code `lock` does not stop for the warning. A project's own group naming an existing versioned copy is a `warn` of its own;
hunsu keeps it, so fix it in `.codex/hooks.json`. A missing versioned path keeps the error above.

**Not active.** A plugin installed for Codex is not an active hook until `.codex/hooks.json` holds its groups: Codex runs a
plugin's hooks only from the project's file, which hunsu generates per machine and which is not committed, so a new
clone or worktree has none. Where Codex runs for the project on this machine, `check` compares the groups a regeneration
would write now (rendered from the copies installed for Codex, on either host) with every group in the project's file.
A group is present in its dispatched form or in the current copy's versioned form (said as versioned above). Any group
missing from it is one `error`, `codex hooks: N hook group(s) the copies installed for Codex declare are not active in
this project — …`, naming each plugin with its installed version and the events of its missing groups, whether
the file does not exist or lacks them, and the generated groups from an older copy still in the cache that a
regeneration drops. The repair is `hunsu upgrade --hooks-only` on either host, or `hunsu lock` on Codex; both keep the
project's own groups. A Codex `lock` does not stop for this error (it writes exactly that render); a Claude Code `lock`
does. Nothing is said when no copy installed for Codex declares a hook, when Codex is not installed or its registry does
not read from here, or when Codex is not a host of the project on this machine; an unreadable file keeps its own error.
(guin-site, 11-05: a worktree with valid installed hook scripts and no project file had no finding at all.)

A version skew between this machine and the manifest (`plugin X: manifest A, host B`), a plugin behind a release, and the
stale-session line say the way out as numbered steps, each only when it is owed, in this order: `claude plugin marketplace
update M`, `claude plugin update X@M --scope project` (in the project), restart the session (a `/clear` does not reload
plugins), `hunsu upgrade X` — which takes the installed release into hunsu.json, rewrites the generated Codex hooks and
lists the situations a `--stale` judge round re-reads — then `hunsu lock`, committed with `hunsu.json` inside a run, like
any change to the environment. On Codex the first two are `codex plugin marketplace upgrade
M`, which refreshes this machine's Git snapshot of the marketplace and the plugins installed from it, and a verification
that the installed copy is the intended release — its version, and the revision its `.codex-marketplace-install.json`
receipt names, and its files compared with the release's (`check` compares content only with what a lock recorded for the
same version) — before the rest; an upgrade takes what the marketplace publishes now and by itself proves no version.
`codex plugin remove` + `add` is not part of an ordinary update — the upgrade has refreshed the copy already: it is for a
copy `check` finds corrupted (reinstall) and, through `install --refresh`, for a linked development source, which is the
step `outdated` names for a development-source link. A development-source link keeps `hunsu add X` to take the goal into hunsu.json and the
separate `hunsu check` step with `hunsu judge request --out DIR --stale`, judging those packets, and
`hunsu judge consume --dir DIR`: `upgrade` does not take a development-source link. The stale-session line uses the same exception
when hunsu itself links to a development source. Generated Codex groups that run through the dispatcher follow the copy Codex installs;
groups generated before 2.4.0 name the old copy until `hunsu upgrade --hooks-only` (on either host) or a `hunsu lock` on
Codex rewrites them.

`check` reads project data without writing project state, but rendering role prompts and modes may need temporary
scratch files; `--remote` also needs scratch space for marketplace clones, and the mutation lock that every command
changing the project (`init`, `add`, `remove`, `install`, `link`/`unlink`, `dev`, `capability`, `role add`, `compose`,
`lock`, `judge`) takes first lives in a temporary directory. Where that space is unavailable (a completely read-only sandbox can
give none), the command stops nonzero with `no writable temporary directory` and what needed it: no summary line, no
findings document, nothing judged or written; the SessionStart line says `check` could not complete. Set `TMPDIR` to a
writable directory or use a sandbox that allows writing there (Codex `workspace-write`).

### `outdated`

read-only, and the one command that asks the network: per locked plugin (the lock's, else `hunsu.json`'s) its locked
version, what is installed for this project, this machine's copy of the marketplace, and what the marketplace publishes
now — with the steps above when something is owed. A git marketplace is read by a shallow, blob-less clone of its
`origin` into a temp directory, deleted after: the copy the host installs from, the plugin cache and the install records
are not touched (`claude plugin marketplace update` stays the step that moves them); a directory marketplace is what is
there. A marketplace that cannot be reached is said on its plugins' lines (`published ?`), and the rest are still asked;
git never prompts for credentials. Install records of the same plugin under another marketplace in
`installed_plugins.json` are named: one that applies to this project (a second copy that can load here) with the
uninstall that keeps the lock's, one for other projects only as not this project's. Writes nothing. Exit 0

### `policy`

read-only: this project's resolutions by situation, for a session mid-way or a person — so nobody opens `hunsu.json`
and `hunsu-conflicts.md` by hand to learn what was decided and on whose word. Per situation: the line the SessionStart
hook prints (from `hunsu.lock.json` — what was locked is what sessions get; an `accept` or `note` the line cuts follows
whole), and who settled it (from `hunsu.json`, where declarations are read: `reviewed` by a name, `delegated:` the
judge's reason, unreviewed, or written by hand). Where the two files differ it says which it read: a resolution not yet
locked, one gone from `hunsu.json`, a rule changed since the lock, or one settled again since. Under an unreviewed
resolution, the conflict it answers: each finding for a person with the members' sentences and what the judge proposed —
a person judges that, not the rule alone (asked to show the rulings, a session put the five rules as yes/no questions
with the recommended answer "yes", and recorded the yeses: sagoforce, 10-26) — and how to record what they said. A
delegated resolution is not owed a person. Last, the judged
situations (`hunsu-judgments.json`) with a finding for a person and no resolution; whether those judgments are current
is `check`'s to say. Exit 0

### `role add` / `role list`

a role's capability alternates. chongdae hires `<role>@<cap>[+<cap>]` from the lock's `roles` for a task that requires
those capabilities (`nitpick@loopback`: the same member on a worker adapter modeled as allowing a local port),
and when a member lacks one it asks the person whether to add it. Yes is one command:

    hunsu role add nitpick@loopback --from nitpick --host claude [--model M] [--effort E] [--force]

copies ROLE's provider from `hunsu.json` `roles` (an argv as written; `plugin:role` materialized the way `lock` does,
`{host}` filled with HOST) into a new key, with `--host` replaced — or added, for a worker on the request/response protocol
(`{request}` in its argv) that has none. `--model` and `--effort` are dropped unless given, and the output says so: a model
named for one host is not valid on another. `--from` defaults to NAME's role. Refused, writing nothing: ROLE absent,
NAME@CAP already there (unless `--force`), a `session` or `native:` role (no worker whose host could change), a command
that is neither a worker nor has a `--host` (hunsu cannot tell how it would run elsewhere — write that one by hand).
Writes `hunsu.json` only and prints the new entry and the next step, `hunsu lock`. `role list` (read-only): each role, a
short line for its provider (member, host, model, effort), its alternates under it, and the keys where `hunsu.json` and
the lock differ. Exit 0; `add` exits 1 on a refusal. `role add` is also the project's answer for those capabilities: it
records `"capabilities": {"<cap>": "alternates"}` (see `capability`), and says so when HOST's sandbox lacks CAP too

### `capability <cap>.. --session | --alternates [--host H [--model M] [--effort E]]`

the project's answer, asked once at setup so a run never stops to ask: do hired hands need `loopback` (a local port: a
dev server like `wrangler dev`) or the `network`? The current `SANDBOX_LACKS` table in `hunsu.py`
models Codex workers as lacking both and Claude workers as lacking neither. This is a fixed routing assumption,
not a live capability probe or a guarantee for every host configuration. `--session`: work a hired member's
sandbox cannot do is the session's; `--alternates`: it is hired on a `<role>@<cap>` role. The setup answer is one
command — `hunsu capability loopback network --alternates --host claude` adds `<role>@<caps>` on that host for every
worker role on a host lacking them, by `role add`'s rules (argv copied, host swapped, `--model`/`--effort` dropped unless
given), skipping roles an alternate already covers; without `--host` it records the decision only (`role add` records it
too, for one role). Writes `hunsu.json` `capabilities`, and also `roles` when `--host` creates alternates.
Chongdae reads the capability decision from the manifest; new role providers must be resolved with `hunsu lock`
before use. Undecided or `session` → the session does such work; `alternates` with a `<role>@<cap>` role → hired there.

### `lock`

write `hunsu.lock.json`; refuses on errors, records warnings. The recorded `warnings` are what `check` says once the lock
— on Codex, also the hook groups it writes — is in place, so a warning about the lock it replaced, or one its own
regeneration repaired, is not recorded (the first `lock` after `upgrade` once recorded "lock pins <old>" for versions it
then pinned). Two kinds of field in it. Code reads: `plugins.*.version`
and `.fingerprint` (`check`, chongdae's builder), `skills` and `denied` (the pre-skill hook), `resolutions` (the
session-start line), `roles` (chongdae), `reporters` and `record-paths` (dwitbuk, hacheong), `settings` (dwitbuk's stop
hook, with `hunsu.json` overriding). The rest — `judge`, `warnings`, `hooks`, `engines`, `skills-fingerprint`,
`roles-declared`, `host`, `artifact-type`, `plugins.*.source`/`marketplace`/`linked` — is for the person reading the
lock in a diff: what was found and where it came from (an `engines` value is `null` when nothing was found, or when this
machine's `hosts` left that host out and nothing required it, so it was not probed — `check` says which). Nothing decides on them; do not cite them as a mechanism.

`plugins.*.linked` is absent for an unlinked plugin, `true` for a development-source link, or
`{"installed-copy": "claude-code", "version": "1.0.0"}` for an installed-copy link (the host may also be `codex`).
It describes the link; nothing decides on it.

`.fingerprint` is the installed copy's content: every file's path and bytes (CRLF read as LF), leaving out `__pycache__/`,
`.git/`, `*.pyc`, `.DS_Store`, the host's `.in_use` marker, and the receipt Codex's marketplace installer writes at a copy's
root, `.codex-marketplace-install.json` (where it installed from, at which revision) — so one release installed by Claude
Code and by Codex is one content. Only that root file is left out: the same name deeper in the tree, any other `.codex-*`
file and other dotfiles are content. A lock written before this rule from a copy that carried the receipt (a lock written
on Codex) hashed it in: `check` now says `content differs` for that plugin although the payload did not change, and
nothing migrates it — verify the installed copy against its release, then `hunsu lock` again (`add --take-content` is for
a release rewritten under its version, not for this). A lock written from a copy without the receipt keeps its fingerprints.
A linked development source is compared with the installed copy through the directory its catalog selects (see
[`link`](#link--unlink)): a repository's `plugin/`, so what the checkout around it holds — the project's own hunsu.json,
tests, run and review records — is never content and never makes `content differs`.

### `compose`

the whole flow: init → add → engines → check → capabilities → judge → lock, stopping at each human decision with exit 2
and a `decision:` line. The capability question (do hired hands need a local port or the network?) is asked here, once,
with both answers as commands — never mid-run. Re-run after answering. `/hunsu:compose` runs it

### `remove <plugin>`

drop a plugin and its link; resolutions that still name it are listed — the judged set changed, so judge again

### `judge request` / `judge consume`

conflict judgment — see below

### `install`

the second machine: for each plugin in the manifest that is missing here, `claude plugin marketplace add` + `claude
plugin install <plugin>@<marketplace> --scope project`, then tells you to `check`. An `unpublished` plugin installs
from its `hunsu link <plugin> <marketplace dir>` on this machine, or not at all. A plugin present at another version
is reported as drift, not reinstalled. `--refresh <plugin>…` (or `all`) re-snapshots a linked *development source* — Claude Code:
uninstall + install at project scope; Codex: `plugin remove` + `plugin add` — so an edited or bumped source reaches
the copy the host loads. (Claude Code loads a `directory` marketplace's plugins in place, so there the refresh only
updates the host's record.) `--refresh all` selects development-source links alone. Naming an installed-copy link
refuses before any host command: there is no development source to re-snapshot; `hunsu upgrade X` compares with the pin,
and `hunsu link X DIR` or `hunsu unlink X` changes the link.

### `link` / `unlink`

this machine only; `unlink` removes the local link. A link directly to a version directory in either host's
`plugins/cache/<marketplace>/<plugin>/` is an installed-copy link when its real path matches that cache directory and
its plugin manifest names the declared plugin. Both caches are read, whatever host runs hunsu; a symlink to a copy
counts, a lookalike path outside the caches or a copy from another marketplace does not. Its manifest supplies the
version (the directory name when absent). Other links are development sources.

A development-source link names a marketplace directory; the plugin in it is found the way a host finds it, and only
there. A catalog that lists the plugin (`.claude-plugin/marketplace.json` or `.agents/plugins/marketplace.json`, this host's
first) decides: its entry's directory — a string source, or Codex's `{"source": "local", "path": …}` — inside the linked
directory. A repository root whose catalog selects `./plugin` is therefore read as its `plugin/`, never as the checkout
around it with its own hunsu.json, tests and records; when that directory is missing, nothing is taken in its place and
`check` says so. With no catalog entry, `<dir>/<plugin>` and then the directory itself are tried (an umbrella directory
holding repositories, or a plugin directory in the root layout older plugins have). A directory that is itself a
marketplace is read the same way, two levels deep at most; a source that leaves the linked directory (absolute, `..`, a
symlink out) or whose manifest names another plugin is never taken

### `dev <plugin>` / `dev --off <plugin>`

this machine only: run a plugin's working source in this project — to try an unreleased change where the committed
settings install the released one. Without `--market`, the first `directory` marketplace in which the plugin resolves as
for a link (a repository root, an umbrella holding it, a root-layout plugin) is taken. Enables `<plugin>@<local marketplace>` at local scope in
`.claude/settings.local.json` (the host's machine-local settings, never committed) and switches the manifest's copy
off there; recorded in `hunsu.local.json`. `check` reports it as in development, not as drift; `lock` refuses until
`--off`, so a lock never names unreleased content. A trial is not committed in the project: what was changed to try it
is reverted (or it runs in a throwaway copy), and the project's commit that uses the change follows the release —
`--off`, install, `lock`. While any plugin is in development here, the manifest's `dev-settings` are this machine's
settings overlay (below); the last `--off` takes back what it applied

## Conflicts between skills

Two skills conflict when following both in the same situation gives contradictory behavior or duplicated authority:
**overlap** (both claim the work in one situation — a `review` and a `pr-check` that both answer "review this"; two skills that only share a name, a design review and a code review, do not), **contradiction** (one instructs what
the other forbids —
a standing mode's "never stall, pick a default" against a skill's human-confirmation gate), **premise** (one assumes
what the other forbids).
The agent does not stop at a conflict; it improvises a compromise that differs per session. That is intent nobody
defined, and the manifest is where intent lives.

hunsu does not judge meaning. It packs the question for a judge the host runs (a fresh, read-only session with a fixed
answer schema) and validates the answer:

1. `judge request --out DIR` — stage 1: names + descriptions of the locked skills, the roles' worker prompts and the
standing modes → the judge groups them by the **situation** they claim (`cluster-response.json`).
2. `judge request --out DIR --groups DIR/cluster-response.json` — stage 2: one packet per group with each member's
full text → findings with **quotes from each member**, a reason, and a proposed resolution.
3. `judge_worker.py --request F --response F [--host claude|codex]` runs one packet.
4. `judge consume --dir DIR` — rejects findings without quotes, with a quote that is not in the member's text (whitespace aside; members travel whole, never cut), with a quote of a mode that names one state (a run id `run-YYYYMMDD-HHMMSS-xxxx`, a timestamp, a count such as "1 task(s) open" or "errors 1" — the reason asks for the mode's sentence that holds in every state), or with unknown members, writes `hunsu-judgments.json`
(each situation with a fingerprint of its members' text — a version bump that changes no text stales nothing) and
`hunsu-conflicts.md` (what was found, the evidence, the proposals — for a human to read).
5. Each finding carries a `class` — how much it asks of a person. `authority`: the members diverge on who decides, a
person's confirmation, or an action hard to undo (commit, delete, publish, install, spend). `sequenced`: one member's text
already says how it yields ("writes go through a run"). `not-co-active`: the two never apply in the same moment.
`duplicate`: either gives the same result. Every class but `authority` stands on `class_quote`, a sentence hunsu finds
in that member's text; no class, an unknown one, or a sentence not there is `authority` (fail closed). Measured on
2026-09-28: three planted conflicts (a "never commit" mode against a skill that commits its review; two differently
named review skills; "ask before any decision" against a worker told to ask no one) all came back `authority`; of the
six findings a person had resolved on a walk-through project, all with accept/order, four came back settled.
You write `resolutions[<situation>]` for the `authority` ones. `check` fails for a situation with an `authority`
finding and no resolution; the same finding (members and quotes) in another situation is covered by one resolution.
A rejected finding (no quote, a quote not in the text — JSON escapes and whitespace aside) is a `check` warning: a real
conflict may be among them. `lock` carries
the resolutions, and the SessionStart hook **materializes** them: one line per situation in the session's first
context ("reviewing a change: use dwitbuk:review; never other:review", `[unreviewed]` when a proposal was applied
unread). `deny` is also enforced by the sentinel at call time; `use`, `order`, `accept` act through these lines.
Measured: the same request reached for a different skill with the line than without, 2/2 each way.
6. Later, a member's text changes (its SKILL.md, a worker's prompt, a mode's script or command): only the situations
it is a member of go stale. A judgment is about text, so a version bump that changes no member's text stales nothing —
a patch release costs no judge round. Each situation records every member's identity on its own (`member-fingerprints`)
and what the judge read of each mode (`modes-read`, the text it says in an empty project — below): a mode's identity is
its script, but a script edited in a way that leaves its text as it was (chongdae 1.20.0 changed how its line counts open
tasks) changed nothing the judge read, so it stales nothing — the hook is run to see, only for a mode whose script moved.
What the judge read depends on the script alone: a run opened or closed, a review written, a check gone red stale nothing. A re-judge that finds the same findings (kind, members, quotes, class) keeps them word for word, their
reason and proposal and the situation's `judged-by` with them; only what it was judged about is recorded anew — a
judge words its reasons anew on every call, and a record rewritten with each rewording is a diff nobody can read. (Judgments made before this carry only a version fingerprint and stay valid by
version until re-judged.) `judge request --out DIR --stale` writes packets for those alone; `consume` merges them over
the old ones. Judgments made before 1.14 read each mode in the project itself, so a quote can be one session's line
(guin-site 10239ce: "hunsu: environment differs from hunsu.json (errors 1 · …)", and a delegated reason that "names
run-20261002-050435-e75b as the open run"). `check` warns of a finding whose mode quote names a run id, a time or a count,
and of a delegated reason that does (`policy` marks it); nothing is rewritten — `--stale` re-judges those situations, and
also one whose mode quote is a sentence the mode does not say in an empty project, saying which and why. A still-fits whose
quote or reason names one state records no delegation (`reviewed: false`); a class sentence that does is refused (`authority`). A plugin that brings a skill no situation has seen needs a new cluster round (stage 1). If a re-judged
situation's findings differ from what its resolution was written against, the judge's `resolution_verdict` decides. A
still-fits whose quote is verbatim in the members' text and names no state replaces `reviewed` with a delegation record
(`{"delegated": "judge <host> <model>: <why>"}`) for a resolution already reviewed — by a person, by hand, or by an earlier
delegation — and leaves a proposal nobody has read at `reviewed: false`: the judge's word does not turn an unread proposal
into approved history. Any other verdict marks the resolution `reviewed: false`. Either way it stays, and dwitbuk lists a
`reviewed: false` one until a human re-reads.
A verdict that does not confirm the resolution (`does-not-fit`, `cannot-tell`) is kept with its situation in
`hunsu-judgments.json` as `resolution-verdict`, whether or not the findings changed: the verdict, its whole why and
quote, whether hunsu found the quote, the judge, and a fingerprint of the hunsu.json entry it was about. `check` warns
of a `does-not-fit` until that entry is edited or a later re-judge says otherwise. Recording the verdict changes no
resolution and no `reviewed`; it is no one's approval or refusal. `hunsu-conflicts.md` shows it for a person to re-read.

A judgment is about its members' words. Since 2.3.0 each situation's fingerprints are taken over the text with
whitespace normalized (`" ".join(text.split())`), recorded as `fingerprint-text: whitespace-normalized`. This covers
`member-fingerprints`, `members-text-fingerprint` and `modes-read`, including each mode's injected text. A rewrap,
re-indentation or blank-line change stales nothing; any word or punctuation change does, including joining or splitting
words. The judge still receives the text itself, and quotes are checked against that text.

A situation judged before this records no form and is compared raw: it stays fresh while its raw fingerprints match,
and goes stale on any change once, a rewrap included. The re-judge then records the new form, even when its findings
are kept unchanged. `check`, `lock` and `upgrade` rewrite no judgment record.

A judge's reading of unchanged words can vary. On 2026-11-06 a re-judge after a rewrap classed three findings as
`authority` that the earlier round had settled as `sequenced`. hunsu does not re-run a judge to average it out.
A whitespace-only change no longer asks a judge at all; a changed word asks a judge again, and its verdict is a new
reading a person reads in `hunsu-conflicts.md`.

What the judge reads is what a session reads, never a script's name. A skill is its SKILL.md. The project's committed
instruction files — `CLAUDE.md`, `.claude/CLAUDE.md`, `AGENTS.md` — are modes (`file:<path>`, members of every
situation, identified by their content); a situation judged with other modes than a session now gets (one added, one
removed) is stale. A mode (a plugin's
SessionStart hook) is the text it injects: hunsu runs the hook the way the host does — a `SessionStart` payload on
stdin, `${CLAUDE_PLUGIN_ROOT}` resolved, `HUNSU_SURVEY=1` in the environment — and packs `additionalContext`, but in an
empty project: a fresh temporary directory, no records, no git above it, as the payload's `cwd` and the hook's working
directory. A hook that reports the project's state says what it says of a project with none, so the text is the same on every
machine and at every moment, and the packet tells the judge it is the standing text. A hook does better to answer its
standing text under `HUNSU_SURVEY=1` — what it can say in any state, as a template without live ids, times or counts
(jokbo, dwitbuk and chongdae do; hunsu's own says what its line does, without a check's counts or the resolutions — those
are the judge's own answers). Running the script beats a declared description: a declaration drifts from what the hook says,
the empty project reads what it actually says. In the project itself, chongdae's line moved from "run … in progress" to
"no run in progress" between two rounds and six of nine situations were re-judged for it, their quotes and reasons
carrying the run's id as a standing fact (guin-site 10-15). A role (a plugin's `roles` argv,
the worker a runner starts) is the prompt that worker is sent: hunsu asks the argv itself with `--prompt-only` on a
sample request, and packs the answer as member `plugin:role`. A worker inherits the project's modes, so a mode that
says "ask the user" beside a role that says "ask no one" is a contradiction the judge can now quote from both sides.

A first judgment is rough by design; refine when a real conflict shows up. Modes are members of every situation, so
with a mode every group is judged (cost: 1 + groups calls).

## Manifest

```json
{
  "engines": {"claude-code": ">=2.1", "python": ">=3.11", "node": ">=20"},
  "plugins": {"hacheong": {"version": "1.1.0", "source": "github:guinjaaaaaaaaaaaaaaaaaaaaakeop/hacheong"}},
  "resolutions": {"review": "dwitbuk"},
  "local-hooks-ok": ["my-observer.py"]
}
```

- `engines` — floors the team requires. `lock` records what this machine actually had. A plugin may declare its own
  floor (`"engines": {"python": ">=3.11"}` in its plugin.json, npm's convention); `check` then requires the team's
  floor to cover it and this machine to meet it, so a plugin whose workers need a newer runtime fails as one line
  here, not as a traceback in a worker later. The six active products (hunsu, chongdae, mangsang, jokbo, hacheong,
  dwitbuk) declare `python >=3.11` in the three manifests of their plugin (`plugin.json`, Claude's and Codex's); an installed copy of an
  earlier release may still declare `>=3.9`, and that is the floor `check` reads for it. This is the supported minimum,
  not a claim that every runtime path has been tested on each newer Python version. The Codex survey reads `~/.codex/config.toml` with
  `tomllib` (3.11+); on an older interpreter it refuses with that reason instead of a traceback.
- `roles` — the project's declaration of who does what: `{"implementer": "hacheong:build", "verifier": "dwitbuk:eyes",
  "planner": "session"}`. A provider is `session`, an argv (`["python3", "{plugin:NAME}/worker.py", "--request",
  "{request}", "--response", "{response}"]`), or `plugin:role` — the last only when that plugin's plugin.json declares
  the role's argv (`"roles": {"build": [...]}`, `{host}` filled with this host's name at lock). Or
  `native:plugin:role`: the same declared role, run by the host's own subagent at the session's hand (see chongdae's
  providers) — locked as `{"native": argv}`. A skill id is not a provider: a skill is text the session agent reads, a
  runner spawns argv. hunsu does not assign roles; `check` verifies each declaration is real, `lock` writes `roles` as
  argv (what a runner spawns) and `roles-declared` as written. A key `<role>@<cap>[+<cap>]` is a capability alternate of
  `<role>` (chongdae hires it for a task that requires those capabilities); `role add` writes one from an existing role.
- `capabilities` — the project's answer for what a hired sandbox cannot do: `{"loopback": "alternates", "network":
  "session"}` (`capability`, `role add`). chongdae reads it from here.
- `resolutions` — keyed by situation. Shorthand for a same-name overlap: `"retro": "dakdol"` or `"deny"`. Full form,
  for a judged situation:
  `{"use": "plugin:skill", "deny": ["plugin:skill"], "order": ["plugin:skill", "plugin:mode"], "accept": "why it is fine", "reviewed": false}`.
  `deny` and `use`-on-a-name reach the lock's `denied` list, which the PreToolUse hook enforces. `order` and `accept` are facts for the agent, not enforced. A member may also be a role's prompt (`hacheong:build`, judged as `plugin:role`): naming one is a fact for the agent too — the hook guards skill calls, not the workers a runner starts.
  `"reviewed": false` marks a proposal applied without a human reading it — allowed, never hidden.
- `judge: "skip"` — lock without a conflict judgment. Recorded in the lock as `judge: skipped`.
- `local-skills-ok` — names of the person's own skills (`~/.claude/skills`) that may be used here; the sentinel refuses
  any other. In `hunsu.local.json` when it is only theirs, here when the team uses it.
- `local-hooks-ok` — script names of user-level hooks that may run here without being part of the project (observers),
  or `plugin:NAME` for a plugin enabled on this machine but not in the manifest.
  A hook that is only yours — a terminal app's agent hooks, a personal logger — goes in `hunsu.local.json` under the
  same key instead: acknowledged on this machine, never committed. The session reads what such a hook runs first; one
  that adds nothing to the session (prints nothing or `{}`, never blocks) it lists there itself and says so in a line —
  the person is asked only about what can change the session (sagoforce 10-26: an Orca hook printing `{}` stopped the
  session twice for a person who said to ignore it if it could be ignored).
- `settings` — the team's switches for its products (e.g. `{"chongdae": {"outside-runs": ["content/"]}}`). A product
  reads its own from this file, so a switch works as soon as it is written — also while a product's working source is
  tried with `dev`, when `lock` refuses. hunsu does not read them; `lock` copies them into the lock as the record of
  what was declared then.
- `dev-settings` — what a trial changes, declared by the project the way a package declares dev dependencies (e.g.
  `{"chongdae": {"commit-records": false}}`: a working source's records are written, not committed). hunsu names no
  product; `dev` applies these as this machine's overlay and `dev --off` takes them back.
- `hunsu.local.json` `hosts` — the hosts this machine runs for the project (`["claude-code"]`, `["codex"]`; never
  committed). Absent, every host `engines` names. A host left out is not version-checked unless a plain role's worker runs
  on it (see [`check`](#check)). hunsu.json has no `hosts`: the team's hosts are its `engines`.
- `hunsu.local.json` `settings` — this machine's overlay over `settings` (never committed, like `.env.local` over
  `.env`): a product reads its own from hunsu.json and lets this machine's override it.
- `reporters` — name → argv of the commands that report findings for the reviewer (`dwitbuk/findings@1`), e.g.
  `["python3", "{plugin:chongdae}/chongdae.py", "report", "--since", "{since}"]`. Locked; dwitbuk reads them from the
  lock. hunsu's own is `check --findings` (drift, unread resolutions), marked `"standing": true`: it reads the
  environment as it is now, so a drift it stops reporting is gone, not owed.

## Codex and the sandbox

Nested `codex exec` workers have failed in tested `read-only` and `workspace-write` sessions with
"failed to initialize in-process app-server client: Operation not permitted". The adapter's sandbox detection
reports this limitation; it does not probe whether a particular host configuration can launch a nested process.
Use a worker runner with the required permissions or the host's native subagents (see chongdae's `native:`
providers). A chongdae write guard coordinates project writes; it does not replace a host security sandbox.
`check` run from inside a detected sandbox says so (`session: sandboxed …`) when a role's worker runs on Codex (a
Claude worker, a plain command, `session` and `native:` do not), without probing the session and without recommending any
flag that turns a sandbox off.
A `read-only` sandbox may also give no writable temporary directory, which `check` needs to render roles and modes; it then
stops with `no writable temporary directory` (see [`check`](#check)) instead of judging without them.
Codex snapshots a plugin when it is added, and a named `codex plugin marketplace upgrade` refreshes a Git marketplace's
snapshot and the plugins installed from it; `install --refresh` re-snapshots a linked development source (remove + add).

## Limits

- Hunsu reports user-level hooks; it does not disable them for a project. Project-level hooks are managed here.
- The sentinel judges plugin skills (`plugin:name`) and the person's own skills, which it knows by their file
  (`~/.claude/skills/<name>/SKILL.md`). Any other name without a prefix is a host built-in (`code-review`, `simplify`,
  `loop`) or the project's own skill and passes — built-ins are never guessed at. The person's own CLAUDE.md is said by
  `check`, not judged: its text is theirs and would be quoted into a committed record.
- Without a judge round (`"judge": "skip"`), skill overlap is detected by name only: two differently named skills that do the same thing are not caught, and two same-named skills for different situations need an `accept`. With a round, the judge decides both.
- The host CLI installs whatever version a marketplace currently has; `install` cannot pin (verified: `claude plugin
  install` takes no version). `check` reports the drift; a human either takes the new version into the manifest
  (`hunsu upgrade <plugin>`, then the stale situations it lists are re-judged) or the marketplace owner pins.
- `install` has been exercised on a fresh host (`CLAUDE_CONFIG_DIR`).
- A link or a `directory` marketplace resolves as [`link`](#link--unlink) says. Since 2.2.0 a catalog source that leaves
  its directory (absolute, `..`, a symlink out), names a directory without the plugin's manifest, or names another plugin,
  resolves to nothing — before, such a source was followed, and a missing one fell back to the directory itself. `check`
  says so for a link and compares nothing with the installed copy. A catalog's remote sources (`github`, `url`,
  `git-subdir`) are not directories here.
- On Codex the PreToolUse hook is declared with the same `Skill` matcher; whether Codex reports skill invocations
  under that tool name is unverified.
- On Codex the shell-command hook is declared with matcher `*` and reads `tool_input.command` (a string, or argv joined);
  that Codex passes its shell commands that way is unverified.
- On Codex the UserPromptSubmit hook (`hooks/codex.json`, materialized by `lock`) is declared the same way; that Codex runs
  it and takes its `additionalContext` is unverified.
- The dispatcher (`.codex/hunsu-dispatch.py`, since 2.4.0) is tested on fake hosts only: no live Codex session has run a
  dispatched hook, and how Codex runs a hook command (which shell, which PATH) is not verified here. Whether Codex shows a
  successful hook's stderr is unverified, so the skew line is also `check`'s (on Claude Code; on Codex its drift error).
  A trusted dispatched group runs whichever copy Codex installed for that plugin and marketplace — the command text no
  longer changes with a release. A command that is not one simple command (`a && b`, a leading `VAR=value`) stays
  versioned. The dispatcher needs Python 3.11+ (`tomllib`) as `python3` on the PATH Codex runs hooks with: an older one
  (macOS's own `/usr/bin/python3` is 3.9) stops every dispatched hook with exit 2, where a versioned group ran its script.
- `compose` stops at every human decision (exit 2). It does not decide; it refuses to advance. Its gates: plugins →
  engines → check errors → conflict judgment (or `judge: skip`) → user-level hooks and skills, unmanaged plugins → lock.
- Both judge worker paths have run: Claude Code (14 calls on a 5-plugin project, 27 findings, 0 rejected) and Codex (8
  + 2 calls on the same products, 7 situations, 1 finding).
- Hosts: Claude Code (`~/.claude`, or `CLAUDE_CONFIG_DIR`; `HUNSU_CLAUDE_DIR` for tests) and Codex
  (`~/.codex/config.toml` and its plugin cache; selected by `AGENT_HOST=codex`, `HUNSU_CODEX_DIR` for tests). On
  Codex, which no longer runs a plugin's own hooks, `lock` (and `upgrade`, on either host) materializes the plugins'
  `hooks/codex.json` into the project's `.codex/hooks.json` (machine paths; gitignored; run `codex` with hook trust or
  `exec --dangerously-bypass-hook-trust`). hunsu checks that the groups are in the file, not that Codex trusts or runs
  them. Existing project hooks, later additions, and edits survive each lock; only
  the previously generated groups are replaced. Their ownership is recorded in the file's supported `description`
  field, published atomically with the hooks. Keep that generated description when editing hooks. Older generated
  files have no ownership snapshot: migration removes exact matches to currently known plugin groups and preserves
  unrecognized groups, which may include an old plugin hook that needs manual removal; `check` names one whose script is
  gone (see [`check`](#check)). The survey reads Codex's own registry (`config.toml`: marketplaces,
  plugins, the user's hooks) and its plugin cache.

- `judge_worker.py` starts its host session through `hostcall.py` — one host call for every worker of this family (hunsu's judge, mangsang's judge, dwitbuk's eyes,
  hacheong's members), vendored: the same file in each plugin, since a plugin imports no other plugin.

- **Where each product keeps its records.** A plugin's plugin.json may declare `records`: the paths it writes in a project
  (directories end with `/`, files do not — chongdae `[".chongdae/"]`, mangsang `["mangsang/", ".mangsang/"]`, dwitbuk
  `["reviews/", ".dwitbuk/"]`, hunsu its own five files). `lock` carries them as `record-paths: {plugin: [paths]}`, so a
  product that must leave the others' records alone — a runner's touched files, a reviewer's diff, a builder's tree check —
  reads one list from the lock instead of naming its siblings.

- **How each product's records are read.** A plugin.json may declare `reads`: question kind -> argv (`file` with `{path}`,
  `since` with `{since}`, `decision`; `{plugin:NAME}` and `{target}` as in roles). `lock` carries the declaring plugins'
  as `reads: {plugin: {kind: argv}}`, so a reader (jokbo) runs a product's own read command without knowing the product.

## Versioning

Semver, and a version names one content. The plugin's content is this directory: every change to it — code, skill or
command text, hooks, this README — comes with a new version in its three manifests (`plugin.json`,
`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`) and its marketplace entry. Hosts copy a plugin at install and
do not look again while the version stands, so an unbumped edit is a copy nobody can tell from the old one. **patch**:
behavior or wording, every interface unchanged. **minor**: a new command, skill, field, hook or artifact key; what exists
keeps working. **major**: an artifact type, lock or record that other products read changes shape, or a supported Python
or host version is dropped (an `engines` floor raised): a machine that ran the old release may not run the new one.
hunsu 2.0.0 is such a release: it raises the `python` floor to `>=3.11` in all three manifests, dropping 3.9 and 3.10 (the
1.x root and Claude manifests declared `>=3.9`). hunsu 2.2.0 moved the plugin into the source repository's `plugin/`
directory, which both catalogs select: what a host installs is this directory alone, and a linked repository root
resolves to it (see [`link`](#link--unlink)). hunsu's `check` fails a linked plugin whose source differs from the
installed copy under one version; for a locally linked plugin, `hunsu install --refresh <plugin>` re-snapshots after the
bump.

How a release is made — which files carry the version, the checks before publishing — and hunsu's own tests belong to
the source repository, not to the plugin.
