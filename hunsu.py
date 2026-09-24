"""hunsu — locks a project's agent environment, the way a package manager locks dependencies.

  survey  [--target DIR] [--json]   everything that runs for this project on this host: plugins, skills, hooks, modes
  init    [--target DIR]            write an empty hunsu.json (hosts = this host)
  add     <plugin> [--target DIR]   record a plugin (version, source) from the survey into hunsu.json
  check   [--target DIR]            manifest vs this machine: plugins, engines, hooks, skill overlaps. exit 1 on errors
  link    <plugin> [PATH]           this machine only: the plugin comes from a local checkout (hunsu.local.json, not committed)
  unlink  <plugin>
  lock    [--target DIR]            write hunsu.lock.json from the resolved environment — only when check has no errors
  compose [--target DIR]            the whole flow, stopping at each human decision (exit 2, last line = the question). Re-run after answering
  install [--target DIR] [--refresh P..] make this machine match hunsu.json: marketplace add + plugin install (project scope), then check; --refresh reinstalls linked plugins
  remove  <plugin>                  drop a plugin from the manifest (and its link); resolutions that name it are reported
  judge   request|consume           conflict judgment by a host-run judge: request packs (stage 1: cluster by situation, stage 2: per group), consume validates and writes hunsu-judgments.json + hunsu-conflicts.md

The manifest (hunsu.json) is committed with the project; it is the definition of "the same environment".
Host: Claude Code first. Other hosts attach when their packaging exists.
"""
import argparse
import io
import json
import os
import re
import shutil
import sys

HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("HUNSU_CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")   # the host honors CLAUDE_CONFIG_DIR; HUNSU_CLAUDE_DIR is for tests
CODEX = os.environ.get("HUNSU_CODEX_DIR") or os.environ.get("AGENT_CODEX_HOME") or os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
HOST = "codex" if os.environ.get("AGENT_HOST") == "codex" else "claude-code"   # which host this machine's survey reads; a runner on Codex sets AGENT_HOST
HOST_EXE = {"claude-code": "claude", "codex": "codex"}   # engine name in the manifest -> the executable whose --version is the floor
MANIFEST = "hunsu.json"


def load_json(path):
    if not os.path.exists(path):
        return {}
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def frontmatter(path):
    """name and description from a SKILL.md header. No YAML parser: two keys are enough."""
    text = io.open(path, encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1) if m else "").split("\n"):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description"):
            out[k.strip()] = v.strip().strip("\"'")
    return out


def plain_command(command):
    """PowerShell -EncodedCommand hides the real script behind base64(UTF-16LE). Decode it in place so check can see it —
    in place, not instead: a hook written for several systems carries the PowerShell for Windows beside the shell for
    everything else, and replacing the whole command with the decoded part hid the script this machine actually runs."""
    import base64

    def decoded(m):
        try:
            return "-Command {%s}" % base64.b64decode(m.group(1)).decode("utf-16le")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", decoded, command)


def hook_entries(hooks, source):
    """A settings/hooks.json `hooks` block -> [{event, matcher, command, source}]. command is the decoded form."""
    out = []
    for event, groups in (hooks or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                out.append({"event": event, "matcher": g.get("matcher", "*"),
                            "command": plain_command(h.get("command", "")), "source": source})
    return out


def plugin_hooks(root, manifest):
    """Plugin hooks: the manifest's `hooks` (path or inline) or hooks/hooks.json."""
    declared = manifest.get("hooks")
    if isinstance(declared, str):
        return load_json(os.path.join(root, declared.lstrip("./"))).get("hooks", {})
    if isinstance(declared, dict):
        return declared
    return load_json(os.path.join(root, "hooks", "hooks.json")).get("hooks", {})


def source_id(market):
    """A marketplace record -> one string others can install from. Local paths stay local paths: check flags them."""
    src = (market or {}).get("source") or {}
    if src.get("repo"):
        return "github:" + src["repo"]
    return (src.get("path") or src.get("url") or src.get("source") or "unknown").replace(os.sep, "/")


def survey_codex(target):
    """Everything that runs for `target` on Codex: ~/.codex/config.toml ([marketplaces.*], [plugins."name@market"]), plugin roots in
    ~/.codex/plugins/cache/<market>/<name>/<version>, the user's [[hooks.*]], the project's .codex/hooks.json. Codex no longer runs a
    plugin's own hooks (`plugin_hooks` is removed), so a plugin's hooks/codex.json is what `lock` materializes into the project."""
    try:
        import tomllib   # 3.11+
    except ModuleNotFoundError:
        raise SystemExit("the Codex survey reads ~/.codex/config.toml with tomllib, which needs Python 3.11+ — run hunsu with a newer interpreter (this one is %d.%d)" % sys.version_info[:2])
    path = os.path.join(CODEX, "config.toml")
    conf = tomllib.loads(io.open(path, encoding="utf-8").read()) if os.path.exists(path) else {}
    markets = conf.get("marketplaces", {})
    plugins, skills, hooks = {}, [], []
    for key, entry in sorted(conf.get("plugins", {}).items()):
        if not (entry or {}).get("enabled"):
            continue
        name, _, market = key.partition("@")
        cache = os.path.join(CODEX, "plugins", "cache", market, name)
        versions = sorted(os.listdir(cache)) if os.path.isdir(cache) else []
        root = os.path.join(cache, versions[-1]) if versions else ""
        manifest = load_json(os.path.join(root, ".codex-plugin", "plugin.json")) or load_json(os.path.join(root, "plugin.json"))
        m = markets.get(market) or {}
        src = re.sub(r"^//\?/", "", str(m.get("source", "")).replace("\\", "/"))   # Codex writes Windows paths as \\?\\C:\\... — a link is C:/...
        source = src if m.get("source_type") == "local" else ("github:" + src.split("github.com/")[-1].removesuffix(".git") if "github.com" in src else src or "unknown")
        plugins[key] = {"name": name, "version": manifest.get("version") or (versions[-1] if versions else None), "source": source, "marketplace": market,
                        "path": root.replace(os.sep, "/"), "engines": manifest.get("engines") or {}, "roles": manifest.get("roles") or {}, "records": manifest.get("records") or []}
        skills_dir = os.path.join(root, "skills")
        if os.path.isdir(skills_dir):
            for d in sorted(os.listdir(skills_dir)):
                sk = os.path.join(skills_dir, d, "SKILL.md")
                if os.path.exists(sk):
                    fm = frontmatter(sk)
                    skills.append({"name": fm.get("name", d), "plugin": name, "marketplace": market, "description": fm.get("description", ""), "path": sk.replace(os.sep, "/")})
        hooks += hook_entries(load_json(os.path.join(root, "hooks", "codex.json")).get("hooks"), "plugin:" + name)
    proj_skills = os.path.join(target, ".codex", "skills")
    if os.path.isdir(proj_skills):
        for d in sorted(os.listdir(proj_skills)):
            sk = os.path.join(proj_skills, d, "SKILL.md")
            if os.path.exists(sk):
                fm = frontmatter(sk)
                skills.append({"name": fm.get("name", d), "plugin": "project", "description": fm.get("description", ""), "path": sk.replace(os.sep, "/")})
    proj_hooks = load_json(os.path.join(target, ".codex", "hooks.json"))
    if not str(proj_hooks.get("description", "")).startswith("written by hunsu lock"):   # hunsu's own materialization of the plugins' hooks: those are counted as the plugins'
        hooks += hook_entries(proj_hooks.get("hooks"), "project")
    user_hooks = {k: v for k, v in (conf.get("hooks") or {}).items() if k != "state"}
    hooks += hook_entries(user_hooks, "user")
    modes = sorted({h["source"] for h in hooks if h["event"] == "SessionStart"})
    return {"host": "codex", "target": os.path.abspath(target).replace(os.sep, "/"), "plugins": plugins, "skills": skills, "hooks": hooks, "modes": modes}


def install_entry(entries, target):
    """The host keeps one install record per (scope, project). The one that applies to `target` is its own project-scope
    record, else the user-scope one, else — for a project that only enabled the plugin — the first; entries[0] was the first
    project that ever installed it, which loads a different copy once versions diverge."""
    want = os.path.realpath(target)
    for e in entries:
        if e.get("scope") == "project" and e.get("projectPath") and os.path.realpath(e["projectPath"]) == want:
            return e
    for e in entries:
        if e.get("scope") == "user":
            return e
    return entries[0] if entries else {}


def host_plugin_root(market, name, inst):
    """Where the host actually loads the plugin from — (path, how). A plugin from a `directory` marketplace is loaded in
    place: Claude Code (2.1.276, observed in a session's init event and in `${CLAUDE_PLUGIN_ROOT}`) runs it from
    `<installLocation>/<the marketplace's source for it>`, while `installed_plugins.json` still records a copy under the cache
    that nothing runs. Everything else runs from the install record's `installPath`. The survey must describe what runs."""
    src = (market or {}).get("source") or {}
    loc = (market or {}).get("installLocation")
    if src.get("source") == "directory" and loc and os.path.isdir(loc):
        in_place = linked_source(loc, name)
        if in_place:
            return in_place.replace(os.sep, "/"), "marketplace directory"
    return inst.get("installPath", ""), "install"


def survey(target):
    """Everything that runs for `target` on this host."""
    if HOST == "codex":
        return survey_codex(target)
    user = load_json(os.path.join(CLAUDE, "settings.json"))
    project = load_json(os.path.join(target, ".claude", "settings.json"))
    local = load_json(os.path.join(target, ".claude", "settings.local.json"))   # this machine's, not committed: `hunsu dev` writes here
    installed = load_json(os.path.join(CLAUDE, "plugins", "installed_plugins.json")).get("plugins", {})
    markets = load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json"))
    enabled = {**user.get("enabledPlugins", {}), **project.get("enabledPlugins", {}), **local.get("enabledPlugins", {})}

    plugins, skills, hooks = {}, [], []
    for key, on in sorted(enabled.items()):
        if not on:
            continue
        name, _, market = key.partition("@")
        inst = install_entry(installed.get(key) or [], target)
        root, loaded_from = host_plugin_root(markets.get(market), name, inst)
        manifest = load_json(os.path.join(root, ".claude-plugin", "plugin.json"))
        plugins[key] = {"name": name, "version": manifest.get("version") or inst.get("version"),
                        "source": source_id(markets.get(market)), "marketplace": market, "path": root, "loaded-from": loaded_from,
                        "engines": manifest.get("engines") or {}, "roles": manifest.get("roles") or {}, "records": manifest.get("records") or []}
        skills_dir = os.path.join(root, "skills")
        if os.path.isdir(skills_dir):
            for d in sorted(os.listdir(skills_dir)):
                sk = os.path.join(skills_dir, d, "SKILL.md")
                if os.path.exists(sk):
                    fm = frontmatter(sk)
                    skills.append({"name": fm.get("name", d), "plugin": name, "marketplace": market, "description": fm.get("description", ""), "path": sk.replace(os.sep, "/")})
        hooks += hook_entries(plugin_hooks(root, manifest), "plugin:" + name)

    proj_skills = os.path.join(target, ".claude", "skills")
    if os.path.isdir(proj_skills):
        for d in sorted(os.listdir(proj_skills)):
            sk = os.path.join(proj_skills, d, "SKILL.md")
            if os.path.exists(sk):
                fm = frontmatter(sk)
                skills.append({"name": fm.get("name", d), "plugin": "project", "description": fm.get("description", ""), "path": sk.replace(os.sep, "/")})
    hooks += hook_entries(project.get("hooks"), "project")
    hooks += hook_entries(user.get("hooks"), "user")

    # Modes: anything injecting instructions at SessionStart changes how all work is done, without being called.
    modes = sorted({h["source"] for h in hooks if h["event"] == "SessionStart"})
    return {"host": HOST, "target": os.path.abspath(target).replace(os.sep, "/"),
            "plugins": plugins, "skills": skills, "hooks": hooks, "modes": modes, "dev": load_json(os.path.join(target, LOCAL)).get("dev", {})}


def enabled(s, name, want):
    """The host's entry for a manifest plugin: the one from the manifest's marketplace, not a namesake from another — or,
    while `hunsu dev` has it in development on this machine, the one from the local marketplace it named."""
    have = s["plugins"].get("%s@%s" % (name, (want or {}).get("marketplace")))
    dev = (s.get("dev") or {}).get(name)
    return s["plugins"].get("%s@%s" % (name, dev)) if dev and not have else have


def in_manifest(manifest, sk, s=None):
    """A skill belongs to the project when its plugin, from its marketplace, is in the manifest (or it is the project's own) —
    or from the local marketplace `hunsu dev` put that plugin in development from, on this machine."""
    if sk["plugin"] == "project":
        return True
    m = manifest.get("plugins", {}).get(sk["plugin"])
    dev = ((s or {}).get("dev") or {}).get(sk["plugin"])
    return bool(m) and (m.get("marketplace") == sk.get("marketplace") or (dev is not None and dev == sk.get("marketplace")))


def cmd_survey(args):
    s = survey(args.target)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print("plugins %d" % len(s["plugins"]))
    for name, p in s["plugins"].items():
        print("  %-16s %-12s %s" % (name, p["version"], p["source"]))
    print("skills %d" % len(s["skills"]))
    for sk in s["skills"]:
        print("  %-28s %s" % (sk["plugin"] + ":" + sk["name"], sk["description"][:70]))
    print("hooks %d" % len(s["hooks"]))
    for h in s["hooks"]:
        print("  %-20s %-26s %-16s %s" % (h["event"], h["matcher"], h["source"], h["command"][:60]))
    print("modes (SessionStart injection) %d: %s" % (len(s["modes"]), ", ".join(s["modes"])))
    return 0


def cmd_init(args):
    path = os.path.join(args.target, MANIFEST)
    if os.path.exists(path):
        raise SystemExit("%s exists — not overwriting" % path)
    save_json(path, {"hosts": [HOST], "engines": {}, "plugins": {}, "roles": {}, "reporters": {}, "resolutions": {}, "local-hooks-ok": []})
    # The link file is this machine's state. Committed by accident, it breaks every other machine.
    # The conflicts doc is a rendering of hunsu-judgments.json (regenerate: `judge render`); worker transcripts are
    # session prose no one reads back — the judgment, quotes and worker account already live in the response JSON.
    ignore = os.path.join(args.target, ".gitignore")
    lines = io.open(ignore, encoding="utf-8").read().split("\n") if os.path.exists(ignore) else []
    missing = [e for e in (LOCAL, LOCAL_SETTINGS, CONFLICTS_DOC, "*-response.transcript.jsonl") if e not in lines]
    if missing:
        with io.open(ignore, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(("" if not lines or lines[-1] == "" else "\n") + "\n".join(missing) + "\n")
    print("wrote %s (hosts: %s), %s ignored. Add plugins with `hunsu add <plugin>`." % (path, HOST, LOCAL))
    return 0


# ---------------------------------------------------------------- check

RUNTIMES = ("python", "python3", "node", "powershell", "powershell.exe", "bash", "sh")


def runtime_of(command):
    """The runtime a hook command starts with, if it is one we know. Absolute paths reduce to their basename."""
    token = (command.strip().split(" ") or [""])[0].strip("\"'")
    base = os.path.basename(token).lower()
    return base if base in RUNTIMES else None


def version_of(exe):
    """`<exe> --version` -> 'X.Y.Z' or None. Only used to compare against engines; never guessed.
    `python` also tries `python3` — many hosts (macOS, most Linux) only provide the latter."""
    import shutil
    import subprocess
    candidates = (exe, "python3") if exe == "python" else (exe,)
    path = next((p for p in (shutil.which(c) for c in candidates) if p), None)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    return m.group(0) if m else None


def satisfies(found, spec):
    """spec is '>=X.Y' or 'X.Y' (exact prefix). Anything else is unsupported and reported, not guessed."""
    if not found:
        return False
    parts = lambda v: [int(x) for x in v.split(".")]
    if spec.startswith(">="):
        return parts(found) >= parts(spec[2:].strip())
    return found.startswith(spec.strip())


def floor_covers(team, spec):
    """Does the team's `>=A` floor guarantee a plugin's `>=B` floor? Only two `>=` specs can be compared; anything else is not a guarantee."""
    if not (team.startswith(">=") and spec.startswith(">=")):
        return False
    parts = lambda v: [int(x) for x in v[2:].strip().split(".")]
    return parts(team) >= parts(spec)


def role_argv(manifest, s, provider):
    """`plugin:role` -> (the argv that plugin declares for the role, None), or (None, why not). `{host}` in the declaration is
    this host's name for the workers' `--host` (claude|codex); `{plugin:NAME}`, `{request}`, `{response}` stay for the runner."""
    if not isinstance(provider, str) or provider.count(":") != 1:
        return None, "not `plugin:role`, an argv, or `session`"
    plugin, role = provider.split(":", 1)
    want = manifest["plugins"].get(plugin)
    if not want:
        return None, "plugin %s is not in the manifest" % plugin
    have = enabled(s, plugin, want) or {}
    declared = (have.get("roles") or {}).get(role)
    if not declared:
        skills = {sk["name"] for sk in s["skills"] if sk["plugin"] == plugin}
        hint = " (%s is a skill of %s — a skill is read by the session agent, not spawned; the plugin must declare the role's argv in its plugin.json `roles`)" % (role, plugin) if role in skills else ""
        return None, "plugin %s declares no role %r%s; it declares: %s" % (plugin, role, hint, ", ".join(sorted(have.get("roles") or {})) or "none")
    host = {"claude-code": "claude"}.get(HOST, HOST)
    return [str(a).replace("{host}", host) for a in declared], None


def locked_role(manifest, s, prov):
    """What the lock carries for a role: `session` and argv as written; `plugin:role` as the declared argv; `native:plugin:role`
    as `{"native": argv}` — the runner then hands the prompt to the host's subagent instead of spawning."""
    if not isinstance(prov, str) or prov == "session":
        return prov
    if prov.startswith("native:"):
        argv = role_argv(manifest, s, prov[len("native:"):])[0]
        return {"native": argv} if argv else prov
    return role_argv(manifest, s, prov)[0] or prov


def judged_by(results):
    """Who the judge was, from the responses' own `worker` records (host, model) — one name when they agree, else the set; the
    runner's claim (`--by`) is for when the record is silent."""
    names = sorted({str(r.get("judged-by")) for r in results if r.get("judged-by") and r.get("judged-by") != "unknown"})
    return names[0] if len(names) == 1 else (", ".join(names) if names else "unknown")


def hook_key(h):
    """Identity of a hook for duplicate detection: event + matcher + the script it runs — its path with a plugin's root token
    replaced by the plugin's name (two plugins' `hooks/session_start.py` are two scripts), not the interpreter."""
    scripts = re.findall(r"[\w./\\:$\{\}-]+\.(?:py|js|sh|cmd|ps1)", h["command"])
    if not scripts:
        return (h["event"], h["matcher"], h["command"][:80])
    script = scripts[-1].replace("\\", "/")
    for token in ("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}"):
        script = script.replace(token, h.get("source", "plugin"))
    return (h["event"], h["matcher"], script if "/" in script else os.path.basename(script))


def user_hook_groups(s):
    """User-level hooks, one entry per script: a tool that registers one script on thirteen events is one thing to tell a
    person about, not thirteen. `runs` is what the command names that exists on this machine (with $HOME / ~ expanded) —
    the file to read to know what the hook does, since a wrapper often picks a script per system."""
    home = os.path.expanduser("~")
    groups = {}
    for h in s["hooks"]:
        if h["source"] != "user":
            continue
        named = re.findall(r"[\w./\\:$\{\}~-]+\.(?:py|js|sh|cmd|ps1|bat|rb|pl)\b", h["command"])
        exist = []
        for n in named:
            path = re.sub(r"\$\{HOME-?\}|\$HOME\b|^~", lambda m: home, n.replace("\\", "/"))
            if os.path.isfile(path) and path not in exist:
                exist.append(path)
        key = os.path.basename(exist[0]) if exist else os.path.basename(hook_key(h)[2])
        g = groups.setdefault(key, {"events": [], "runs": [], "hooks": []})
        g["hooks"].append(h)
        if h["event"] not in g["events"]:
            g["events"].append(h["event"])
        g["runs"] += [x.replace(home, "~") for x in exist if x.replace(home, "~") not in g["runs"]]
    return groups


def check(target):
    """Everything `check` asks of a project, in order: the machine-local files, the plugins against the manifest (the rest
    waits until every plugin is here), the session, the engines, the hooks, skill-name overlap, the roles, the judgments.
    Each step is a function below; all append to one (errors, warnings, info)."""
    manifest = load_json(os.path.join(target, MANIFEST))
    if not manifest:
        raise SystemExit("no %s — run `hunsu init` first" % MANIFEST)
    s = survey(target)
    found = ([], [], [])
    check_local_files(target, found)
    if check_plugins(target, manifest, s, found):
        return found
    check_session(manifest, found)
    check_engines(manifest, s, found)
    check_hooks(target, manifest, s, found)
    check_skill_overlap(manifest, s, found)
    check_roles(manifest, s, found)
    check_judgments(target, manifest, s, found)
    return found


def check_local_files(target, found):
    errors, warnings, info = found
    # 0. the local files are this machine's (links, dev plugins, hooks only this person runs): they must never be committed
    for local_file in (LOCAL, LOCAL_SETTINGS):
        if os.path.exists(os.path.join(target, local_file)) and os.path.isdir(os.path.join(target, ".git")):
            import subprocess
            try:
                ignored = subprocess.run(["git", "check-ignore", "-q", local_file], cwd=target, capture_output=True).returncode == 0
                tracked = bool(subprocess.run(["git", "ls-files", local_file], cwd=target, capture_output=True, text=True).stdout.strip())
            except OSError:
                ignored, tracked = True, False
            if tracked:
                errors.append("%s is committed — it holds this machine's links and hooks: `git rm --cached %s`, and add it to .gitignore" % (local_file, local_file))
            elif not ignored:
                warnings.append("%s is not ignored by git — it holds this machine's links and hooks; add it to .gitignore" % local_file)


def check_plugins(target, manifest, s, found):
    """True when a plugin is missing here: the checks that read skills, hooks, roles and judgments wait for it."""
    errors, warnings, info = found
    # 1. manifest vs what is enabled here. A linked plugin is this machine's development copy: version drift is expected.
    linked = links(target)
    dev = s.get("dev") or {}
    for name, want in manifest["plugins"].items():
        have = enabled(s, name, want)
        if not have:
            errors.append("plugin %s: in manifest (from %s), not enabled on this host — `hunsu install`" % (name, want.get("marketplace")))
            continue
        if name in dev:
            # this machine runs the plugin's working source, unreleased: the lock's version and content are not what runs here,
            # on purpose, and nothing about it is committed (.claude/settings.local.json, hunsu.local.json)
            info.append("plugin %s: in development here — from local marketplace %s at %s (the manifest pins %s from %s); `hunsu dev --off %s` before locking"
                        % (name, dev[name], have["version"], want["version"], want.get("marketplace"), name))
            continue
        if have["version"] != want["version"] and name not in linked:
            errors.append("plugin %s: manifest %s, host %s" % (name, want["version"], have["version"]))
        elif have["version"] != want["version"]:
            info.append("plugin %s: linked to %s at %s (manifest %s)" % (name, linked[name], have["version"], want["version"]))
        elif name in linked:
            info.append("plugin %s: linked to %s" % (name, linked[name]))
        # the lock names content: the same version with other content than was locked is a version that lies — bump it
        locked = load_json(os.path.join(target, LOCK)).get("plugins", {}).get(name) or {}
        taken = (want.get("content-taken") or {})
        if locked.get("fingerprint") and locked.get("version") == have["version"] and os.path.isdir(have.get("path") or "") \
                and tree_fingerprint(have["path"]) != locked["fingerprint"] and tree_fingerprint(have["path"]) != taken.get("fingerprint"):
            errors.append("plugin %s: content differs from what the lock recorded for version %s — a version names one content: bump it in plugin.json (patch at least), then `hunsu lock`"
                          " (or, when the release itself was rewritten under this version, `hunsu add %s --take-content \"why\"` and lock)"
                          % (name, have["version"], name))
        elif taken.get("fingerprint") and locked.get("fingerprint") and taken["fingerprint"] != locked["fingerprint"] and locked.get("version") == have["version"]:
            info.append("plugin %s: content taken under version %s (%s)" % (name, have["version"], taken.get("why", "")))
        # a linked plugin is this machine's development copy: the installed copy (what the host loads) must be that source —
        # whatever the manifest says about versions, this is about the link and the install
        if name in linked:
            src = linked_source(linked[name], name)
            src_version = source_version(src) if src else None
            if not os.path.isdir(linked[name]):
                warnings.append("plugin %s: linked to %s, which does not exist on this machine — `hunsu link %s <marketplace dir>` (links are per machine)" % (name, linked[name], name))
            elif src_version and src_version != have["version"]:
                warnings.append("plugin %s: the linked source is at %s, the installed copy at %s — `hunsu install --refresh %s`, then the manifest decides (`hunsu add %s`)"
                                % (name, src_version, have["version"], name, name))
            elif src and have.get("path") and os.path.isdir(have["path"]) and tree_fingerprint(src) != tree_fingerprint(have["path"]):
                errors.append("plugin %s: the linked source (%s) differs from the installed copy under the same version %s — a version names one content: "
                              "bump it in plugin.json (patch at least), then `hunsu install --refresh %s`" % (name, src, have["version"], name))
        if not shareable(want["source"]):
            warnings.append("plugin %s: source %r — the team cannot install it until it has a shareable source" % (name, want["source"]))
    for key, p in s["plugins"].items():
        m = manifest["plugins"].get(p["name"])
        if dev.get(p["name"]) == p["marketplace"]:
            continue
        if not m or m.get("marketplace") != p["marketplace"]:
            info.append("plugin %s: enabled here but not in manifest — its skills are not part of this project" % key)
    if any(e.startswith("plugin ") and "not enabled" in e for e in errors):
        info.append("skills, hooks, roles and judgments are checked once every plugin is here")
        return True
    return False


def check_session(manifest, found):
    errors, warnings, info = found
    # 1b. the session this check runs in: a sandboxed Codex session cannot start the roles' workers (a nested `codex exec` dies
    # at "app-server client: Operation not permitted"), so a lock with command-provider roles cannot be run from here
    if os.environ.get("CODEX_SANDBOX") and any(isinstance(v, list) for v in manifest.get("roles", {}).values()):
        warnings.append("session: sandboxed (CODEX_SANDBOX=%s) — the roles' workers (%s) are `codex exec` processes and cannot start from inside a sandboxed session; "
                        "run a session that drives them with --dangerously-bypass-approvals-and-sandbox (the write guard is then the guard), or run the workers outside it"
                        % (os.environ["CODEX_SANDBOX"], ", ".join(r for r, v in manifest["roles"].items() if isinstance(v, list))))


def check_engines(manifest, s, found):
    errors, warnings, info = found
    # 2. engines — the floor the team requires, checked against what this machine actually has
    for engine, spec in manifest.get("engines", {}).items():
        found = version_of(HOST_EXE.get(engine, engine))
        if not satisfies(found, spec):
            errors.append("engine %s: requires %s, found %s" % (engine, spec, found or "nothing"))
    # a plugin declares its own floor (`engines` in plugin.json, npm's convention); the team's floor must cover it,
    # and this machine must meet it — otherwise the plugin's workers die at call time with a traceback nobody asked for
    for name, want in sorted(manifest["plugins"].items()):
        have = enabled(s, name, want)
        for engine, spec in sorted((have or {}).get("engines", {}).items()):
            team = manifest.get("engines", {}).get(engine)
            if team is None or not floor_covers(team, spec):
                errors.append("plugin %s: needs %s %s, manifest `engines` %s — raise it" % (name, engine, spec, "requires %s" % team if team else "does not require it"))
            found = version_of(HOST_EXE.get(engine, engine))
            if not satisfies(found, spec):
                errors.append("plugin %s: needs %s %s, found %s here" % (name, engine, spec, found or "nothing"))
    needed = sorted({runtime_of(h["command"]) for h in s["hooks"] if runtime_of(h["command"])})
    import shutil
    for rt in needed:
        aliases = ("python", "python3") if rt in ("python", "python3") else (rt,)
        if not any(shutil.which(alt) for alt in aliases):
            errors.append("runtime %s: called by a hook, not on PATH (checked %s) — that hook silently fails here" % (rt, "/".join(aliases)))
    for base in sorted({rt.replace(".exe", "").replace("python3", "python") for rt in needed}):
        if base in ("python", "node") and base not in manifest.get("engines", {}):
            warnings.append("engine %s: hooks call it but the manifest does not require it — add to `engines`" % base)


def check_hooks(target, manifest, s, found):
    errors, warnings, info = found
    # 3. hooks: duplicates, outside the manifest, machine-bound commands
    seen = {}
    for h in s["hooks"]:
        seen.setdefault(hook_key(h), []).append(h)
    for key, group in seen.items():
        if len(group) > 1:
            warnings.append("hook %s %s: registered %d times (%s)" % (key[0], os.path.basename(key[2]), len(group), ", ".join(sorted({g["source"] for g in group}))))
    # a user-level hook is the person's, not the team's: it may be acknowledged in the team's manifest, or — when it is only this
    # person's tool (an observer, a terminal app's agent hooks) — in hunsu.local.json, which is not committed, so one person's
    # setup never becomes part of the project's record
    ok = set(manifest.get("local-hooks-ok", [])) | set(load_json(os.path.join(target, LOCAL)).get("local-hooks-ok", []))
    for name, g in user_hook_groups(s).items():
        if not any(tag in h["command"] for h in g["hooks"] for tag in ok):
            warnings.append("user hook %s: runs for this project on %d event(s) (%s) and is not the project's%s%s — tell the person, offer to read what it does, "
                            "then list it in local-hooks-ok (hunsu.local.json when it is only theirs, hunsu.json when the team runs it) or remove it from their settings"
                            % (name, len(g["events"]), ", ".join(g["events"]), "; on this machine it runs " + ", ".join(g["runs"]) if g["runs"] else "",
                               "; SessionStart among them, so it can add instructions to every session" if "SessionStart" in g["events"] else ""))
    for h in s["hooks"]:
        if re.search(r"[A-Za-z]:\\|/Users/|/home/", h["command"]) and "${CLAUDE_PLUGIN_ROOT}" not in h["command"]:
            info.append("hook %s (%s): command has an absolute path — bound to this machine" % (h["event"], h["source"]))


def check_skill_overlap(manifest, s, found):
    errors, warnings, info = found
    # 4. skill name overlap inside the manifest — needs a resolution
    owners = {}
    for sk in s["skills"]:
        if in_manifest(manifest, sk, s):
            owners.setdefault(sk["name"], []).append(sk["plugin"])
    for name, plugins in sorted(owners.items()):
        if len(plugins) > 1 and name not in manifest.get("resolutions", {}):
            errors.append("skill %s: provided by %s — set resolutions[%r] to one of them or \"deny\"" % (name, ", ".join(plugins), name))


def check_roles(manifest, s, found):
    errors, warnings, info = found
    # 5. roles — a human declaration (role -> provider). hunsu does not assign roles; it checks the declaration is real and runnable:
    # `session`, an argv, or `plugin:role` where that plugin declares the role's argv in its plugin.json (`roles`) — a skill is text
    # for the session agent, not something a runner can spawn, so a skill id is not a provider.
    for role, provider in sorted(manifest.get("roles", {}).items()):
        if isinstance(provider, list) or provider == "session":
            continue   # a command (argv with {plugin:NAME}): the runner resolves it on each machine; hunsu checks names, not commands
        # `native:plugin:role`: the session dispatches the host's own subagent with the role's prompt — the same declaration, another runner
        why = role_argv(manifest, s, provider[len("native:"):] if isinstance(provider, str) and provider.startswith("native:") else provider)[1]
        if why:
            errors.append("role %s: provider %r — %s" % (role, provider, why))


def check_judgments(target, manifest, s, found):
    errors, warnings, info = found
    # 6. judged conflicts — every situation with findings needs a resolution; judgments about another set are stale
    judged, stale = judgments_status(manifest, s, target)
    if judged is None:
        info.append("no conflict judgment yet — `hunsu judge` groups the locked skills by situation and looks for overlap/contradiction")
    elif stale:
        if stale["unclustered"]:
            errors.append("judgments have never seen %s — new members (a skill, or a role's prompt) need a cluster round: `hunsu judge request --out DIR`" % ", ".join(stale["unclustered"]))
        if stale["situations"]:
            errors.append("%d situation(s) were judged about other text of their members (%s) — `hunsu judge request --out DIR --stale`, judge those packets, `consume`"
                          % (len(stale["situations"]), "; ".join(stale["situations"])))
    else:
        for r in judged.get("situations", []):
            if r["findings"] and r["situation"] not in manifest.get("resolutions", {}):
                errors.append("situation %r: %d finding(s), no resolution — see %s" % (r["situation"], len(r["findings"]), CONFLICTS_DOC))
        known = {r["situation"] for r in judged.get("situations", [])} | {sid.split(":", 1)[1] for sid in judged_ids(manifest, s)}
        for key in manifest.get("resolutions", {}):   # a cluster round names situations anew; a resolution about an old name decides nothing
            if key not in known:
                warnings.append("resolutions[%r] is about a situation no judgment has (and no skill is named so) — a cluster round renamed it; move or remove it" % key)
    judged = set(judged_ids(manifest, s))
    for key, res in manifest.get("resolutions", {}).items():
        named = [x for x in ([res] if isinstance(res, str) else [res.get("use")] + list(res.get("deny", [])) + list(res.get("order", []))) if x and x != "deny"]
        for x in named:
            if ":" in x and x not in judged and not x.startswith("plugin:"):
                warnings.append("resolutions[%r] names %s, which is not a locked skill or a declared role" % (key, x))


def cmd_check(args):
    errors, warnings, info = check(args.target)
    if getattr(args, "findings", False):
        manifest = load_json(os.path.join(args.target, MANIFEST))
        findings = [{"kind": "drift", "where": e.split(":")[0], "text": e} for e in errors]
        findings += [{"kind": "unreviewed", "where": "hunsu.json resolutions[%r]" % k, "text": "applied from the judge's proposal, marked reviewed: false"}
                     for k, r in manifest.get("resolutions", {}).items() if isinstance(r, dict) and r.get("reviewed") is False]
        print(json.dumps({"artifact-type": "dwitbuk/findings@1", "source": "hunsu", "findings": findings}, ensure_ascii=False, indent=1))
        return 1 if errors else 0
    for label, items in (("error", errors), ("warn", warnings), ("info", info)):
        for line in items:
            print("  %-5s %s" % (label, line))
    print("errors %d · warnings %d · info %d" % (len(errors), len(warnings), len(info)))
    return 1 if errors else 0


# ---------------------------------------------------------------- resolutions and judgments
#
# resolutions is keyed by *situation* — either a skill name (same-name overlap) or a situation label the judge produced.
#   "<skill name>": "<plugin>" | "deny"                              shorthand for same-name overlap
#   "<situation>":  {"use": "plugin:skill", "deny": ["plugin:skill", ...], "order": [...], "accept": "why", "note": "..."}
# hunsu never decides a resolution. It checks that every judged situation has one, and computes what is denied.

JUDGMENTS = "hunsu-judgments.json"
CONFLICTS_DOC = "hunsu-conflicts.md"
KINDS = ("overlap", "contradiction", "premise")


def locked_skill_ids(manifest, s):
    return sorted("%s:%s" % (sk["plugin"], sk["name"]) for sk in s["skills"] if in_manifest(manifest, sk, s))


def role_ids(manifest, s):
    """The roles the manifest's plugins declare, as judged members `plugin:role`. A role whose name is also a skill of the same
    plugin (dakdol's `build` was both, the worker's prompt being that SKILL.md) is judged once, as the skill."""
    skills = set(locked_skill_ids(manifest, s))
    return sorted("%s:%s" % (name, role) for name, want in manifest.get("plugins", {}).items()
                  for role in ((enabled(s, name, want) or {}).get("roles") or {}) if "%s:%s" % (name, role) not in skills)


def judged_ids(manifest, s):
    """The set a judgment is about: the locked skills and the declared roles' prompts (modes are members of every situation)."""
    return locked_skill_ids(manifest, s) + role_ids(manifest, s)


def skills_fingerprint(manifest, s):
    """Identity of the judged set: skill ids with their plugin versions. Judgments are about exactly this set."""
    import hashlib
    items = ["%s@%s" % (sid, manifest["plugins"].get(sid.split(":")[0], {}).get("version", "project"))
             for sid in locked_skill_ids(manifest, s)]
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:12]


def denied_skills(manifest, skill_ids):
    """Every locked skill that the resolutions rule out. The sentinel reads this from the lock; it never re-derives."""
    denied = set()
    for key, res in manifest.get("resolutions", {}).items():
        same_name = [sid for sid in skill_ids if sid.split(":", 1)[1] == key]
        if isinstance(res, str):
            if res == "deny":
                denied.update(same_name)
            else:
                denied.update(sid for sid in same_name if sid.split(":", 1)[0] != res)
            continue
        denied.update(sid for sid in res.get("deny", []) if sid in skill_ids)
        use = res.get("use")
        if use and same_name and ":" not in use:
            denied.update(sid for sid in same_name if sid.split(":", 1)[0] != use)
    return sorted(denied)


def members_fingerprint(manifest, members):
    """Identity of one situation's judged members by version: ids with their plugin versions. Older judgments carry this
    (`members-fingerprint`); it is what they are compared against, so a judgment made about versions stays valid by version."""
    import hashlib
    ver = lambda m: manifest["plugins"].get(m.split(":", 1)[1] if m.startswith("plugin:") else m.split(":")[0], {}).get("version", "project")
    return hashlib.sha256("\n".join("%s@%s" % (m, ver(m)) for m in sorted(members)).encode("utf-8")).hexdigest()[:12]


_MODE_TEXT = {}


def machine_neutral(text, s):
    """A text a judge will quote, with this machine taken out: each plugin's root -> `<plugin:NAME>`, the project -> `.`, the
    home directory -> `~`. What a mode injects and what a worker is sent carry absolute paths (chongdae's line names its
    engine's file); quoted verbatim into hunsu-judgments.json they would publish the machine's layout in a committed record,
    and make the same judgment read differently on every machine that checks it."""
    for p in sorted(s["plugins"].values(), key=lambda p: -len(p.get("path") or "")):
        if p.get("path"):
            text = text.replace(p["path"], "<plugin:%s>" % p["name"]).replace(p["path"].replace("/", os.sep), "<plugin:%s>" % p["name"])
    text = text.replace(s["target"], ".").replace(s["target"].replace("/", os.sep), ".")
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "~" else text


def mode_text(h, s):
    """What a SessionStart hook injects: run its command the way the host does (a synthetic payload on stdin, `${CLAUDE_PLUGIN_ROOT}`
    resolved to the plugin's root) and read `hookSpecificOutput.additionalContext` — the text the session actually receives.
    A mode is judged by what it says, not by the name of its script. The command's failure is recorded as its text.
    The probe sets HUNSU_SURVEY=1: a hook that runs hunsu itself (hunsu's own does) must not probe again, and any hook may answer
    its standing text under it. Inside a probe this returns the command."""
    import subprocess
    if os.environ.get("HUNSU_SURVEY"):
        return h["command"]
    root = ""
    if h["source"].startswith("plugin:"):
        p = next((x for x in s["plugins"].values() if x["name"] == h["source"][7:]), None)
        root = (p or {}).get("path", "")
    key = (h["source"], h["command"], root)
    if key in _MODE_TEXT:
        return _MODE_TEXT[key]
    cmd = h["command"].replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${PLUGIN_ROOT}", root)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": s["target"]})   # no session id: hooks that record sessions skip
    try:
        done = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, cwd=s["target"],
                              env=dict(os.environ, CLAUDE_PLUGIN_ROOT=root, HUNSU_SURVEY="1"))
        out = (done.stdout or "").strip()
        try:
            doc = json.loads(out)
            text = ((doc.get("hookSpecificOutput") or {}).get("additionalContext") or "").strip() or out
        except ValueError:
            text = out
        if done.returncode and not text:
            text = "<hook exited %d: %s>" % (done.returncode, (done.stderr or "").strip()[-200:])
    except (OSError, subprocess.SubprocessError) as err:
        text = "<hook did not run: %s>" % err
    _MODE_TEXT[key] = text = machine_neutral(text, s)
    return text


_ROLE_PROMPT = {}


def role_prompts(manifest, s):
    """The prompt each declared role's worker is sent — asked of the worker itself (`--prompt-only` on a stub request), so the
    judged text is exactly what a builder or a quibbler reads. Members keyed `plugin:role`, like the roles declaration."""
    import subprocess, tempfile
    out = {}
    for name, want in manifest.get("plugins", {}).items():
        have = enabled(s, name, want) or {}
        for role, argv in (have.get("roles") or {}).items():
            mid = "%s:%s" % (name, role)
            if mid not in role_ids(manifest, s):
                continue
            tmp = tempfile.mkdtemp(prefix="hunsu-role-")
            req = os.path.join(tmp, "request.json")
            # the stage a runner would send this role: the manifest's verifier gets `verify`, its implementer `build`, a role hired
            # around a task (before/after) its own name — the worker's prompt depends on it
            fixed = [str(a) for a in argv if not re.fullmatch(r"\{(request|response|host|base|since)\}", str(a))]
            assigned = next((k for k, v in manifest.get("roles", {}).items()
                             if v == mid or (isinstance(v, list) and all(t in [str(x) for x in v] for t in fixed))), None)
            # the stage a runner would send: from the manifest's assignment when there is one (verifier -> verify), else the
            # role's own name — and when the worker refuses that, the runner's other stages in turn: a project that has not
            # assigned its roles yet still gets each worker's real prompt judged, not its refusal
            first = {"verifier": "verify", "implementer": "build"}.get(assigned, "build" if role == "build" else role)
            stages = [first] + [x for x in ("verify", "build") if x != first]
            host = {"claude-code": "claude"}.get(HOST, HOST)
            cmd = [str(a).replace("{plugin:%s}" % name, have.get("path", "")).replace("{request}", req).replace("{response}", os.path.join(tmp, "response.json")).replace("{host}", host)
                   for a in argv] + ["--prompt-only"]
            for m in re.findall(r"\{plugin:([\w.-]+)\}", " ".join(cmd)):
                other = next((x for x in s["plugins"].values() if x["name"] == m), None)
                cmd = [c.replace("{plugin:%s}" % m, (other or {}).get("path", "")) for c in cmd]
            if cmd and cmd[0] in ("python", "python3") and not shutil.which(cmd[0]):
                cmd[0] = next((c for c in ("python3", "python") if shutil.which(c)), cmd[0])
            key = (mid, have.get("path", ""), named_files_fingerprint(cmd, have.get("path", "")))   # asked once per script content
            if key in _ROLE_PROMPT:
                out[mid] = _ROLE_PROMPT[key]
                shutil.rmtree(tmp, ignore_errors=True)
                continue
            text = None
            for stage in stages:
                save_json(req, {"artifact-type": "chongdae/request@1", "stage": stage, "run": "hunsu-judge", "task": "sample", "role": "verifier" if stage == "verify" else role,
                                "target": s["target"], "goal": "<the project's goal>", "brief": "<the task's brief>", "closes": ["Q-sample"],
                                "contract": {"Q-sample": "## Q-sample\n\n<an acceptance sentence>"}, "checks": [["python3", "-m", "unittest"]], "response": "<the response file>"})
                try:
                    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=s["target"])
                except (OSError, subprocess.SubprocessError) as err:
                    text = "<worker did not run: %s>" % type(err).__name__
                    break
                if done.returncode == 0 and (done.stdout or "").strip():
                    text = done.stdout.strip()
                    break
            if text is None:   # a stable text: the temp request's path would make every check a different member
                text = "<worker printed no prompt for a sample request at stage %s>" % " / ".join(stages)
            text = machine_neutral(text.replace(tmp, "<tmp>"), s)
            shutil.rmtree(tmp, ignore_errors=True)
            script = next((c for c in cmd if os.path.isfile(c) and c.endswith((".py", ".js", ".sh"))), cmd[1] if len(cmd) > 1 else cmd[0])
            _ROLE_PROMPT[key] = out[mid] = {"id": mid, "plugin": name, "role": role, "path": script.replace(os.sep, "/"), "text": text,
                                            "description": "the prompt the `%s` worker is sent (plugin.json roles of %s) — a worker acts on it in a fresh process, asks no person anything" % (role, name)}
    return out


def named_files_fingerprint(tokens, root):
    """The content of the files a command's tokens name under `root` (its script) — "" when it names none. A script edited under
    one command line is then a different member; a bump that touched only a README is not."""
    import hashlib
    parts = []
    for cand in tokens:
        if os.path.isfile(cand) and (not root or os.path.abspath(cand).startswith(os.path.abspath(root))):
            with open(cand, "rb") as fh:
                parts.append(hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()[:12])
    return (" #" + "+".join(parts)) if parts else ""


def mode_script_fingerprint(h, s):
    root = next((p.get("path", "") for p in s["plugins"].values() if h["source"] == "plugin:" + p["name"]), "")
    tokens = [next(t for t in tok if t).replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${PLUGIN_ROOT}", root)
              for tok in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', h["command"])]
    return named_files_fingerprint(tokens, root)


def member_texts(s, manifest=None, scripts=True):
    """What identifies each judged member, per id: a skill's SKILL.md (LF-normalized), a role's worker prompt, a mode's
    SessionStart command(s) plus the script files the command names. A judgment is about text; a version bump that changes
    no text changes no judgment. A mode is judged by what it injects (mode_text), but that text may carry the project's state
    (a run in progress, a check's result) and would stale every situation each time the state moved — so a mode's identity
    is its command and the content of the script it runs, not the text of one session."""
    texts = {}
    for sk in s["skills"]:
        path = sk["path"]
        texts["%s:%s" % (sk["plugin"], sk["name"])] = io.open(path, encoding="utf-8").read().replace("\r\n", "\n") if os.path.exists(path) else ""
    for h in s["hooks"]:
        if h["event"] == "SessionStart":
            texts.setdefault(h["source"], []).append(h["command"] + (mode_script_fingerprint(h, s) if scripts else ""))
    if manifest is not None:
        for mid, r in role_prompts(manifest, s).items():
            texts[mid] = r["text"]
    return {k: "\n".join(sorted(v)) if isinstance(v, list) else v for k, v in texts.items()}


def members_text_fingerprint(s, members, manifest=None, scripts=True):
    """Identity of one situation's judged members by content — see member_texts. A member no longer present hashes as absent.
    `scripts=False` is the identity judgments made before 1.2 carry (a mode by its command alone); judgments_status accepts
    either, so an upgrade stales nothing — the next consume records the fuller one."""
    import hashlib
    texts = member_texts(s, manifest, scripts)
    return hashlib.sha256("\n".join("%s\0%s" % (m, texts.get(m, "<absent>")) for m in sorted(members)).encode("utf-8")).hexdigest()[:12]


def judgments_status(manifest, s, target):
    """(judgments or None, stale) — stale = {"situations": [labels whose members changed version], "unclustered": [locked skills no
    situation has seen]}; empty when the judgments are about exactly this set. Judgments without per-situation fingerprints
    (older) are stale as a whole when the set changed."""
    j = load_json(os.path.join(target, JUDGMENTS))
    if not j:
        return None, {}
    locked = set(judged_ids(manifest, s))
    if "skills" not in j:   # older judgments: about the whole set at once; stale as a whole when the set changed
        changed = j.get("skills-fingerprint") != skills_fingerprint(manifest, s)
        stale = {"situations": [r["situation"] for r in j.get("situations", [])] if changed else [], "unclustered": []}
    else:
        def changed(r):   # judged about text (current) or, for older judgments, about versions — each compared the way it was made
            members = r.get("members", []) + r.get("modes", [])
            if "members-text-fingerprint" in r:
                return r["members-text-fingerprint"] not in (members_text_fingerprint(s, members, manifest), members_text_fingerprint(s, members, manifest, scripts=False))
            return r.get("members-fingerprint", "?") != members_fingerprint(manifest, members)
        stale = {"situations": [r["situation"] for r in j.get("situations", []) if changed(r)],
                 "unclustered": sorted(locked - set(j["skills"]))}
    return j, stale if stale["situations"] or stale["unclustered"] else {}


def cmd_judge(args):
    """Two stages, both done by a judge the host runs (read-only, fixed schema). hunsu packs and unpacks; it does not judge.

      judge request --out DIR                       stage 1 packet: names + descriptions -> the judge groups skills by the situation they claim
      judge request --out DIR --groups FILE         stage 2 packets: one per group, with the members' full text -> overlap/contradiction/premise findings
      judge request --out DIR --stale               stage 2 packets for stale situations only (a member's text changed) — the rest of the judgments stand
      judge consume --dir DIR                       validate every response (quotes required), write hunsu-judgments.json and hunsu-conflicts.md
    """
    target = args.target
    manifest = load_json(os.path.join(target, MANIFEST))
    if not manifest:
        raise SystemExit("no %s" % MANIFEST)
    if args.mode == "render":
        return judge_render(target, manifest)
    j = judge_context(target, manifest)
    if args.mode == "request" and not (args.groups or args.stale):
        return judge_cluster_request(args, j)
    if args.mode == "request":
        return judge_group_requests(args, target, manifest, j)
    return judge_consume(args, target, manifest, j)


def judge_render(target, manifest):
    # the conflicts doc is a rendering of the judgments — regenerable on demand, so it is not committed
    doc = load_json(os.path.join(target, JUDGMENTS))
    if not doc:
        raise SystemExit("no %s — nothing to render" % JUDGMENTS)
    write_conflicts_doc(target, doc, manifest)
    print("%s rendered from %s (a derived document — read it, do not commit it)" % (CONFLICTS_DOC, JUDGMENTS))
    return 0


def judge_context(target, manifest):
    """What every judge stage reads: the survey, the locked skills, the modes and their injected text, the roles' prompts."""
    s = survey(target)
    skills = [sk for sk in s["skills"] if in_manifest(manifest, sk, s)]
    modes = [h for h in s["hooks"] if h["event"] == "SessionStart" and h["source"].startswith("plugin:")
             and h["source"][7:] in manifest["plugins"]]
    ident = lambda sk: "%s:%s" % (sk["plugin"], sk["name"])
    # what the judge reads: a mode by the text it injects (the host runs its command; so does hunsu), a role by the prompt its
    # worker is sent (`--prompt-only`) — never by a script's name. A worker inherits the project's modes, so a mode that says
    # "ask the user" and a role that says "ask no one" is a contradiction the judge can only see with both texts in front of it.
    mode_packets = [{"source": h["source"], "text": mode_text(h, s)[:6000], "note": "injected at SessionStart — a member of every situation"} for h in modes]
    roles = role_prompts(manifest, s)
    return {"s": s, "skills": skills, "modes": modes, "ident": ident, "mode_packets": mode_packets, "roles": roles}


def judge_cluster_request(args, j):
    """Stage 1 packet: names and descriptions; the judge groups skills (and roles' prompts) by the situation they claim."""
    s, skills, modes, ident, mode_packets, roles = j["s"], j["skills"], j["modes"], j["ident"], j["mode_packets"], j["roles"]
    os.makedirs(args.out, exist_ok=True)
    packet = {"artifact-type": "hunsu/judge-request@1", "stage": "cluster", "target": s["target"],
              "skills": [{"id": ident(sk), "description": sk["description"], "path": sk["path"]} for sk in skills]
                        + [{"id": r["id"], "description": r["description"], "path": r["path"]} for r in roles.values()],
              "modes": mode_packets,
              "instructions": ("Group these skills by the situation they claim to handle, using each description's "
                               "'use when' clause and, if needed, the SKILL.md at `path` (Read). A skill may be in several groups. "
                               "A `plugin:role` entry is a worker's prompt, not a skill a session invokes: it belongs to the situation "
                               "its description names (building, reviewing...). Every mode is implicitly in every group; do not list "
                               "modes as members. Give each group a short situation label in the project's language of work (e.g. "
                               "'reviewing a change'). Only group what the text supports; a group with one member is fine and means "
                               "no overlap there. Change no files.")}
    save_json(os.path.join(args.out, "cluster-request.json"), packet)
    print("stage 1 packet -> %s (%d skills, %d roles, %d modes). Run the judge, then `judge request --groups <its response>`"
          % (os.path.join(args.out, "cluster-request.json"), len(skills), len(roles), len(modes)))
    return 0


def judge_group_requests(args, target, manifest, j):
    """Stage 2 packets: one per group with the members' full text — or, with --stale, only the situations whose members' text
    changed since they were judged."""
    s, skills, modes, ident, mode_packets, roles = j["s"], j["skills"], j["modes"], j["ident"], j["mode_packets"], j["roles"]
    if args.stale:
        j, stale = judgments_status(manifest, s, target)
        if not j:
            raise SystemExit("no %s — nothing is stale; start with `judge request --out DIR`" % JUDGMENTS)
        if stale.get("unclustered"):
            raise SystemExit("new skills no situation has seen (%s) — a cluster round is needed: `judge request --out DIR`" % ", ".join(stale["unclustered"]))
        groups = [{"situation": r["situation"], "members": r["members"]} for r in j["situations"] if r["situation"] in stale.get("situations", [])]
        if not groups:
            print("nothing stale")
            return 0
    else:
        groups = load_json(args.groups).get("groups", [])
    if not groups:
        raise SystemExit("%s has no groups" % args.groups)
    by_id = {ident(sk): sk for sk in skills}
    by_id.update(roles)   # a role's prompt is judged like a skill's text; its members text is already in hand
    prior = {r["situation"]: r.get("findings", []) for r in load_json(os.path.join(target, JUDGMENTS)).get("situations", [])}
    os.makedirs(args.out, exist_ok=True)
    if args.stale:
        for name in os.listdir(args.out):   # a stale round writes only its own packets; older responses must not be consumed again
            if name.startswith("group-") or name.startswith("cluster-"):
                os.remove(os.path.join(args.out, name))
    n = 0
    for i, g in enumerate(groups, 1):
        members = [m for m in g.get("members", []) if m in by_id]
        if len(members) < 2 and not modes:
            continue
        def body(m):
            if "text" in by_id[m]:   # a role: the prompt its worker is sent
                return by_id[m]["text"][:6000]
            path = by_id[m]["path"]
            return (io.open(path, encoding="utf-8").read() if os.path.exists(path) else "")[:6000]
        packet = {"artifact-type": "hunsu/judge-request@1", "stage": "group", "situation": g.get("situation", "group %d" % i),
                  "members": [{"id": m, "description": by_id[m]["description"], "path": by_id[m]["path"], "text": body(m)} for m in members],
                  "modes": [{"source": m["source"], "text": m["text"]} for m in mode_packets],
                  "kinds": {"overlap": "two or more claim the same work in this situation",
                            "contradiction": "one instructs what another forbids, or a mode's standing instruction conflicts with a member "
                                             "(a worker started by a `plugin:role` member receives the modes too)",
                            "premise": "one assumes something another forbids (e.g. artifacts in the repo vs a clean repo)"},
                  "instructions": ("Judge only this situation. For each finding, name the members involved (a mode is named by its source), "
                                   "quote the exact sentence from each member's `text` (a mode's `text` is what it injects into every session) "
                                   "that creates the conflict, say why, and propose one resolution as a JSON object written as a string: {\"use\": id} | {\"deny\": [ids]} | "
                                   "{\"order\": [ids]} | {\"accept\": reason}. No quote, no finding. Similar names are not evidence. Change no files.")}
        res = manifest.get("resolutions", {}).get(packet["situation"])
        if isinstance(res, dict):   # the situation is already resolved: the judge also says whether that resolution still fits what it finds now
            packet["existing_resolution"] = {"resolution": res, **({"prior_findings": prior[packet["situation"]]} if packet["situation"] in prior else {})}
            packet["instructions"] += (" This situation already has a resolution (`existing_resolution`; the findings it was written about are its "
                                       "`prior_findings`). Additionally answer `resolution_verdict`: does that resolution still fit your findings? "
                                       "`verdict` is still-fits | does-not-fit | cannot-tell, with `why` and `quote` — a verbatim sentence from the "
                                       "member texts above grounding the verdict. No verbatim quote, no still-fits.")
        save_json(os.path.join(args.out, "group-%02d-request.json" % i), packet)
        n += 1
    print("stage 2: %d group packets -> %s. Run the judge on each, then `judge consume --dir %s`" % (n, args.out, args.out))
    return 0


def judge_consume(args, target, manifest, j):
    """Every response validated (quotes required, known members, a known kind); the judgments written with fingerprints of
    what was judged; a resolution whose findings changed is kept only on the judge's grounded still-fits, else marked for a person."""
    s, skills, modes, ident, mode_packets, roles = j["s"], j["skills"], j["modes"], j["ident"], j["mode_packets"], j["roles"]
    # consume
    d = args.dir
    cluster = load_json(os.path.join(d, "cluster-response.json"))
    existing = load_json(os.path.join(target, JUDGMENTS))
    if not cluster.get("groups") and not existing:
        raise SystemExit("no cluster-response.json with groups in %s" % d)
    valid_ids = {ident(sk) for sk in skills} | {h["source"] for h in modes} | set(roles)
    results, rejected, verdicts = [], [], {}
    for name in sorted(os.listdir(d)):
        if not (name.startswith("group-") and name.endswith("-response.json")):
            continue
        req = load_json(os.path.join(d, name.replace("-response", "-request")))
        resp = load_json(os.path.join(d, name))
        findings = []
        for f in resp.get("findings", []):
            members = f.get("members", [])
            quotes = f.get("quotes", {})
            if isinstance(quotes, list):   # the schema's shape (a list of {member, quote}: Codex refuses open maps); read either
                quotes = {q.get("member"): q.get("quote") for q in quotes if isinstance(q, dict)}
            bad = (f.get("kind") not in KINDS or len(members) < 2 or any(m not in valid_ids for m in members)
                   or any(not str(quotes.get(m, "")).strip() for m in members) or not f.get("why"))
            if bad:
                rejected.append("%s: %s" % (name, json.dumps(f, ensure_ascii=False)[:160]))
                continue
            proposed = f.get("proposed") or {}
            if isinstance(proposed, str):   # Codex's strict schema carries it as text
                try:
                    proposed = json.loads(proposed) if proposed.strip() else {}
                except ValueError:
                    proposed = {"accept": proposed}
            findings.append({"kind": f["kind"], "members": members, "quotes": quotes, "why": f["why"], "proposed": proposed if isinstance(proposed, dict) else {}})
        members = [m["id"] for m in req.get("members", [])]
        mode_ids = [m["source"] for m in req.get("modes", [])]   # a mode is a member of every situation; its plugin's version counts too
        # what travels for the resolution gate: the judge's verdict on the existing resolution, the texts it could quote from, and who judged
        w = resp.get("worker") or {}
        account = " ".join(str(w[k]) for k in ("host", "model") if w.get(k)) or args.by or "unknown"
        packed = "\n".join([str(m.get("text", "")) for m in req.get("members", [])] + [str(m.get("text", m.get("command", ""))) for m in req.get("modes", [])])
        verdicts[req.get("situation", name)] = (resp.get("resolution_verdict"), packed, account)
        results.append({"situation": req.get("situation", name), "members": members, "modes": mode_ids,
                        "members-fingerprint": members_fingerprint(manifest, members + mode_ids),
                        "members-text-fingerprint": members_text_fingerprint(s, members + mode_ids, manifest),
                        # who judged: the worker's own account (host + model, machine-readable) when the response
                        # carries one; --by is the runner's claim, kept only as the fallback — "default model" tells
                        # a later reader nothing, "claude-code claude-opus-5[1m]" does
                        "judged-by": account, "findings": findings})
    import hashlib
    for r in results:
        r["findings-fingerprint"] = hashlib.sha256(json.dumps([(f["kind"], f["members"], f["quotes"]) for f in r["findings"]], sort_keys=True).encode("utf-8")).hexdigest()[:12]
    # A resolution a human wrote is about the findings they read. When a re-judged situation's findings differ, the judge's
    # own `resolution_verdict` decides: still-fits with a verbatim quote keeps the resolution (a delegation record replaces
    # `reviewed`); anything else — does-not-fit, cannot-tell, no verdict, a quote not in the texts — marks `reviewed: false`
    # for a human. Fail closed: the machine only closes the gate on grounded evidence. dwitbuk lists the unreviewed ones.
    before = {r["situation"]: r.get("findings-fingerprint") for r in existing.get("situations", [])}
    changed = [r["situation"] for r in results if r["situation"] in before and before[r["situation"]] != r["findings-fingerprint"]
               and isinstance(manifest.get("resolutions", {}).get(r["situation"]), dict)]
    unread, delegated = [], []
    for sit in changed:
        verdict, packed, account = verdicts.get(sit) or (None, "", args.by)
        v = verdict if isinstance(verdict, dict) else {}
        quote = str(v.get("quote") or "")
        if v.get("verdict") == "still-fits" and quote.strip() and quote in packed:
            manifest["resolutions"][sit]["reviewed"] = {"delegated": "judge %s: %s" % (account, str(v.get("why") or "")[:200])}
            delegated.append(sit)
            print("situation %r: findings changed — the judge says the resolution still fits (quote verified); kept, delegation recorded" % sit)
        else:
            why = ("verdict %r" % v.get("verdict")) if v.get("verdict") in ("does-not-fit", "cannot-tell") else \
                  ("quote not found verbatim in the member texts" if v.get("verdict") == "still-fits" else "no resolution_verdict")
            manifest["resolutions"][sit]["reviewed"] = False
            unread.append(sit)
            print("situation %r: findings changed — %s; marked reviewed: false (a human must re-read)" % (sit, why))
    if changed:
        save_json(os.path.join(target, MANIFEST), manifest)
    if not cluster.get("groups"):   # a stale round: these situations replace their old selves; the rest stand
        redone = {r["situation"] for r in results}
        results = [r for r in existing.get("situations", []) if r["situation"] not in redone] + results
        rejected = existing.get("rejected", []) + rejected
    doc = {"artifact-type": "hunsu/judgments@1", "skills-fingerprint": skills_fingerprint(manifest, s), "skills": judged_ids(manifest, s),
           "judged-by": args.by or judged_by(results), "situations": results, "rejected": rejected}
    save_json(os.path.join(target, JUDGMENTS), doc)
    write_conflicts_doc(target, doc, manifest)
    n_find = sum(len(r["findings"]) for r in results)
    print("judgments: %d situations · %d findings · %d rejected (no quote / unknown member / bad kind) -> %s, %s"
          % (len(results), n_find, len(rejected), JUDGMENTS, CONFLICTS_DOC))
    if unread:
        print("findings changed under %d existing resolution(s) — marked reviewed: false until a human re-reads: %s" % (len(unread), "; ".join(unread)))
    if delegated:
        print("findings changed under %d existing resolution(s) — the judge confirmed each still fits; delegation recorded: %s" % (len(delegated), "; ".join(delegated)))
    print("next: for each situation with findings, write resolutions[<situation>] in %s, then `hunsu check`" % MANIFEST)
    return 0


def write_conflicts_doc(target, doc, manifest):
    """The human-readable side of the judgments: what was found, the evidence, and what the judge proposed."""
    res = manifest.get("resolutions", {})
    lines = ["# hunsu — conflicts", "", "Judged by %s for skill set `%s`. A situation with findings needs an entry in `hunsu.json` `resolutions` "
             "keyed by the situation label; `check` fails until it has one." % (doc.get("judged-by", "?"), doc["skills-fingerprint"]), ""]
    for r in doc["situations"]:
        state = "resolved" if r["situation"] in res else ("needs a resolution" if r["findings"] else "no conflict")
        lines += ["## %s — %s" % (r["situation"], state), "", "members: " + ", ".join("`%s`" % m for m in r["members"]), ""]
        for f in r["findings"]:
            lines += ["- **%s** between %s" % (f["kind"], ", ".join("`%s`" % m for m in f["members"])), "  - why: %s" % f["why"]]
            for m, q in f["quotes"].items():
                lines.append("  - `%s`: “%s”" % (m, str(q).strip()))
            if f["proposed"]:
                lines.append("  - proposed: `%s`" % json.dumps(f["proposed"], ensure_ascii=False))
        if r["situation"] in res:
            lines.append("- resolution: `%s`" % json.dumps(res[r["situation"]], ensure_ascii=False))
        lines.append("")
    if doc.get("rejected"):
        lines += ["## rejected by hunsu (no quote, unknown member, or bad kind)", ""] + ["- %s" % x for x in doc["rejected"]] + [""]
    with io.open(os.path.join(target, CONFLICTS_DOC), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def cmd_remove(args):
    path = os.path.join(args.target, MANIFEST)
    manifest = load_json(path)
    if args.plugin not in manifest.get("plugins", {}):
        raise SystemExit("%s is not in %s" % (args.plugin, MANIFEST))
    del manifest["plugins"][args.plugin]
    save_json(path, manifest)
    set_link(args.target, args.plugin, None)
    stale = [k for k, v in manifest.get("resolutions", {}).items()
             if args.plugin in json.dumps(v) or (isinstance(v, str) and v == args.plugin)]
    print("removed %s%s" % (args.plugin, (" — resolutions still mention it: %s (edit them)" % ", ".join(stale)) if stale else ""))
    print("the judged set changed: re-run `hunsu judge` before locking")
    return 0


# ---------------------------------------------------------------- lock

LOCK = "hunsu.lock.json"


def cmd_install(args):
    """Make this machine match the manifest: add each plugin's marketplace, install at project scope, then check.

    The host CLI installs whatever version the marketplace currently has; there is no pin. `check` afterwards
    says whether that matches the manifest — if not, the human either updates the manifest or pins the marketplace.
    """
    import shutil
    import subprocess
    manifest = load_json(os.path.join(args.target, MANIFEST))
    if not manifest:
        raise SystemExit("no %s — nothing to install" % MANIFEST)
    s = survey(args.target)
    linked = links(args.target)
    todo, blocked = [], []
    refresh = set(args.refresh or [])
    if refresh == {"all"}:
        refresh = set(linked)
    for name in sorted(refresh):
        if name not in linked:
            raise SystemExit("plugin %s is not linked here — `--refresh` reinstalls a linked plugin from its link (hunsu.local.json)" % name)
        want = manifest["plugins"].get(name) or {}
        spec = "%s@%s" % (name, want["marketplace"]) if want.get("marketplace") else name
        if HOST == "codex":
            # Codex snapshots a plugin at `plugin add`; remove + add takes a new snapshot of the marketplace's current tree
            codex_exe = shutil.which("codex") or "codex"
            print("  refresh %s: codex plugin remove + add — Codex keeps a snapshot until the plugin is added again" % spec)
            for verb in ("remove", "add"):
                done = subprocess.run([codex_exe, "plugin", verb, spec], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=args.target)
                if done.returncode:
                    print("  failed (%s): %s" % (verb, (done.stderr or done.stdout).strip()[-300:]))
                    blocked.append(name)
                    break
            continue
        print("  refresh %s: uninstall + install (project scope) — the host keeps a same-version plugin as it was installed" % spec)
        for verb in ("uninstall", "install"):
            done = subprocess.run(["claude", "plugin", verb, spec, "--scope", "project"], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=args.target)
            text = (done.stderr or done.stdout).strip()
            if done.returncode and verb == "uninstall" and "not installed" in text:
                print("  (not installed at this project's scope — only enabled; installing)")
                continue
            if done.returncode:
                print("  failed (%s): %s" % (verb, text[-300:]))
                blocked.append(name)
                break
    if refresh:
        s = survey(args.target)
    for name, want in manifest["plugins"].items():
        have = enabled(s, name, want)
        if have:
            if have["version"] != want["version"] and name not in linked:
                print("  drift   %s: host has %s, manifest says %s — the host installs what the marketplace has now; `hunsu check` decides" % (name, have["version"], want["version"]))
            continue
        if shareable(want["source"]):
            todo.append((name, want, want["source"].split(":", 1)[1] if want["source"].startswith("github:") else want["source"]))
        elif name in linked:
            todo.append((name, want, linked[name]))   # a local checkout is this machine's marketplace for an unpublished plugin
        else:
            blocked.append("%s: source %r — nothing to install from; the owner must publish it or you must `hunsu link %s <marketplace dir>`" % (name, want["source"], name))
    matching = len(manifest["plugins"]) - len(todo) - len(blocked)   # present, at the manifest's version or not
    for line in blocked:
        print("  blocked %s" % line)
    ok = 0
    for name, want, repo in todo:
        if HOST == "codex":
            print("  codex plugin add %s@%s (marketplace %s)" % (name, want.get("marketplace"), repo))
            codex_exe = shutil.which("codex") or "codex"
            subprocess.run([codex_exe, "plugin", "marketplace", "add", repo], capture_output=True, text=True, encoding="utf-8", errors="replace")
            done = subprocess.run([codex_exe, "plugin", "add", "%s@%s" % (name, want.get("marketplace"))], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if done.returncode:
                print("  failed: %s" % (done.stderr or done.stdout).strip()[-300:])
                blocked.append(name)
            else:
                ok += 1
            continue
        known = load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json"))
        if want.get("marketplace") not in known:
            print("  marketplace add %s" % repo)
            done = subprocess.run(["claude", "plugin", "marketplace", "add", repo], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if done.returncode:
                print("  failed: %s" % (done.stderr or done.stdout).strip()[-300:])
                blocked.append(name)
                continue
        spec = "%s@%s" % (name, want["marketplace"]) if want.get("marketplace") else name
        print("  install %s (project scope)" % spec)
        done = subprocess.run(["claude", "plugin", "install", spec, "--scope", "project"], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=args.target)
        if done.returncode:
            print("  failed: %s" % (done.stderr or done.stdout).strip()[-300:])
            blocked.append(name)
        else:
            ok += 1
    print("installed %d · blocked %d · already present %d — now `hunsu check` (restart the host for new plugins to load)"
          % (ok, len(blocked), matching))
    return 1 if blocked else 0


def policy_lines(lock):
    """The lock's resolutions as the lines the host injects at SessionStart — the policy materialized where the agent reads,
    one line per situation. In a package manager, resolving is applying; here `deny` is applied by the sentinel and the rest by
    these lines. `reviewed: false` is shown as such, never hidden."""
    out = []
    for key, res in (lock.get("resolutions") or {}).items():
        if isinstance(res, str):
            out.append("%s: %s" % (key, "denied" if res == "deny" else "%s's" % res))
            continue
        parts = []
        if res.get("use"):
            parts.append("use %s" % res["use"])
        if res.get("deny"):
            parts.append("never %s" % ", ".join(res["deny"]))
        if res.get("order"):
            parts.append("%s first, then %s" % (res["order"][0], ", ".join(res["order"][1:])) if len(res["order"]) > 1 else "%s" % res["order"][0])
        if res.get("accept"):
            parts.append("both apply: %s" % res["accept"][:160])
        if res.get("note"):
            parts.append("(%s)" % res["note"][:120])
        line = "%s: %s" % (key, "; ".join(parts) or "no rule")
        if res.get("reviewed") is False:
            line += " [unreviewed — applied from the judge's proposal]"
        elif isinstance(res.get("reviewed"), dict) and res["reviewed"].get("delegated"):
            line += " [re-judged by delegation]"
        out.append(line)
    return out


def cmd_lock(args):
    """Snapshot of the resolved environment. Written only when check has no errors; warnings are recorded, not hidden."""
    dev = load_json(os.path.join(args.target, LOCAL)).get("dev", {})
    if dev:
        # a lock names released content; a working source on one machine is neither released nor the team's
        raise SystemExit("not locked — in development here: %s. The lock names what the team installs; `hunsu dev --off %s` first"
                         % (", ".join("%s (from %s)" % kv for kv in sorted(dev.items())), " ".join(sorted(dev))))
    errors, warnings, _ = check(args.target)
    if errors:
        for line in errors:
            print("  error %s" % line)
        raise SystemExit("not locked — %d error(s); fix them or run `hunsu check`" % len(errors))
    manifest = load_json(os.path.join(args.target, MANIFEST))
    s = survey(args.target)
    linked = links(args.target)
    lock = {"artifact-type": "hunsu/lock@1", "host": HOST,
            "engines": {e: version_of(HOST_EXE.get(e, e)) for e in manifest.get("engines", {})},
            "plugins": {name: {"version": enabled(s, name, want)["version"], "source": want["source"], "marketplace": want.get("marketplace"),
                               "fingerprint": tree_fingerprint(enabled(s, name, want)["path"]) if os.path.isdir(enabled(s, name, want).get("path") or "") else None,
                               **({"linked": True} if name in linked else {})}   # the content this version named when it was locked
                        for name, want in manifest["plugins"].items()},
            "skills": locked_skill_ids(manifest, s),
            "denied": denied_skills(manifest, locked_skill_ids(manifest, s)),
            "skills-fingerprint": skills_fingerprint(manifest, s),
            "hooks": [h for h in s["hooks"] if h["source"] != "user"],
            "roles": {role: locked_role(manifest, s, prov) for role, prov in manifest.get("roles", {}).items()},   # `plugin:role` materialized to the argv the plugin declares; runners spawn argv
            "roles-declared": manifest.get("roles", {}),
            "reporters": manifest.get("reporters", {}),   # name -> argv ({plugin:NAME}, {since}): who reports findings for the reviewer
            "settings": manifest.get("settings", {}),     # the team's switches for its products (e.g. dwitbuk stop-eyes) — hunsu carries, products read their own
            "resolutions": manifest.get("resolutions", {}),
            # where each product keeps its records (`records` in its plugin.json: directories end with `/`, files do not) — so a
            # product that must leave the others' records alone (a runner's touched files, a reviewer's diff, a builder's tree
            # check) reads one list here instead of naming its siblings
            "record-paths": {name: sorted(enabled(s, name, want).get("records") or []) for name, want in manifest["plugins"].items()},
            "judge": "skipped" if manifest.get("judge") == "skip" else ("none" if judgments_status(manifest, s, args.target)[0] is None else "judged"),
            "warnings": warnings}
    save_json(os.path.join(args.target, LOCK), lock)
    print("locked %d plugins · %d skills · %d hooks -> %s (%d warnings recorded)"
          % (len(lock["plugins"]), len(lock["skills"]), len(lock["hooks"]), LOCK, len(warnings)))
    if HOST == "codex":
        materialize_codex_hooks(args.target, manifest, s)
    return 0


def materialize_codex_hooks(target, manifest, s):
    """Codex runs a project's .codex/hooks.json, not a plugin's own hooks. So the lock writes the locked plugins' hooks/codex.json
    there, `${PLUGIN_ROOT}` resolved to each plugin's root on this machine — a machine file, ignored by git, remade by every lock."""
    merged = {}
    for name, want in manifest["plugins"].items():
        p = enabled(s, name, want)
        if not p:
            continue
        declared = load_json(os.path.join(p["path"], "hooks", "codex.json")).get("hooks") or {}
        for event, groups in declared.items():
            for g in groups:
                # the interpreter the command names is resolved to the one this machine has (macOS ships python3 only; some hosts only python)
                text = json.dumps(g).replace("${PLUGIN_ROOT}", p["path"].replace("\\", "/"))
                py = "python3" if shutil.which("python3") else "python"
                text = re.sub(r'(?<=")python3? (?=\\")', py + " ", text)
                merged.setdefault(event, []).append(json.loads(text))
    out = os.path.join(target, ".codex", "hooks.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    save_json(out, {"hooks": merged, "description": "written by hunsu lock from the locked plugins' hooks/codex.json; machine paths — not committed"})   # Codex accepts `description`, refuses unknown keys
    ignore = os.path.join(target, ".gitignore")
    lines = io.open(ignore, encoding="utf-8").read().split("\n") if os.path.exists(ignore) else []
    if ".codex/hooks.json" not in lines:
        io.open(ignore, "a", encoding="utf-8", newline="\n").write(("" if not lines or lines[-1] == "" else "\n") + ".codex/hooks.json\n")
    n = sum(len(v) for v in merged.values())
    print("codex: %d hook group(s) from the locked plugins -> .codex/hooks.json (this machine's; run `codex` with hook trust, or exec with --dangerously-bypass-hook-trust)" % n)


def shareable(source):
    return source.startswith(("github:", "http"))


def cmd_add(args):
    path = os.path.join(args.target, MANIFEST)
    manifest = load_json(path)
    if not manifest:
        raise SystemExit("no %s — run `hunsu init` first" % MANIFEST)
    name, _, market = args.plugin.partition("@")
    s = survey(args.target)
    candidates = {k: p for k, p in s["plugins"].items() if p["name"] == name and (not market or p["marketplace"] == market)}
    if not candidates:
        raise SystemExit("%s is not enabled on this host — install and enable it first, then add" % args.plugin)
    if len(candidates) > 1:
        raise SystemExit("%s is enabled from %s — say which: `hunsu add %s`" % (name, ", ".join(sorted(candidates)), "` or `hunsu add ".join(sorted(candidates))))
    found = next(iter(candidates.values()))
    args.plugin = name
    source = found["source"] if shareable(found["source"]) else "unpublished"
    manifest["plugins"][name] = {"version": found["version"], "source": source, "marketplace": found["marketplace"]}
    if getattr(args, "take_content", None):
        # the version's content changed under its name (a release rewritten, a history erased) and a person takes it as
        # this version's: the reason is the manifest's, committed, and `check` accepts exactly this content — not the next edit
        manifest["plugins"][name]["content-taken"] = {"fingerprint": tree_fingerprint(found["path"]) if os.path.isdir(found.get("path") or "") else None,
                                                      "why": args.take_content, "at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    save_json(path, manifest)
    note = ""
    if source == "unpublished":
        # A local marketplace is this machine's state, not the project's declaration: it goes to the link file.
        set_link(args.target, args.plugin, found["source"])
        note = " — local marketplace, linked in %s; the team cannot install it until it has a shareable source" % LOCAL
    print("added %s %s (%s)%s" % (args.plugin, found["version"], source, note))
    return 0


# ---------------------------------------------------------------- links (this machine only)

LOCAL = "hunsu.local.json"
LOCAL_SETTINGS = ".claude/settings.local.json"   # the host's own machine-local settings: `hunsu dev` enables a working source there


def links(target):
    return load_json(os.path.join(target, LOCAL)).get("links", {})


def linked_source(link, name):
    """A link is a marketplace directory. The plugin's source inside it: what its marketplace.json says, else `<link>/<name>`,
    else the link itself — whichever actually holds a plugin manifest. None when nothing does (then there is nothing to compare)."""
    candidates = []
    for mf in (os.path.join(link, ".claude-plugin", "marketplace.json"), os.path.join(link, ".agents", "plugins", "marketplace.json")):
        for entry in load_json(mf).get("plugins", []):
            if entry.get("name") == name and isinstance(entry.get("source"), str):
                candidates.append(os.path.normpath(os.path.join(link, entry["source"])))
    candidates += [os.path.join(link, name), link]
    for c in candidates:
        if any(os.path.exists(os.path.join(c, m)) for m in (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"), "plugin.json")):
            return c
    return None


def source_version(root):
    """The version a plugin source declares, from whichever manifest copy it has."""
    for m in (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"), "plugin.json"):
        v = load_json(os.path.join(root, m)).get("version")
        if v:
            return v
    return None


def tree_fingerprint(root):
    """Identity of a plugin's content: every file's path and bytes, minus what the interpreter, the OS and the host leave
    behind (Claude Code marks a cached copy it runs with `.in_use`). A version is the name of one content; this is how two
    copies under one name are told apart."""
    import hashlib
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", ".in_use"))   # the host's marker is a file on some versions, a folder of pids on others
        for f in sorted(files):
            if f.endswith(".pyc") or f in (".DS_Store", ".in_use"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
            h.update(rel.encode("utf-8") + b"\0")
            with open(os.path.join(dirpath, f), "rb") as fh:
                h.update(fh.read().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()[:12]


def set_link(target, plugin, path):
    doc = load_json(os.path.join(target, LOCAL)) or {"links": {}}
    if path is None:
        doc["links"].pop(plugin, None)
    else:
        doc["links"][plugin] = path.replace(os.sep, "/")
    save_json(os.path.join(target, LOCAL), doc)


def cmd_dev(args):
    """Run a plugin's working source in this project on this machine only — to try an unreleased change in a project whose
    committed settings install the released one (a public project cannot name a local marketplace). `.claude/settings.local.json`
    (the host's machine-local settings, never committed) enables `<plugin>@<local marketplace>` at local scope and turns the
    manifest's copy off; `hunsu.local.json` remembers it. Nothing committed changes; `lock` refuses until `--off`."""
    import subprocess
    target, name = args.target, args.plugin
    manifest = load_json(os.path.join(target, MANIFEST))
    want = manifest.get("plugins", {}).get(name)
    if not want:
        raise SystemExit("%s is not in %s — `hunsu add` it first; dev replaces a declared plugin, it does not add one" % (name, MANIFEST))
    doc = load_json(os.path.join(target, LOCAL)) or {"links": {}}
    settings_path = os.path.join(target, LOCAL_SETTINGS)
    settings = load_json(settings_path)
    ep = settings.setdefault("enabledPlugins", {})
    run = lambda *a: subprocess.run(["claude", "plugin", *a], cwd=target, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if args.off:
        market = doc.get("dev", {}).pop(name, None)
        if not market:
            raise SystemExit("%s is not in development here" % name)
        run("uninstall", "%s@%s" % (name, market), "--scope", "local")
        ep.pop("%s@%s" % (name, market), None)
        ep.pop("%s@%s" % (name, want["marketplace"]), None)
        if not doc["dev"]:
            doc.pop("dev")
    else:
        markets = load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json"))
        market = args.market or next((m for m, rec in markets.items() if (rec.get("source") or {}).get("source") == "directory"
                                      and os.path.isdir(os.path.join((rec.get("source") or {}).get("path", ""), name))), None)
        if not market or market not in markets:
            raise SystemExit("no local (directory) marketplace with %s is known here — `claude plugin marketplace add <dir>`, or --market NAME" % name)
        done = run("install", "%s@%s" % (name, market), "--scope", "local")
        if done.returncode:
            raise SystemExit("installing %s@%s at local scope failed: %s" % (name, market, (done.stderr or done.stdout).strip()[-300:]))
        settings = load_json(settings_path)   # the host wrote its own entry; keep it and add the switch-off
        ep = settings.setdefault("enabledPlugins", {})
        ep["%s@%s" % (name, market)] = True
        ep["%s@%s" % (name, want["marketplace"])] = False
        doc.setdefault("dev", {})[name] = market
    apply_dev_settings(doc, manifest)
    save_json(settings_path, settings)
    save_json(os.path.join(target, LOCAL), doc)
    ensure_ignored(target, [LOCAL, LOCAL_SETTINGS])
    print(("%s back to %s — the manifest's copy runs here again" % (name, want["marketplace"])) if args.off else
          "%s in development here: %s@%s at local scope (%s, not committed); `lock` refuses until `hunsu dev --off %s`" % (name, name, market, LOCAL_SETTINGS, name))
    return 0


def apply_dev_settings(doc, manifest):
    """While any plugin runs from a working source here, the manifest's `dev-settings` — the team's declaration of what a
    trial changes, like a package's dev dependencies — are this machine's settings overlay (`settings` in hunsu.local.json,
    which products read over hunsu.json's). After the last `dev --off` the values it applied are taken back, unless the
    person changed one since. hunsu names no product: which settings a trial needs is the project's to say."""
    local = doc.get("settings") or {}
    for product, keys in (doc.pop("dev-settings-applied", None) or {}).items():
        for k, v in keys.items():
            if (local.get(product) or {}).get(k) == v:
                local[product].pop(k)
        if product in local and not local[product]:
            local.pop(product)
    want = manifest.get("dev-settings") or {}
    if doc.get("dev") and want:
        for product, keys in want.items():
            local.setdefault(product, {}).update(keys)
        doc["dev-settings-applied"] = json.loads(json.dumps(want))
    if local:
        doc["settings"] = local
    else:
        doc.pop("settings", None)
    return doc


def ensure_ignored(target, entries):
    path = os.path.join(target, ".gitignore")
    lines = io.open(path, encoding="utf-8").read().split("\n") if os.path.exists(path) else []
    missing = [e for e in entries if e not in lines]
    if missing:
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(("" if not lines or lines[-1] == "" else "\n") + "\n".join(missing) + "\n")


def cmd_link(args):
    manifest = load_json(os.path.join(args.target, MANIFEST))
    path = args.path or (enabled(survey(args.target), args.plugin, manifest.get("plugins", {}).get(args.plugin)) or {}).get("source")
    if not path or shareable(path):
        raise SystemExit("%s: no local path to link — give one, or it is not installed from a local marketplace" % args.plugin)
    set_link(args.target, args.plugin, path)
    print("linked %s -> %s (%s, not committed)" % (args.plugin, path, LOCAL))
    return 0


def cmd_unlink(args):
    set_link(args.target, args.plugin, None)
    print("unlinked %s" % args.plugin)
    return 0


# ---------------------------------------------------------------- compose: the whole flow as a state machine

DECISION = 2   # exit code: stopped at a human decision; the last output line is the question


def stop(question, *lines):
    for line in lines:
        print("  " + line)
    print("decision: " + question)
    return DECISION


def cmd_compose(args):
    """init -> add -> engines -> check -> lock, stopping wherever a human must decide. Re-run after each answer.

    Order is enforced here, not in prose: nothing advances past an unanswered gate.
    """
    target = args.target
    path = os.path.join(target, MANIFEST)
    if not os.path.exists(path):
        cmd_init(args)
    manifest = load_json(path)
    s = survey(target)
    if not manifest["plugins"]:
        return stop("which of these plugins does this project use? answer with `hunsu add <plugin>` for each, then re-run compose",
                    *("%-16s %-12s %s" % (n, p["version"], p["source"]) for n, p in s["plugins"].items()))
    needed = sorted({rt.replace(".exe", "").replace("python3", "python") for rt in
                     (runtime_of(h["command"]) for h in s["hooks"]) if rt} & {"python", "node"})
    missing = [rt for rt in needed if rt not in manifest.get("engines", {})]
    if HOST not in manifest.get("engines", {}) or missing:
        found = {rt: version_of(HOST_EXE.get(rt, rt)) for rt in [HOST] + needed}
        floors = {}
        for name, want in manifest["plugins"].items():
            for rt, spec in ((enabled(s, name, want) or {}).get("engines") or {}).items():
                floors.setdefault(rt, []).append("%s %s" % (name, spec))
        for rt in floors:
            found.setdefault(rt, version_of(HOST_EXE.get(rt, rt)))
        absent = [rt for rt in [HOST] + needed if rt not in manifest.get("engines", {})]
        return stop("which floor versions does the team require for %s? write them into hunsu.json `engines` (e.g. %s), then re-run compose"
                    % (", ".join(absent), ", ".join('"%s": ">=%s"' % (rt, ".".join((found.get(rt) or "0.0").split(".")[:2])) for rt in absent)),
                    *("%-12s found here: %s%s" % (rt, v or "not found", "  (plugins need: %s)" % ", ".join(floors[rt]) if rt in floors else "")
                      for rt, v in found.items()))
    errors, warnings, _ = check(target)
    if errors:
        return stop("resolve these errors (resolutions[<situation>]; install missing plugins; raise engines; re-run judge if stale), then re-run compose", *errors)
    judged, _ = judgments_status(manifest, s, target)
    if judged is None and manifest.get("judge") != "skip":
        return stop("no conflict judgment yet: run `hunsu judge request --out DIR`, the judge on each packet (judge_worker.py), `hunsu judge consume --dir DIR`, then resolutions — or set \"judge\": \"skip\" in hunsu.json to lock without one (recorded)")
    user_hooks = [w for w in warnings if w.startswith("user ")]
    if user_hooks:
        # Acknowledged = listed in local-hooks-ok. One mechanism; no separate "reviewed" flag.
        return stop("user-level hooks run here and are not the project's. Tell the person first, one line per hook below, and ask whether they want "
                    "one looked into — if so, read the file it runs on this machine and say what it takes in, where it sends it, and whether it can "
                    "change the session. Then ask where it belongs: `local-hooks-ok` in hunsu.local.json (only theirs, not committed), in hunsu.json "
                    "(the whole team runs it), or removed from their settings — then re-run compose",
                    *user_hooks)
    return cmd_lock(args)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hunsu", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("survey", "init", "add", "remove", "check", "link", "unlink", "dev", "lock", "compose", "install", "judge"):
        p = sub.add_parser(name)
        if name == "check":
            p.add_argument("--findings", action="store_true", help="print drift and unread resolutions as dwitbuk/findings@1 JSON")
        p.add_argument("--target", default=".")
        if name == "survey":
            p.add_argument("--json", action="store_true")
        if name in ("add", "remove", "link", "unlink", "dev"):
            p.add_argument("plugin")
        if name == "dev":
            p.add_argument("--market", default=None, help="the local (directory) marketplace holding the working source; default: the one that has the plugin")
            p.add_argument("--off", action="store_true", help="back to the manifest's copy")
        if name == "add":
            p.add_argument("--take-content", default=None, metavar="WHY", help="the version's content changed under its name (a rewritten release): take this content as the version's, with the reason — recorded in hunsu.json")
        if name == "link":
            p.add_argument("path", nargs="?", default=None)
        if name == "install":
            p.add_argument("--refresh", nargs="+", metavar="PLUGIN", help="reinstall these linked plugins (or `all`) from their link — the host keeps a same-version plugin as first installed")
        if name == "judge":
            p.add_argument("mode", choices=["request", "consume", "render"])
            p.add_argument("--out", default="hunsu-judge", help="request: folder for packets")
            p.add_argument("--groups", default=None, help="request: the stage-1 response -> stage-2 packets")
            p.add_argument("--stale", action="store_true", help="request: stage-2 packets for the situations whose members' text changed, from the judgments file")
            p.add_argument("--dir", default="hunsu-judge", help="consume: folder holding the responses")
            p.add_argument("--by", default=None, help="consume: who judged — default: what the workers' own `worker` records say (host and model)")
    args = parser.parse_args(argv)
    return {"survey": cmd_survey, "init": cmd_init, "add": cmd_add, "check": cmd_check,
            "link": cmd_link, "unlink": cmd_unlink, "dev": cmd_dev, "lock": cmd_lock, "compose": cmd_compose,
            "install": cmd_install, "judge": cmd_judge, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
