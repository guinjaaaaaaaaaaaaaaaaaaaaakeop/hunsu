"""hunsu — locks a project's agent environment, the way a package manager locks dependencies.

  survey  [--target DIR] [--json]   everything that runs for this project on this host: plugins, skills, hooks, modes
  init    [--target DIR]            write an empty hunsu.json
  add     <plugin> [--target DIR]   record a plugin (version, source) from the survey into hunsu.json
  check   [--target DIR]            manifest vs this machine: plugins, engines, hooks, skill overlaps, undecided capabilities. exit 1 on errors
  policy  [--target DIR]            read-only: the resolutions by situation — the hook's line, each one's reason and who settled it
  role    add NAME@CAP[+CAP] --from ROLE --host claude|codex [--model M] [--effort E] [--force]
                                    a capability alternate: ROLE's provider on another host, into hunsu.json `roles` (model/effort dropped unless given)
  role    list                      read-only: the roles and their alternates
  capability CAP.. --session | --alternates [--host claude [--model M] [--effort E]]
                                    the project's answer, once: work a hired sandbox cannot do (loopback, network) is the session's, or an
                                    alternate's — with --host, `<role>@<caps>` added for every worker role on a host lacking them
  link    <plugin> [PATH]           this machine only: the plugin comes from a local checkout (hunsu.local.json, not committed)
  unlink  <plugin>
  lock    [--target DIR]            write hunsu.lock.json from the resolved environment — only when check has no errors
  compose [--target DIR]            the whole flow, stopping at each human decision (exit 2, last line = the question). Re-run after answering
  outdated [--target DIR]           read-only, over the network: what each locked plugin's marketplace publishes now vs the lock and the install, with the update steps
  install [--target DIR] [--refresh P..] make this machine match hunsu.json: marketplace add + plugin install (project scope), then check; --refresh reinstalls linked plugins
  remove  <plugin>                  drop a plugin from the manifest (and its link); resolutions that name it are reported
  judge   request|consume           conflict judgment by a host-run judge: request packs (stage 1: cluster by situation, stage 2: per group), consume validates and writes hunsu-judgments.json + hunsu-conflicts.md

The manifest (hunsu.json) is committed with the project; it is the definition of "the same environment".
Host: Claude Code first. Other hosts attach when their packaging exists.
"""
import argparse
import contextlib
import functools
import io
import importlib.util
import json
import os
import re
import shutil
import shlex
import sys
import tempfile
import threading

DISPATCH_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "dispatch.py")
@functools.lru_cache(maxsize=1)
def load_dispatch():
    """Load the standalone dispatcher only for commands that need it."""
    spec = importlib.util.spec_from_file_location("hunsu_dispatch", DISPATCH_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatched_command(command):
    """(dispatcher, key, argv) for a project dispatcher command, else None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) >= 4 and words[1].replace("\\", "/").endswith("/.codex/hunsu-dispatch.py"):
        return words[1], words[2], words[3:]
    return None




HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("HUNSU_CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")   # the host honors CLAUDE_CONFIG_DIR; HUNSU_CLAUDE_DIR is for tests
CODEX = os.environ.get("HUNSU_CODEX_DIR") or os.environ.get("AGENT_CODEX_HOME") or os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
HOST = "codex" if os.environ.get("AGENT_HOST") == "codex" else "claude-code"   # which host this machine's survey reads; a runner on Codex sets AGENT_HOST
HOST_EXE = {"claude-code": "claude", "codex": "codex"}   # engine name in the manifest -> the executable whose --version is the floor
MANIFEST = "hunsu.json"
TEXT_FORM = "whitespace-normalized"


def load_json(path):
    if not os.path.exists(path):
        return {}
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    """Publish a complete document; a failed write leaves the previous document readable."""
    path = os.path.realpath(path)
    fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            if os.path.exists(path):
                os.chmod(temporary, os.stat(path).st_mode & 0o777)
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ScratchUnavailable(Exception):
    """Scratch setup failed; never expose the OS error's machine paths."""


@contextlib.contextmanager
def scratch_space(needed):
    try:
        yield
    except OSError:
        _MODE_TEXT.clear()
        _ROLE_PROMPT.clear()
        raise ScratchUnavailable(
            "no writable temporary directory for %s; set TMPDIR to a writable directory "
            "or use a sandbox that allows writing there (Codex workspace-write)" % needed
        ) from None


def scratch_directory(prefix, needed):
    with scratch_space(needed):
        return tempfile.mkdtemp(prefix=prefix)


_PROJECT_LOCKS = {}
_PROJECT_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCK_DEPTH = threading.local()


@contextlib.contextmanager
def project_mutation(target):
    """Serialize each project's complete read/modify/write, including nested commands.

    The OS releases the lock on process death. Keep its inode in a machine-local temp directory rather than deleting
    it on release: another process may already be waiting on it. Real paths make checkout aliases share one lock.
    """
    import hashlib
    target = os.path.normcase(os.path.realpath(target))
    with _PROJECT_LOCKS_GUARD:
        mutex = _PROJECT_LOCKS.setdefault(target, threading.RLock())
    with mutex:
        held = getattr(_PROJECT_LOCK_DEPTH, "held", None)
        if held is None:
            held = _PROJECT_LOCK_DEPTH.held = set()
        if target in held:
            yield
            return
        with scratch_space("the mutation lock"):
            directory = os.path.join(tempfile.gettempdir(), "hunsu-mutations")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, hashlib.sha256(target.encode("utf-8")).hexdigest() + ".lock")
            lock_file = open(path, "a+b")
        with lock_file as fh:
            if os.name == "nt":
                import msvcrt
                import errno
                import time
                if os.fstat(fh.fileno()).st_size == 0:
                    fh.write(b"\0")
                    fh.flush()
                while True:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as err:
                        if err.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            held.add(target)
            try:
                yield
            finally:
                held.remove(target)
                if os.name == "nt":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def mutates_project(fn):
    @functools.wraps(fn)
    def guarded(first, *args, **kwargs):
        target = first.target if isinstance(first, argparse.Namespace) else first
        with project_mutation(target):
            return fn(first, *args, **kwargs)
    return guarded


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
    try:
        return _survey_codex(target)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SystemExit("Codex's registry does not read here: %s" % exc) from None


def _survey_codex(target):
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
        versions = by_version(os.listdir(cache)) if os.path.isdir(cache) else []
        root = os.path.join(cache, versions[-1]) if versions else ""
        manifest = load_json(os.path.join(root, ".codex-plugin", "plugin.json")) or load_json(os.path.join(root, "plugin.json"))
        m = markets.get(market) or {}
        src = re.sub(r"^//\?/", "", str(m.get("source", "")).replace("\\", "/"))   # Codex writes Windows paths as \\?\\C:\\... — a link is C:/...
        source = src if m.get("source_type") == "local" else ("github:" + src.split("github.com/")[-1].removesuffix(".git") if "github.com" in src else src or "unknown")
        plugins[key] = {"name": name, "version": manifest.get("version") or (versions[-1] if versions else None), "source": source, "marketplace": market,
                        "path": root.replace(os.sep, "/"), "engines": manifest.get("engines") or {}, "roles": manifest.get("roles") or {}, "records": manifest.get("records") or [],
                        "reads": manifest.get("reads") or {}}
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
    proj_hooks = codex_project_hooks(target, plugins)
    hooks += hook_entries(proj_hooks.get("hooks"), "project")
    user_hooks = {k: v for k, v in (conf.get("hooks") or {}).items() if k != "state"}
    hooks += hook_entries(user_hooks, "user")
    modes = sorted({h["source"] for h in hooks if h["event"] == "SessionStart"})
    return {"host": "codex", "target": os.path.abspath(target).replace(os.sep, "/"), "plugins": plugins, "skills": skills, "hooks": hooks, "modes": modes}


def project_root(start):
    """The project a hook's directory belongs to: the nearest directory at or above `start` holding hunsu.json or
    hunsu.lock.json, else `start`. The host's payload `cwd` follows the shell: sagoforce's session `cd engine` (a repository
    inside the workspace), and the prompt hook read no lock there, took another marketplace's hunsu and said "1.15.0 is
    installed for this project" — the sentinel and the stray-path guard read no lock there either."""
    p = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(p, MANIFEST)) or os.path.exists(os.path.join(p, "hunsu.lock.json")):
            return p
        up = os.path.dirname(p)
        if up == p:
            return os.path.abspath(start)
        p = up


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
                        "engines": manifest.get("engines") or {}, "roles": manifest.get("roles") or {}, "records": manifest.get("records") or [],
                        "reads": manifest.get("reads") or {}}
        skills_dir = os.path.join(root, "skills")
        if os.path.isdir(skills_dir):
            for d in sorted(os.listdir(skills_dir)):
                sk = os.path.join(skills_dir, d, "SKILL.md")
                if os.path.exists(sk):
                    fm = frontmatter(sk)
                    skills.append({"name": fm.get("name", d), "plugin": name, "marketplace": market, "description": fm.get("description", ""), "path": sk.replace(os.sep, "/")})
        hooks += hook_entries(plugin_hooks(root, manifest), "plugin:" + name)

    # the person's own skills (~/.claude/skills): the host offers them in every project, called without a plugin prefix — the
    # sentinel read "no prefix" as a host built-in and let them through, and nothing else saw them
    user_skills = os.path.join(CLAUDE, "skills")
    if os.path.isdir(user_skills):
        for d in sorted(os.listdir(user_skills)):
            sk = os.path.join(user_skills, d, "SKILL.md")
            if os.path.isfile(sk):
                fm = frontmatter(sk)
                skills.append({"name": fm.get("name", d), "plugin": "user", "description": fm.get("description", ""), "path": sk.replace(os.sep, "/")})
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


@mutates_project
def cmd_init(args):
    path = os.path.join(args.target, MANIFEST)
    if os.path.exists(path):
        raise SystemExit("%s exists — not overwriting" % path)
    # no `hosts` list: it was written here and read by nothing — the host a lock was taken on is the lock's `host` line
    save_json(path, {"engines": {}, "plugins": {}, "roles": {}, "reporters": {}, "resolutions": {}, "local-hooks-ok": []})
    # The link file is this machine's state. Committed by accident, it breaks every other machine.
    # The conflicts doc is a rendering of hunsu-judgments.json (regenerate: `judge render`); worker transcripts are
    # session prose no one reads back — the judgment, quotes and worker account already live in the response JSON.
    ignore = os.path.join(args.target, ".gitignore")
    lines = io.open(ignore, encoding="utf-8").read().split("\n") if os.path.exists(ignore) else []
    missing = [e for e in (LOCAL, LOCAL_SETTINGS, CONFLICTS_DOC, "*-response.transcript.jsonl") if e not in lines]
    if missing:
        with io.open(ignore, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(("" if not lines or lines[-1] == "" else "\n") + "\n".join(missing) + "\n")
    print("wrote %s, %s ignored. Add plugins with `hunsu add <plugin>`." % (path, LOCAL))
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


def role_argv(manifest, s, provider, host=None):
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
    host = host or {"claude-code": "claude"}.get(HOST, HOST)
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
    check_host_profile(target, found)
    check_behind(target, manifest, s, found)
    check_capabilities(manifest, found)   # hunsu.json and the host table only: asked even while plugins are missing
    check_codex_hooks(target, manifest, s, found)   # a plugin update is when a generated hook's copy goes, and when plugins differ
    if check_plugins(target, manifest, s, found):
        return found
    check_session(target, manifest, s, found)
    check_engines(target, manifest, s, found)
    check_hooks(target, manifest, s, found)
    check_local_skills(target, manifest, s, found)
    check_skill_overlap(target, manifest, s, found)
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


BEHIND = "is behind:"   # the phrase the SessionStart hook counts


def _semver(v):
    """(1, 13, 0) for "1.13.0"; None for anything else — an unknown format is skipped, never guessed at."""
    return tuple(int(x) for x in v.split(".")) if isinstance(v, str) and re.fullmatch(r"\d+(\.\d+){0,3}", v) else None


def marketplace_version(market, name):
    """The version a marketplace offers for `name`, read from this machine's copy of it — no network: a git-backed
    marketplace's copy is only as fresh as its last `claude plugin marketplace update`. The plugin's own manifest inside the
    copy wins over the listing's `version` (the listing is written by hand and drifts). None when there is no copy."""
    loc = (market or {}).get("installLocation")
    if not loc or not os.path.isdir(loc):
        return None
    root = linked_source(loc, name)
    v = source_version(root) if root else None
    if not v:
        entry = catalog_entry(lambda cat: load_json(os.path.join(loc, *cat.split("/"))), name) or {}
        v = entry.get("version")
    return v


def install_scope(target, name, market):
    """The scope this project's install of `name@market` was made at (the host's record) — `project` when there is none:
    `hunsu install` installs at project scope."""
    if HOST == "codex":
        return None
    entries = load_json(os.path.join(CLAUDE, "plugins", "installed_plugins.json")).get("plugins", {}).get("%s@%s" % (name, market)) or []
    return install_entry(entries, target).get("scope") or "project"


def update_steps(name, market, goal, installed=None, pinned=None, locked=None, copy=None, scope="project", linked=False):
    """The steps, in order, that bring this project to `goal` of `name` — written out so nobody greps old transcripts for them
    (guin-site, 10-13: twice). `installed` is what this host has installed for the project, `pinned` hunsu.json's version,
    `locked` the lock's, `copy` the version this machine's copy of the marketplace has (None when unknown), `linked` whether this
    machine links the plugin to a development source (hunsu.local.json). Each step only
    when it is owed: an install already at `goal` needs no update, a manifest already at it no `upgrade`. The session restarts
    whenever the plugins it runs may differ from what is installed: a host loads plugins once, at start."""
    steps = []
    if installed != goal:
        if HOST == "codex" and linked:
            # a linked development source is re-snapshotted, not upgraded: remove + add, through `install --refresh`
            steps.append("`hunsu install --refresh %s` — this machine links %s to a development source; the refresh re-snapshots it (remove + add)" % (name, name))
        elif HOST == "codex":
            # the named upgrade refreshes the marketplace's Git snapshot and the plugins installed from it (the 2.0.1 adoption:
            # four upgrades, no `plugin add`, every receipt naming the approved revision, the copies byte-exact): a routine
            # remove + add is not owed. Reinstalling stays for a corrupted copy (`check` says so) and `install --refresh` for a
            # linked source. Either way the generated Codex hooks name the old copy until `hunsu lock` on Codex regenerates them
            steps.append("`codex plugin marketplace upgrade %s` — refreshes this machine's Git snapshot of the marketplace and the plugins installed from it" % market)
            steps.append("verify what Codex now has for %s: its copy in the Codex plugin cache (`plugins/cache/%s/%s/%s/`) exists, the "
                         "`.codex-marketplace-install.json` at its root names the revision the release %s was published from, and its files are that "
                         "release's — an upgrade takes what the marketplace publishes now, which by itself proves no version, and `hunsu check` "
                         "compares content only with what a lock recorded for the same version" % (name, market, name, goal, goal))
        else:
            if copy != goal:
                steps.append("`claude plugin marketplace update %s` — this machine's copy of the marketplace" % market)
            steps.append("`claude plugin update %s@%s --scope %s`, run in this project — it installs what the marketplace has now" % (name, market, scope or "project"))
        steps.append("restart the session — a session keeps the plugins it started with, and a /clear does not reload them")
    elif pinned != goal or locked != goal:
        steps.append("restart the session if it started before %s %s was installed — a /clear does not reload plugins" % (name, goal))
    if pinned != goal:
        if linked:
            steps.append("`hunsu add %s` — hunsu.json takes %s" % (name, goal))
            steps.append("`hunsu check` — situations whose members' text changed are stale: `hunsu judge request --out DIR --stale`, judge those packets, `hunsu judge consume --dir DIR`")
        else:
            steps.append("`hunsu upgrade %s` — hunsu.json takes the installed %s, the generated hooks are rewritten from the installed copies, and it lists the stale situations to judge" % (name, goal))
    if pinned != goal or locked != goal:
        steps.append("`hunsu lock`, and commit hunsu.json, hunsu.lock.json (and hunsu-judgments.json when it changed) inside a run, like any change to the environment")
    return steps


def steps_text(steps):
    return " ".join("%d. %s;" % (i, st) for i, st in enumerate(steps, 1)).rstrip(";")


def check_behind(target, manifest, s, found):
    """0. A pinned plugin older than what is available here. `check` compared the manifest with this machine only, so a
    project pinned a release behind stayed green (guin-site, 2026-09-30: mangsang 1.12.2 and hunsu 1.6.2 pinned, 1.13.0 and
    1.7.0 released). Info, not a warning: taking a release is a decision (`hunsu add`, then lock), not a drift. Sources: the
    local copy of the plugin's marketplace, and a linked checkout (`hunsu link`) when there is one."""
    errors, warnings, info = found
    markets = load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json")) if HOST != "codex" else {}
    linked = development_links(target, manifest)
    for name, want in sorted(manifest.get("plugins", {}).items()):
        pinned = _semver(want.get("version"))
        if pinned is None:
            continue
        offers = []
        mv = marketplace_version(markets.get(want.get("marketplace")), name)
        if _semver(mv) and _semver(mv) > pinned:
            offers.append("the marketplace %s lists %s (local copy)" % (want.get("marketplace"), mv))
        src = linked_source(linked[name], name) if name in linked else None
        lv = source_version(src) if src else None
        if _semver(lv) and _semver(lv) > pinned:
            offers.append("the linked source %s is at %s" % (src.replace(os.path.expanduser("~"), "~"), lv))
        if offers and name in linked:
            info.append("plugin %s %s pinned %s; %s — `hunsu install --refresh %s`, restart the session, `hunsu add %s`, then lock, to take it"
                        % (name, BEHIND, want["version"], "; ".join(offers), name, name))
        elif offers:
            market = want.get("marketplace")
            have = installed_version(target, name, market)
            locked = ((load_json(os.path.join(target, LOCK)).get("plugins") or {}).get(name) or {}).get("version")
            steps = update_steps(name, market, mv, installed=have, pinned=want["version"], locked=locked, copy=mv, scope=install_scope(target, name, market))
            info.append("plugin %s %s pinned %s; %s — to take it: %s" % (name, BEHIND, want["version"], "; ".join(offers), steps_text(steps)))


def check_plugins(target, manifest, s, found):
    """True when a plugin is missing here: the checks that read skills, hooks, roles and judgments wait for it."""
    errors, warnings, info = found
    # 1. manifest vs what is enabled here. A linked plugin is this machine's development copy: version drift is expected.
    linked = development_links(target, manifest)
    all_links = links(target)
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
            market = want.get("marketplace")
            locked = ((load_json(os.path.join(target, LOCK)).get("plugins") or {}).get(name) or {}).get("version")
            newer = _semver(have["version"]) and _semver(want["version"]) and _semver(have["version"]) > _semver(want["version"])
            if newer:   # updated here, not yet taken: the project decides to take it
                steps = update_steps(name, market, have["version"], installed=have["version"], pinned=want["version"], locked=locked)
                how = "to take %s: %s" % (have["version"], steps_text(steps))
            else:       # the manifest is ahead of this machine (pulled from someone who took a release)
                steps = update_steps(name, market, want["version"], installed=have["version"], pinned=want["version"], locked=locked,
                                     copy=marketplace_version(load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json")).get(market), name) if HOST != "codex" else None,
                                     scope=install_scope(target, name, market))
                how = "to install %s: %s (the host installs what the marketplace has now; `hunsu check` again after)" % (want["version"], steps_text(steps))
            errors.append("plugin %s: manifest %s, host %s — %s" % (name, want["version"], have["version"], how))
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
        copy_link = installed_copy_link(name, want.get("marketplace"), all_links[name]) if name in all_links else None
        if copy_link:
            host, cv, root = copy_link
            link = all_links[name]
            if cv != have["version"]:
                destination = os.path.join(CLAUDE if host == "claude-code" else CODEX,
                                           "plugins", "cache", want["marketplace"], name, have["version"]).replace(os.sep, "/")
                if os.path.isdir(destination):
                    advice = "`hunsu link %s %s` moves the link (or `hunsu unlink %s`)" % (name, destination, name)
                else:
                    advice = "install %s %s for %s and `hunsu link %s <its copy>`, or `hunsu unlink %s`" % (name, have["version"], host, name, name)
                warnings.append("plugin %s: linked to %s's installed copy %s (%s), while %s %s is installed for this project — %s"
                                % (name, host, cv, link, name, have["version"], advice))
            else:
                info.append("plugin %s: linked to %s's installed copy %s (%s) — an installed copy, not a development source"
                            % (name, host, cv, link))
        # a linked plugin is this machine's development copy: the installed copy (what the host loads) must be that source —
        # whatever the manifest says about versions, this is about the link and the install
        if name in linked or (copy_link and copy_link[1] == have["version"]):
            src = copy_link[2] if copy_link else linked_source(linked[name], name)
            src_version = source_version(src) if src else None
            if not copy_link and not os.path.isdir(linked[name]):
                warnings.append("plugin %s: linked to %s, which does not exist on this machine — `hunsu link %s <marketplace dir>` (links are per machine)" % (name, linked[name], name))
            elif not src:
                # a catalog naming a directory that holds no plugin (a repository whose plugin/ is missing), or one outside the link:
                # nothing is taken in its place — least of all the development checkout around it — and nothing is compared
                warnings.append("plugin %s: linked to %s, where no source of %s resolves (a catalog entry's directory inside it holding %s's "
                                "manifest — `./plugin` in the repository layout — or, with no entry, `%s/` or the directory itself); nothing is "
                                "compared with the installed copy — `hunsu link %s <marketplace dir>`" % (name, linked[name], name, name, name, name))
            elif src_version and src_version != have["version"]:
                warnings.append("plugin %s: the linked source is at %s, the installed copy at %s — `hunsu install --refresh %s`, then the manifest decides (`hunsu add %s`)"
                                % (name, src_version, have["version"], name, name))
            elif src and have.get("path") and os.path.isdir(have["path"]) and tree_fingerprint(src) != tree_fingerprint(have["path"]):
                if copy_link:
                    errors.append("plugin %s: %s's installed copy %s (%s) differs from the copy this host loads for this project under the same version %s — a version names one content: "
                                  "one of the two copies is not the release; compare each with the release and reinstall the one that differs (`hunsu unlink %s` takes the link out of the comparison)"
                                  % (name, host, cv, link, cv, name))
                else:
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


HOST_ENGINE = {"claude": "claude-code", "claude-code": "claude-code", "codex": "codex"}   # a worker's `--host` -> the `engines` name of its host


def host_profile(target):
    """(hosts, why not) — this machine's hunsu.local.json `hosts`: (None, None) when it does not say; (the hosts, None) for a
    usable profile, a non-empty list of known host names; (None, why) for anything else — a dict, a nested list, null, a
    misspelled name, an empty list. A profile hunsu cannot read is never read as a narrower one: it selects nothing."""
    doc = load_json(os.path.join(target, LOCAL))
    if not isinstance(doc, dict) or "hosts" not in doc:
        return None, None
    raw = doc["hosts"]
    if not isinstance(raw, list):
        return None, "not a list"
    if not raw:
        return None, "an empty list selects no host"
    bad = [h for h in raw if not (isinstance(h, str) and h in HOST_EXE)]
    if bad:
        return None, "not a host hunsu knows: %s" % ", ".join(json.dumps(h) for h in bad)
    return set(raw), None


def machine_hosts(target, manifest):
    """(the hosts this machine runs for the project, written?) — a usable hunsu.local.json `hosts` (this machine's statement,
    never committed), else every host the manifest's `engines` names: the shared mixed configuration is the default, a session's
    host need not be its workers', and a profile that does not read (check_host_profile's error) suppresses no probe."""
    hosts, _ = host_profile(target)
    if hosts is not None:
        return hosts, True
    return {e for e in manifest.get("engines", {}) if e in HOST_EXE}, False


def check_host_profile(target, found):
    errors, warnings, info = found
    # 0b. this machine's host profile: one that does not read is an error, and every host `engines` names is checked meanwhile
    _, why = host_profile(target)
    if why:
        errors.append("%s `hosts`: %s — %s; a profile is a non-empty list of the hosts this machine runs (%s), e.g. [\"codex\"]. Until "
                      "it reads, every host `engines` names is checked, as if it were absent"
                      % (LOCAL, json.dumps(load_json(os.path.join(target, LOCAL)).get("hosts")), why, ", ".join(sorted(HOST_EXE))))


def worker_of(manifest, s, provider):
    """The host (`claude-code`/`codex`) a role's worker runs on, read from the argv a runner would spawn: an argv as written,
    `plugin:role` resolved first to what the plugin declares (`{host}` filled with this host as `lock` does; a fixed `--host`
    stays). None for `session` and `native:` (the host's own subagent runs where the session runs), for a command that is not
    a worker, and for a `plugin:role` that does not resolve (check_roles says why)."""
    if isinstance(provider, list):
        argv = [str(a) for a in provider]
    elif isinstance(provider, str) and provider != "session" and not provider.startswith("native:"):
        argv = role_argv(manifest, s, provider)[0]
    else:
        return None
    return HOST_ENGINE.get(worker_host(argv)) if argv else None


def role_hosts(manifest, s):
    """({host: [plain roles]}, {host: [alternates]}) — whose workers run on each host. A plain role is hired for every task of
    its kind; a `<role>@<cap>` alternate only for a task that requires those capabilities."""
    plain, alternates = {}, {}
    for name, provider in sorted((manifest.get("roles") or {}).items()):
        host = worker_of(manifest, s, provider)
        if host:
            (alternates if ALTERNATE.match(name) else plain).setdefault(host, []).append(name)
    return plain, alternates


def hosts_in_use(target, manifest, s):
    """The hosts that run anything for this project on this machine: its hosts, a plain role's worker host (required whatever
    `hosts` says), an alternate's host that `hosts` does not leave out, and the host this check runs on."""
    selected, written = machine_hosts(target, manifest)
    plain, alternates = role_hosts(manifest, s)
    return selected | set(plain) | (set() if written else set(alternates)) | {HOST}


def required_hosts(manifest, s):
    """The host engines this machine must probe whatever its `hosts` says: a plain role's worker host, and a host an enabled
    plugin's own `engines` (plugin.json) names. An alternate's host is not among them: it is hired only for a task that requires it."""
    plain, _ = role_hosts(manifest, s)
    plugins = {e for name, want in (manifest.get("plugins") or {}).items() for e in ((enabled(s, name, want) or {}).get("engines") or {})}
    return set(plain) | {e for e in plugins if e in HOST_EXE}


def unprobed_hosts(target, manifest, s):
    """The host engines neither `check` nor `lock` runs `--version` for: `engines` names them, a usable hunsu.local.json `hosts`
    leaves them out, and nothing here requires them (required_hosts). Empty without a profile: the shared mixed configuration
    probes every host `engines` names. The lock records them as null — not probed, not missing."""
    selected, written = machine_hosts(target, manifest)
    if not written:
        return set()
    needed = required_hosts(manifest, s)
    return {e for e in manifest.get("engines", {}) if e in HOST_EXE and e not in selected and e not in needed}


def check_session(target, manifest, s, found):
    errors, warnings, info = found
    # 1b. the session this check runs in: inside a Codex sandbox, a nested `codex exec` has failed to start ("failed to initialize
    # in-process app-server client: Operation not permitted"). Only a worker that runs on Codex is a nested `codex exec`: a Claude
    # worker, a plain command, `session` and `native:` are not, so they bring no line. Nothing is probed and nothing is recommended
    # that turns a sandbox off.
    if not os.environ.get("CODEX_SANDBOX"):
        return
    selected, written = machine_hosts(target, manifest)
    plain, alternates = role_hosts(manifest, s)
    names = plain.get("codex", []) + ([] if written and "codex" not in selected else alternates.get("codex", []))
    if names:
        warnings.append("session: sandboxed (CODEX_SANDBOX=%s) — %s run their workers on Codex (a nested `codex exec`), which has failed to start "
                        "inside tested read-only and workspace-write sandboxes (\"failed to initialize in-process app-server client: Operation not "
                        "permitted\"); hunsu does not probe this session. If hiring them fails so, run them from a session whose sandbox lets them "
                        "start, or the team declares `native:` providers for them" % (os.environ["CODEX_SANDBOX"], ", ".join(sorted(names))))


def check_engines(target, manifest, s, found):
    errors, warnings, info = found
    # 2. engines — the floor the team requires, checked against what this machine actually has. A host engine is probed
    # where this machine runs that host (hunsu.local.json `hosts`, else every host `engines` names), a plain role's worker runs
    # on it, or a plugin's own `engines` names it; an alternate never makes its host required (hired only for a task that
    # requires it). One rule with `lock` (unprobed_hosts): a host left out is not run at all
    engines = manifest.get("engines", {})
    selected, written = machine_hosts(target, manifest)
    plain, alternates = role_hosts(manifest, s)
    needed, skip = required_hosts(manifest, s), unprobed_hosts(target, manifest, s)
    seen = {}

    def have(engine):
        if engine not in seen:
            seen[engine] = version_of(HOST_EXE.get(engine, engine))
        return seen[engine]

    hosts_said = ", ".join(sorted(selected)) or "no host"
    for engine, spec in engines.items():
        if engine in skip:
            info.append("engine %s: not probed — %s `hosts` says this machine runs %s, and no plain role's worker or plugin needs %s; "
                        "the lock records it as null (not probed, not missing)" % (engine, LOCAL, hosts_said, engine))
            continue
        version = have(engine)
        if not satisfies(version, spec):
            others = sorted(e for e in engines if e in HOST_EXE and e != engine)
            hint = (" — a machine that runs %s only says so in %s (`\"hosts\": [%s]`, not committed); the team's floor stays in %s"
                    % (", ".join(others), LOCAL, ", ".join('"%s"' % e for e in others), MANIFEST)
                    if engine in HOST_EXE and not version and not written and engine not in needed and others else "")
            errors.append("engine %s: requires %s, found %s%s" % (engine, spec, version or "nothing", hint))
    # a plugin declares its own floor (`engines` in plugin.json, npm's convention); the team's floor must cover it,
    # and this machine must meet it — otherwise the plugin's workers die at call time with a traceback nobody asked for
    for name, want in sorted(manifest["plugins"].items()):
        plugin = enabled(s, name, want)
        for engine, spec in sorted((plugin or {}).get("engines", {}).items()):
            team = engines.get(engine)
            if team is None or not floor_covers(team, spec):
                errors.append("plugin %s: needs %s %s, manifest `engines` %s — raise it" % (name, engine, spec, "requires %s" % team if team else "does not require it"))
            version = have(engine)
            if not satisfies(version, spec):
                errors.append("plugin %s: needs %s %s, found %s here" % (name, engine, spec, version or "nothing"))
    # the roles' workers, before anything hires them: a plain role's host is required here; an alternate's is not
    keep = "hunsu does not move a role to another host, model or provider"
    for host, names in sorted(plain.items()):
        if written and host not in selected:
            errors.append("roles %s: their workers run on %s, which %s `hosts` leaves out on this machine (%s) — a plain role is hired for every "
                          "task of its kind; %s: run %s here and list it in `hosts`, or the team changes these roles in %s"
                          % (", ".join(names), host, LOCAL, hosts_said, keep, host, MANIFEST))
        elif not have(host):
            errors.append("roles %s: their workers run on %s, and `%s` is not found on this machine — hiring them fails here; %s: install %s, "
                          "or the team changes these roles in %s" % (", ".join(names), host, HOST_EXE[host], keep, host, MANIFEST))
        if host not in engines:
            warnings.append("engine %s: the workers of %s run on it, and `engines` does not require it — add a floor (nothing checks its version)"
                            % (host, ", ".join(names)))
    for host, names in sorted(alternates.items()):
        caps = sorted({c for n in names for c in ALTERNATE.match(n).group(2).split("+")})
        if written and host not in selected:
            why = "which %s `hosts` leaves out on this machine (%s)" % (LOCAL, hosts_said)
        elif not have(host):
            why = "and `%s` is not found on this machine" % HOST_EXE[host]
        else:
            continue
        warnings.append("alternates %s: their workers run on %s, %s — a task that requires %s cannot run here until the project is reconfigured "
                        "explicitly; the alternates stay as declared (argv, model) — removing them would send such a task to the plain role instead"
                        % (", ".join(names), host, why, " or ".join(caps)))
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
            warnings.append("user hook %s: runs for this project on %d event(s) (%s) and is not the project's%s%s — read the file it runs: one that adds "
                            "nothing to the session (prints nothing or `{}`: no context, no permission decision) is this machine's own tool — list it in "
                            "local-hooks-ok in hunsu.local.json (not committed) and tell the person in a line; ask them only about one that can change the "
                            "session or that the team runs (hunsu.json), or to remove it from their settings"
                            % (name, len(g["events"]), ", ".join(g["events"]), "; on this machine it runs " + ", ".join(g["runs"]) if g["runs"] else "",
                               "; SessionStart among them, so it can add instructions to every session" if "SessionStart" in g["events"] else ""))
    # a plugin enabled on this host but not in the manifest: its skills are refused (not in the lock), its hooks are not — they
    # run for this project like a user hook, and a SessionStart one adds instructions to every session nobody judged
    outside = {}
    for h in s["hooks"]:
        name = h["source"][7:] if h["source"].startswith("plugin:") else None
        if name and name not in manifest.get("plugins", {}) and name not in (s.get("dev") or {}) and h["source"] not in ok \
                and not any(tag in h["command"] for tag in ok):
            outside.setdefault(name, []).append(h["event"])
    for name, events in sorted(outside.items()):
        events = sorted(set(events))
        warnings.append("unmanaged plugin %s: enabled here but not in hunsu.json, and its hooks run for this project on %s%s — add it (`hunsu add %s`), "
                        "disable it for this project, or list `plugin:%s` in local-hooks-ok"
                        % (name, ", ".join(events), "; SessionStart among them, so it adds instructions to every session and no judge has read them" if "SessionStart" in events else "", name, name))
    for h in s["hooks"]:
        if re.search(r"[A-Za-z]:\\|/Users/|/home/", h["command"]) and "${CLAUDE_PLUGIN_ROOT}" not in h["command"]:
            info.append("hook %s (%s): command has an absolute path — bound to this machine" % (h["event"], h["source"]))


def check_local_skills(target, manifest, s, found):
    """What the host brings into this project that is nobody's decision in it: the person's own skills (~/.claude/skills) and
    their own CLAUDE.md. A skill is refused at call time until it is listed in `local-skills-ok` — hunsu.local.json when it is
    only theirs, hunsu.json when the team uses it — the same mechanism as a user hook. The person's CLAUDE.md is said, not
    judged: its text is theirs and would otherwise be quoted into a committed record."""
    errors, warnings, info = found
    ok = set(manifest.get("local-skills-ok", [])) | set(load_json(os.path.join(target, LOCAL)).get("local-skills-ok", []))
    project = {sk["name"] for sk in s["skills"] if sk["plugin"] == "project"}
    for sk in s["skills"]:
        if sk["plugin"] != "user" or sk["name"] in ok:
            continue
        if sk["name"] in project:
            info.append("user skill %s: the project has its own skill of that name; the project's is the one this project means" % sk["name"])
            continue
        warnings.append("user skill %s: in %s, offered to every session here as `%s` and not the project's — the sentinel refuses it; tell the person, "
                        "then list it in local-skills-ok (hunsu.local.json when it is only theirs, hunsu.json when the team uses it) or leave it refused"
                        % (sk["name"], os.path.dirname(sk["path"]).replace(os.path.expanduser("~"), "~"), sk["name"]))
    if os.path.isfile(os.path.join(CLAUDE, "CLAUDE.md")):
        info.append("the person's own CLAUDE.md (%s) is read into every session here; it is theirs and not judged against the project's skills"
                    % os.path.join(CLAUDE, "CLAUDE.md").replace(os.path.expanduser("~"), "~"))


def check_skill_overlap(target, manifest, s, found):
    """4. Two plugins shipping a skill of the same name. Calling is never ambiguous (`plugin:skill`); what can collide is the
    situation — a request both skills claim. That is the judge's question, and a name is a weak proxy for it: `design:review` and
    `code:review` share a name and no situation, `pr-check` and `review` share a situation and no name. So the judge decides:
    a fresh judgment that found no conflict between them needs no resolution; one that did is `check_judgments`' error. Only
    a project locked without a judge (`"judge": "skip"`) has nothing else to ask, and there the name is an error, as before."""
    errors, warnings, info = found
    owners = {}
    for sk in s["skills"]:
        if in_manifest(manifest, sk, s):
            owners.setdefault(sk["name"], []).append(sk["plugin"])
    overlaps = {name: plugins for name, plugins in sorted(owners.items()) if len(plugins) > 1 and name not in manifest.get("resolutions", {})}
    if not overlaps:
        return
    how = "set resolutions[%r] to one of them, \"deny\", or {\"accept\": \"why both apply\"} when they serve different situations"
    if manifest.get("judge") == "skip":
        for name, plugins in overlaps.items():
            errors.append(("skill %s: provided by %s, and this project locks without a judge — " + how) % (name, ", ".join(plugins), name))
        return
    judged, stale = judgments_status(manifest, s, target)
    for name, plugins in overlaps.items():
        ids = {"%s:%s" % (p, name) for p in plugins}
        if judged is None or stale:
            info.append(("skill %s: provided by %s — the judge round decides whether they claim the same situation (or " + how + ")") % (name, ", ".join(plugins), name))
            continue
        together = [r["situation"] for r in judged.get("situations", []) for f in r.get("findings", []) if len(ids & set(f.get("members", []))) > 1]
        if not together:
            info.append("skill %s: provided by %s — judged, no conflict between them; both stay" % (name, ", ".join(plugins)))
        # else: a finding names both — check_judgments asks for that situation's resolution


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
    # an argv names its program in a plugin's copy (`{plugin:hacheong}/worker.py`): a file the copy enabled here does not have
    # fails the hire — an error for a plain role, a warning for an alternate (hired only for a task that requires it)
    for role, provider in sorted(manifest.get("roles", {}).items()):
        for token in (provider if isinstance(provider, list) else []):   # an argv written here; a `plugin:role`'s is the plugin's own packaging
            m = re.match(r"^\{plugin:([^{}/]+)\}/([^{}]+)$", str(token))
            want = manifest["plugins"].get(m.group(1)) if m else None
            copy = (enabled(s, m.group(1), want) or {}) if want else {}
            if copy and os.path.isdir(copy.get("path") or "") and not os.path.exists(os.path.join(copy["path"], m.group(2))):
                (warnings if ALTERNATE.match(role) else errors).append(
                    "role %s: its argv runs %s, and %s %s here has no %s — hiring it fails on this machine"
                    % (role, token, m.group(1), copy.get("version"), m.group(2)))
    plugins = manifest.get("plugins", {})
    if "chongdae" in plugins and manifest.get("roles") and "reviewer" not in manifest["roles"]:
        # chongdae said it at each of a day's ten closes and nothing here did (sagoforce, 10-26): the lock is where it is decided
        info.append("no `reviewer` role: chongdae ends every run with its record unread, a non-claim in each run — declare one in roles%s "
                    "and `hunsu lock`, or leave it so" % (" (dwitbuk's: \"reviewer\": [\"python3\", \"{plugin:dwitbuk}/dwitbuk.py\", \"review\", "
                                                           "\"--target\", \".\"])" if "dwitbuk" in plugins else ""))


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
        res = manifest.get("resolutions", {})
        # a finding repeated across situations (the same members, the same sentences) is one conflict: a resolution of any
        # situation that holds it covers the others — a person wrote the same answer ten times before this
        covered = {finding_signature(f) for r in judged.get("situations", []) if r["situation"] in res for f in r["findings"]}
        settled = 0
        for r in judged.get("situations", []):
            if r["situation"] in res:
                continue
            open_ = [f for f in r["findings"] if finding_class(f) == "authority" and finding_signature(f) not in covered]
            settled += sum(finding_class(f) in SETTLED for f in r["findings"])
            if open_:
                errors.append("situation %r: %d finding(s) for a person, no resolution — see %s" % (r["situation"], len(open_), CONFLICTS_DOC))
        bound = state_bound_situations(judged)
        if bound:   # never rewritten here: a quote is the judge's, and only a judge round replaces it
            warnings.append("%d situation(s) hold findings that quote one state of a mode, read in a live session — true of that session, "
                            "not of the mode (%s); re-judge them on the mode's standing text: `hunsu judge request --out DIR --stale`, "
                            "judge those packets, `consume`"
                            % (len(bound), "; ".join("%s: %s %s" % (k, v[0][0], v[0][1]) for k, v in bound.items())))
        if judged.get("rejected"):
            warnings.append("%d judge finding(s) were rejected (no quote, a quote not in the text, a mode quote naming one state, an unknown member or kind) — a real conflict may be among them; read them in %s" % (len(judged["rejected"]), CONFLICTS_DOC))
        if settled:
            info.append("%d finding(s) settled by the members' own text (sequenced / not co-active / duplicate, each on a verified sentence) — no resolution needed; see %s" % (settled, CONFLICTS_DOC))
        known = {r["situation"] for r in judged.get("situations", [])} | {sid.split(":", 1)[1] for sid in judged_ids(manifest, s)}
        for key in manifest.get("resolutions", {}):   # a cluster round names situations anew; a resolution about an old name decides nothing
            if key not in known:
                warnings.append("resolutions[%r] is about a situation no judgment has (and no skill is named so) — a cluster round renamed it; move or remove it" % key)
    for r in (judged or {}).get("situations", []):
        v = r.get("resolution-verdict") or {}
        entry = manifest.get("resolutions", {}).get(r["situation"])
        if (v.get("verdict") == "does-not-fit" and isinstance(entry, dict)
                and v.get("resolution") == resolution_fingerprint(entry)):
            warnings.append("situation %r: judge %s called the resolution no longer fitting its findings: %s — a person "
                            "re-reads it in %s and changes the resolution or records their own review in it"
                            % (r["situation"], v.get("judged-by", "unknown"), v.get("why", ""), CONFLICTS_DOC))
    for key, res in manifest.get("resolutions", {}).items():
        rv = res.get("reviewed") if isinstance(res, dict) else None
        bound = state_bound(rv.get("delegated")) if isinstance(rv, dict) else []
        if bound:   # `policy` prints this reason to every later session; the state it names has moved on
            warnings.append("resolutions[%r]: its delegated reason names one state (%s) — `policy` prints it as if it stood; a person "
                            "re-reads it and records `reviewed: {\"by\": NAME}`, or a `--stale` round that finds other findings asks the judge again"
                            % (key, "; ".join(bound)))
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
        findings += [{"kind": "unreviewed", "where": "hunsu.json resolutions[%r]" % k, "text": "applied from the judge's proposal, marked reviewed: false — `hunsu policy` shows the conflict a person judges"}
                     for k, r in manifest.get("resolutions", {}).items() if isinstance(r, dict) and r.get("reviewed") is False]
        print(json.dumps({"artifact-type": "dwitbuk/findings@1", "source": "hunsu", "standing": True, "findings": findings}, ensure_ascii=False, indent=1))
        return 1 if errors else 0
    for label, items in (("error", errors), ("warn", warnings), ("info", info)):
        for line in items:
            print("  %-5s %s" % (label, line))
    if getattr(args, "remote", False):
        cmd_outdated(args)
    else:   # asked "is anything newer published?", a session ran check and then `git fetch` in each marketplace by hand (guin-site, 10-14)
        print("  (offline: a release is known here only from this machine's marketplace copies — `hunsu outdated` asks the marketplaces, read-only)")
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
# How much a finding asks of a person. Only `authority` needs a resolution; the others are settled by the members' own text,
# and each must be grounded by a quote that hunsu finds in that text — else the finding is `authority` (fail closed).
# Measured before this rubric: 10 findings on two walk-through projects, all resolved by a person with accept/order, none with
# deny/use, nearly all one pattern (a mode that refuses writes outside a run × a skill that writes) repeated per situation.
CLASSES = {"authority": "the members diverge on who decides, a person's confirmation, or an action hard to undo (commit, delete, publish, "
                        "install, spend) — a person must resolve it",
           "sequenced": "one member's text already says how it yields or in what order both apply (\"writes go through a run\"); "
                        "class_quote is that sentence",
           "not-co-active": "the two never apply in the same moment (a worker that only runs inside a run vs a mode's no-run branch); "
                            "class_quote is the condition that keeps them apart",
           "duplicate": "following either gives the same result — two ways to do one thing, nothing contradicts; class_quote is "
                        "the sentence showing the second one does the same"}
SETTLED = ("sequenced", "not-co-active", "duplicate")


def quote_norm(t):
    """What a quote is compared as: whitespace collapsed, and a JSON escape undone — a judge writing JSON inside JSON quoted
    `"reviewed": false` as `\\"reviewed\\": false`, and a real finding was rejected for it (measured, 2026-09-28)."""
    return " ".join(str(t).replace('\\"', '"').replace("\\'", "'").split())


def finding_class(f):
    """A finding without a class (judged before the rubric) asks for a resolution, as it always did."""
    return f.get("class") if f.get("class") in CLASSES else "authority"


def finding_signature(f):
    """The same conflict found again in another situation: same kind, same members, same quotes. One resolution covers it."""
    return json.dumps([f.get("kind"), sorted(f.get("members", [])), sorted((m, " ".join(str(q).split())) for m, q in (f.get("quotes") or {}).items())])


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


def resolution_fingerprint(entry):
    """The whole resolution entry, including its review, that a verdict was about."""
    import hashlib
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()[:12]


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


INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")


def instruction_files(target):
    """The project's committed standing instructions — CLAUDE.md (Claude Code), AGENTS.md (Codex): read into every session like
    a mode's injected text, so they are members of every situation, as `file:<path>`."""
    out = []
    for rel in INSTRUCTION_FILES:
        path = os.path.join(target, rel)
        if os.path.isfile(path):
            out.append({"source": "file:" + rel, "path": path.replace(os.sep, "/"), "text": io.open(path, encoding="utf-8").read().replace("\r\n", "\n")})
    return out


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
    """What a SessionStart hook says — the text a judge reads of a mode: its command run the way the host runs it (a synthetic
    payload on stdin, `${CLAUDE_PLUGIN_ROOT}` resolved to the plugin's root, `hookSpecificOutput.additionalContext` read), but
    in an empty project — a fresh temporary directory with no records, no git above it — never in this one. A hook that reports
    the project's state says it of an empty project, so what is judged depends on the plugin's script alone: the same on every
    machine, before and after a run. In the project itself chongdae's line read "run … in progress — 1 task(s) open" one day and
    "no run in progress" the next; six of nine situations were re-judged for it, and the judge's quotes and reasons carried
    the run's id into hunsu-judgments.json and hunsu.json as if it stood (guin-site 10-15, 10239ce). The probe also sets
    HUNSU_SURVEY=1: a hook that runs hunsu itself (hunsu's own does) must not probe again, and any hook may answer its standing
    text under it (jokbo does; hunsu's own does). The command's failure is recorded as its text. Inside a probe this returns the
    command."""
    import subprocess, tempfile
    if os.environ.get("HUNSU_SURVEY"):
        return h["command"]
    root = ""
    if h["source"].startswith("plugin:"):
        p = next((x for x in s["plugins"].values() if x["name"] == h["source"][7:]), None)
        root = (p or {}).get("path", "")
    key = (h["source"], h["command"], root, mode_script_fingerprint(h, s))   # an edited script under one root says something else
    if key in _MODE_TEXT:
        return _MODE_TEXT[key]
    empty = scratch_directory("hunsu-mode-", "rendering a mode's text")
    cmd = h["command"].replace("${CLAUDE_PLUGIN_ROOT}", root).replace("${PLUGIN_ROOT}", root)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": empty})   # no session id: hooks that record sessions skip
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=root, HUNSU_SURVEY="1", GIT_CEILING_DIRECTORIES=os.path.dirname(empty))   # git finds no repository above it
    env.pop("AGENT_WORKER", None)   # a judge round run from a worker would read every hook's silence
    try:
        done = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, cwd=empty, env=env)
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
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    for spelling in sorted({os.path.realpath(empty), empty}, key=len, reverse=True):   # macOS: /private/var/… and /var/…
        text = text.replace(spelling, ".")
    _MODE_TEXT[key] = text = machine_neutral(text, s)
    return text


# What is true of one state of a project and not of the mode that reports it: a run's id, a time, a count. A judge that quotes
# such a sentence writes a fact of one session into a committed record ("names run-20261002-050435-e75b as the open run",
# guin-site 10239ce), and `policy` prints it to every later session as if it stood. A mode's text is read in an empty project
# (mode_text), so a quote of it never needs one; one that has one was read from a live session.
STATE_BOUND = (("a run id", r"\brun-\d{8}-\d{6}-[0-9a-f]{4}\b"),
               ("a timestamp", r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?"),
               ("a count", r"\b\d+ tasks? open\b|\b\d+ (?:[\w-]+ ){0,2}?[\w-]+\(s\)|\b(?:errors|warnings) \d+\b"))


def state_bound(text):
    """What in `text` is only true of one state — ["a run id 'run-…'", …]; [] when nothing is."""
    found = []
    for label, pattern in STATE_BOUND:
        m = re.search(pattern, quote_norm(text or ""))
        if m:
            found.append("%s %r" % (label, m.group(0)))
    return found


def state_bound_quotes(f, modes):
    """A finding's quotes of a mode (a plugin's SessionStart hook among `modes`) that name one state — [(mode, what)…]. A skill,
    a role's prompt and an instruction file are standing text: a run id there is an example, not a state."""
    hooks = [m for m in modes if m.startswith("plugin:")]
    quotes = dict(f.get("quotes") or {})
    cq = f.get("class_quote") if isinstance(f.get("class_quote"), dict) else {}
    pairs = [(m, quotes.get(m)) for m in hooks if m in quotes] + ([(cq.get("member"), cq.get("quote"))] if cq.get("member") in hooks else [])
    return [(m, what) for m, q in pairs for what in state_bound(q)]


def state_bound_situations(judged):
    """Recorded situations whose findings quote a mode's state — {situation: [(mode, what)…]}. Judged before 1.14, a mode was read
    in the project itself; such a quote is true of the session it was read in. `check` says so; `judge request --stale` re-judges them."""
    out = {}
    for r in (judged or {}).get("situations", []):
        bound = [x for f in r.get("findings", []) for x in state_bound_quotes(f, r.get("modes", []))]
        if bound:
            out[r["situation"]] = bound
    return out


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
            tmp = scratch_directory("hunsu-role-", "rendering role prompts")
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
    no text changes no judgment. A mode is judged by what it says in an empty project (mode_text); its identity here is its
    command and the content of the script it runs — cheap to read on every check, where running each hook is not — and only a
    mode whose script moved has its hook run, to see whether what it says moved (modes_read)."""
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
    for f in instruction_files(s["target"]):   # a file: its content is its identity
        texts[f["source"]] = f["text"]
    return {k: "\n".join(sorted(v)) if isinstance(v, list) else v for k, v in texts.items()}


def _hash12(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def fingerprint_text(text, form):
    """The member's words for a fingerprint; judge packets and quote checks keep the text itself."""
    return " ".join(text.split()) if form == TEXT_FORM else text


def member_fingerprints(s, members, manifest=None, *, form="raw"):
    """Each judged member's identity on its own (member_texts, hashed) — so a changed situation can say which member moved."""
    texts = member_texts(s, manifest)
    return {m: _hash12(fingerprint_text(texts.get(m, "<absent>"), form)) for m in sorted(members)}


def modes_read(mode_packets, *, form="raw"):
    """What the judge read of each mode — the text its hook says in an empty project (mode_text), hashed per source (a plugin
    with two SessionStart hooks injects two). A mode's identity is its script, but a script can change in ways that leave its
    text alone (chongdae 1.20.0 changed how its line counts open tasks; the line read the same): then what was judged did not
    change. The text depends on the script alone, never on the project's state: a run opened or closed stales nothing.
    (Judged before 1.14, this hashed the project's live line: the first script change after is one re-judge.)"""
    by = {}
    for m in mode_packets:
        by.setdefault(m.get("source"), []).append(fingerprint_text(str(m.get("text", "")), form))
    return {src: _hash12("\n".join(sorted(texts))) for src, texts in by.items()}


def mode_read_now(s, source, *, form="raw"):
    """The same hash for the text `source`'s hooks inject now — the hooks are run (mode_text), so only asked of a mode whose
    script moved since it was judged. An instruction file is its own text: nothing to run, its identity says it all."""
    hooks = [h for h in s["hooks"] if h["event"] == "SessionStart" and h["source"] == source]
    return _hash12("\n".join(sorted(fingerprint_text(mode_text(h, s), form) for h in hooks))) if hooks else None


def members_text_fingerprint(s, members, manifest=None, scripts=True, *, form="raw"):
    """Identity of one situation's judged members by content — see member_texts. A member no longer present hashes as absent.
    `scripts=False` is the identity judgments made before 1.2 carry (a mode by its command alone); judgments_status accepts
    either, so an upgrade stales nothing — the next consume records the fuller one."""
    import hashlib
    texts = member_texts(s, manifest, scripts)
    return hashlib.sha256("\n".join("%s\0%s" % (m, fingerprint_text(texts.get(m, "<absent>"), form)) for m in sorted(members)).encode("utf-8")).hexdigest()[:12]


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
        # the modes a situation was judged with must be the modes a session gets now: a new one (a plugin's SessionStart hook, the
        # project's CLAUDE.md) was read by no judge, a removed one leaves a judgment about a session that no longer happens
        now_modes = {h["source"] for h in s["hooks"] if h["event"] == "SessionStart" and h["source"].startswith("plugin:")
                     and h["source"][7:] in manifest.get("plugins", {})} | {f["source"] for f in instruction_files(target)}

        def changed(r):   # judged about text (current) or, for older judgments, about versions — each compared the way it was made
            if "members-text-fingerprint" in r and set(r.get("modes", [])) != now_modes:
                return True
            members = r.get("members", []) + r.get("modes", [])
            form = r.get("fingerprint-text") or "raw"
            if "member-fingerprints" in r:
                # per member: one whose identity moved is a change only when what the judge read of it moved — for a skill, a
                # role or an instruction file that is its identity; for a mode, the text its hook injects (asked only now).
                # guin-site, 10-13: chongdae's hook script changed, its line did not, and all nine situations were re-judged
                now = member_fingerprints(s, members, manifest, form=form)
                read = r.get("modes-read") or {}
                moved = [m for m in members if now.get(m) != r["member-fingerprints"].get(m)]
                if os.environ.get("HUNSU_SURVEY"):   # inside a probe a moved mode is not asked (its hook cannot run here): only the rest count
                    moved = [m for m in moved if m not in read]
                return any(not (m in read and mode_read_now(s, m, form=form) == read[m]) for m in moved)
            if "members-text-fingerprint" in r:
                return r["members-text-fingerprint"] not in (members_text_fingerprint(s, members, manifest, form=form), members_text_fingerprint(s, members, manifest, scripts=False, form=form))
            return r.get("members-fingerprint", "?") != members_fingerprint(manifest, members)
        stale = {"situations": [r["situation"] for r in j.get("situations", []) if changed(r)],
                 "unclustered": sorted(locked - set(j["skills"]))}
    return j, stale if stale["situations"] or stale["unclustered"] else {}


@mutates_project
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


MODE_NOTE = ("injected at SessionStart — a member of every situation. This is what the hook says in an empty project (no records, "
             "no run): its standing text. In a live session the parts that report state differ; judge the standing text")


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
    # whole, as members are: the judge quotes from this text
    mode_packets = [{"source": h["source"], "text": mode_text(h, s), "note": MODE_NOTE} for h in modes]
    for f in instruction_files(target):
        modes.append({"source": f["source"], "event": "SessionStart", "command": "", "matcher": "*"})
        mode_packets.append({"source": f["source"], "text": f["text"], "note": "read into every session (a committed instruction file) — a member of every situation"})
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
        # and the situations whose findings quote a mode's live state (read in a live session before 1.14): one that names a run,
        # a time or a count (`check` says so), or a sentence the mode does not say in an empty project — guin-site's dwitbuk quote
        # ("`dispose …` answers one.") was its line about one review's open objections; in an empty project it says nothing
        said = {}
        for m in mode_packets:
            said.setdefault(m["source"], []).append(quote_norm(m["text"]))
        live = {k: ["%s %s" % x for x in v] for k, v in state_bound_situations(j).items()}
        for r in j["situations"]:
            for f in r.get("findings", []):
                for m, q in (f.get("quotes") or {}).items():
                    if m.startswith("plugin:") and m in said and not any(quote_norm(q) in t for t in said[m]):
                        live.setdefault(r["situation"], []).append("%s: a sentence it does not say in an empty project" % m)
        for k, v in live.items():
            if k not in stale.get("situations", []):
                print("situation %r: re-judged — its findings quote a mode's live state (%s)" % (k, v[0]))
        redo = set(stale.get("situations", [])) | set(live)
        groups = [{"situation": r["situation"], "members": r["members"]} for r in j["situations"] if r["situation"] in redo]
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
            # whole: a cut at 6000 characters hid the second half of the longest skills (mangsang operate is 10.5k), and the
            # judge is told to quote from this text — a conflict in the tail could not be found, or quoted
            if "text" in by_id[m]:   # a role: the prompt its worker is sent
                return by_id[m]["text"]
            path = by_id[m]["path"]
            return io.open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        packet = {"artifact-type": "hunsu/judge-request@1", "stage": "group", "situation": g.get("situation", "group %d" % i),
                  "members": [{"id": m, "description": by_id[m]["description"], "path": by_id[m]["path"], "text": body(m)} for m in members],
                  "modes": [{"source": m["source"], "text": m["text"]} for m in mode_packets],
                  "kinds": {"overlap": "two or more claim the same work in this situation",
                            "contradiction": "one instructs what another forbids, or a mode's standing instruction conflicts with a member "
                                             "(a worker started by a `plugin:role` member receives the modes too)",
                            "premise": "one assumes something another forbids (e.g. artifacts in the repo vs a clean repo)"},
                  "classes": CLASSES,
                  "instructions": ("Judge only this situation. For each finding, name the members involved (a mode is named by its source), "
                                   "quote the exact sentence from each member's `text` (a mode's `text` is what it injects into every session) "
                                   "that creates the conflict (verbatim: hunsu checks each quote against that text and drops a finding whose quote is not there), say why, and propose one resolution as a JSON object written as a string: {\"use\": id} | {\"deny\": [ids]} | "
                                   "{\"order\": [ids]} | {\"accept\": reason}. No quote, no finding. Similar names are not evidence. "
                                   "Then give the finding a `class` from `classes`: how much it asks of a person. `authority` unless the members' own "
                                   "text settles it — and for any other class, `class_quote` is {member, quote}: the verbatim sentence from that member's "
                                   "text that settles it (hunsu checks it; a class whose sentence is not there is treated as authority). When unsure, "
                                   "authority. A mode's `text` is what its hook says in an empty project: quote the sentences that hold in "
                                   "every state, never a run's id, a time or a count (hunsu drops a finding whose mode quote names one). "
                                   "Change no files.")}
        res = manifest.get("resolutions", {}).get(packet["situation"])
        if isinstance(res, dict):   # the situation is already resolved: the judge also says whether that resolution still fits what it finds now
            packet["existing_resolution"] = {"resolution": res, **({"prior_findings": prior[packet["situation"]]} if packet["situation"] in prior else {})}
            packet["instructions"] += (" This situation already has a resolution (`existing_resolution`; the findings it was written about are its "
                                       "`prior_findings`). Additionally answer `resolution_verdict`: does that resolution still fit your findings? "
                                       "`verdict` is still-fits | does-not-fit | cannot-tell, with `why` and `quote` — a verbatim sentence from the "
                                       "member texts above grounding the verdict. No verbatim quote, no still-fits. The verdict is about "
                                       "the members' standing text: its `why` names no run, time or count, which the record would carry "
                                       "to every later session as if it stood.")
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
        # a finding's quote is checked against the text it claims to quote — the member's text as packed, or the mode's injected
        # text — whitespace-normalized. "No quote, no finding" was checked for presence only: a sentence the judge paraphrased or
        # made up passed, and a person was asked to resolve a conflict no text contains.
        norm = quote_norm
        texts = {m.get("id"): norm(m.get("text", "")) for m in req.get("members", [])}
        texts.update({m.get("source"): norm(m.get("text", "")) for m in req.get("modes", [])})
        findings = []
        for f in resp.get("findings", []):
            members = f.get("members", [])
            quotes = f.get("quotes", {})
            if isinstance(quotes, list):   # the schema's shape (a list of {member, quote}: Codex refuses open maps); read either
                quotes = {q.get("member"): q.get("quote") for q in quotes if isinstance(q, dict)}
            bad = (f.get("kind") not in KINDS or len(members) < 2 or any(m not in valid_ids for m in members)
                   or any(not str(quotes.get(m, "")).strip() for m in members) or not f.get("why"))
            unquoted = [] if bad else [m for m in members if norm(quotes.get(m, "")) not in texts.get(m, "")]
            if bad or unquoted:
                rejected.append("%s: %s%s" % (name, "quote not in the text of %s — " % ", ".join(unquoted) if unquoted else "",
                                              json.dumps(f, ensure_ascii=False)[:160]))
                continue
            hooked = [m.get("source") for m in req.get("modes", [])]
            bound = state_bound_quotes({"quotes": quotes}, hooked)
            if bound:   # true of the session it was read in, not of the mode: quote the sentence that holds in every state
                rejected.append("%s: a quote of %s names one state (%s) — quote the mode's sentence that holds in every state — %s"
                                % (name, bound[0][0], "; ".join(w for _, w in bound), json.dumps(f, ensure_ascii=False)[:160]))
                continue
            proposed = f.get("proposed") or {}
            if isinstance(proposed, str):   # Codex's strict schema carries it as text
                try:
                    proposed = json.loads(proposed) if proposed.strip() else {}
                except ValueError:
                    proposed = {"accept": proposed}
            finding = {"kind": f["kind"], "members": members, "quotes": quotes, "why": f["why"], "proposed": proposed if isinstance(proposed, dict) else {}}
            # the class: a settled one stands only on a sentence found in the text of one of the finding's members; else authority
            claimed, cq = f.get("class"), f.get("class_quote") if isinstance(f.get("class_quote"), dict) else {}
            if claimed in SETTLED:
                if state_bound_quotes({"class_quote": cq}, hooked):   # a count is no sentence that settles anything for good
                    finding.update({"class": "authority", "class-refused": "%s claimed on a sentence that names one state (%s)"
                                    % (claimed, "; ".join(w for _, w in state_bound_quotes({"class_quote": cq}, hooked)))})
                elif cq.get("member") in members and norm(cq.get("quote", "")) and norm(cq.get("quote", "")) in texts.get(cq.get("member"), ""):
                    finding.update({"class": claimed, "class_quote": {"member": cq["member"], "quote": cq["quote"]}})
                else:
                    finding.update({"class": "authority", "class-refused": "%s claimed; its sentence is not in %s's text" % (claimed, cq.get("member") or "a member")})
            else:
                finding["class"] = "authority"
            findings.append(finding)
        members = [m["id"] for m in req.get("members", [])]
        mode_ids = [m["source"] for m in req.get("modes", [])]   # a mode is a member of every situation; its plugin's version counts too
        # what travels for the resolution gate: the judge's verdict on the existing resolution, the texts it could quote from, and who judged
        w = resp.get("worker") or {}
        account = " ".join(str(w[k]) for k in ("host", "model") if w.get(k)) or args.by or "unknown"
        packed = "\n".join([str(m.get("text", "")) for m in req.get("members", [])] + [str(m.get("text", m.get("command", ""))) for m in req.get("modes", [])])
        verdicts[req.get("situation", name)] = (resp.get("resolution_verdict"), packed, account)
        results.append({"situation": req.get("situation", name), "members": members, "modes": mode_ids,
                        "members-fingerprint": members_fingerprint(manifest, members + mode_ids),
                        "fingerprint-text": TEXT_FORM,
                        "members-text-fingerprint": members_text_fingerprint(s, members + mode_ids, manifest, form=TEXT_FORM),
                        "member-fingerprints": member_fingerprints(s, members + mode_ids, manifest, form=TEXT_FORM),
                        "modes-read": modes_read(req.get("modes", []), form=TEXT_FORM),
                        # who judged: the worker's own account (host + model, machine-readable) when the response
                        # carries one; --by is the runner's claim, kept only as the fallback — "default model" tells
                        # a later reader nothing, "claude-code claude-opus-5[1m]" does
                        "judged-by": account, "findings": findings})
    # A re-judge that finds what was found before (the same kind, members and quotes, the same class) keeps the old finding as
    # it was — its `why`, its proposal, its class sentence: a judge words a reason anew each call, and a record that changes with
    # every rewording is a diff nobody can read (guin-site f304f91: 9 situations re-judged, 66 changed lines, no verdict changed).
    # When every finding is the old one, the situation keeps its judged-by too; only what it was judged about is recorded anew.
    old_by = {r["situation"]: r for r in existing.get("situations", [])}
    kept = []
    for r in results:
        old = old_by.get(r["situation"])
        if not old:
            continue
        pool = list(old.get("findings", []))
        same = 0
        for i, f in enumerate(r["findings"]):
            match = next((o for o in pool if finding_signature(o) == finding_signature(f) and finding_class(o) == finding_class(f)
                          and [(q.get("member"), quote_norm(q.get("quote", ""))) for q in [o.get("class_quote") or {}]]
                          == [(q.get("member"), quote_norm(q.get("quote", ""))) for q in [f.get("class_quote") or {}]]), None)
            if match is not None:
                pool.remove(match)
                r["findings"][i] = match
                same += 1
        if same == len(r["findings"]) and not pool:
            r["judged-by"] = old.get("judged-by", r["judged-by"])
            kept.append(r["situation"])
    import hashlib
    for r in results:
        r["findings-fingerprint"] = hashlib.sha256(json.dumps([(f["kind"], f["members"], f["quotes"]) for f in r["findings"]], sort_keys=True).encode("utf-8")).hexdigest()[:12]
    # A resolution a human wrote is about the findings they read. When a re-judged situation's findings differ, the judge's
    # own `resolution_verdict` decides: still-fits with a verbatim quote keeps the resolution (a delegation record replaces
    # `reviewed`, unless it is already exactly false); anything else — does-not-fit, cannot-tell, no verdict, a quote not in the texts — marks `reviewed: false`
    # for a human. Fail closed: the machine only closes the gate on grounded evidence. dwitbuk lists the unreviewed ones.
    before = {r["situation"]: r.get("findings-fingerprint") for r in existing.get("situations", [])}
    changed = [r["situation"] for r in results if r["situation"] in before and before[r["situation"]] != r["findings-fingerprint"]
               and isinstance(manifest.get("resolutions", {}).get(r["situation"]), dict)]
    unread, delegated = [], []
    for sit in changed:
        verdict, packed, account = verdicts.get(sit) or (None, "", args.by)
        v = verdict if isinstance(verdict, dict) else {}
        quote = str(v.get("quote") or "")
        bound = state_bound(quote) + state_bound(v.get("why"))
        if v.get("verdict") == "still-fits" and quote.strip() and quote_norm(quote) in quote_norm(packed) and not bound:
            if manifest["resolutions"][sit].get("reviewed") is False:
                unread.append(sit)
                print("situation %r: findings changed — the judge says the resolution still fits (quote verified); no person has read the proposal, so it stays unreviewed (reviewed: false)" % sit)
                continue
            # whole: a record cut at 200 kept half a sentence in the manifest and the lock (guin-site, 2026-09-28); where a line must be
            # short, it is cut where it is printed, not where it is stored
            manifest["resolutions"][sit]["reviewed"] = {"delegated": "judge %s: %s" % (account, str(v.get("why") or ""))}
            delegated.append(sit)
            print("situation %r: findings changed — the judge says the resolution still fits (quote verified); kept, delegation recorded" % sit)
        else:
            why = ("verdict %r" % v.get("verdict")) if v.get("verdict") in ("does-not-fit", "cannot-tell") else \
                  ("the verdict names one state (%s), not a standing fact" % "; ".join(bound) if v.get("verdict") == "still-fits" and bound else
                   "quote not found verbatim in the member texts" if v.get("verdict") == "still-fits" else "no resolution_verdict")
            manifest["resolutions"][sit]["reviewed"] = False
            unread.append(sit)
            print("situation %r: findings changed — %s; marked reviewed: false (a human must re-read)" % (sit, why))
    if changed:
        save_json(os.path.join(target, MANIFEST), manifest)
    # Record after the changed-findings gate, against the entry that consume leaves behind.
    for r in results:
        v, packed, account = verdicts[r["situation"]]
        entry = manifest.get("resolutions", {}).get(r["situation"])
        if not isinstance(v, dict) or v.get("verdict") not in ("does-not-fit", "cannot-tell") or not isinstance(entry, dict):
            continue
        quote = v.get("quote") or ""
        normalized = quote_norm(quote)
        r["resolution-verdict"] = {"verdict": v["verdict"], "why": v.get("why") or "", "quote": quote,
                                   "quote-found": bool(normalized) and normalized in quote_norm(packed),
                                   "judged-by": account, "resolution": resolution_fingerprint(entry)}
        print("situation %r: recorded %s%s"
              % (r["situation"], v["verdict"], "" if r["situation"] in changed else
                 " — the resolution and its reviewed are unchanged by this verdict"))
    if not cluster.get("groups"):   # a stale round: these situations replace their old selves; the rest stand
        redone = {r["situation"] for r in results}
        results = [r for r in existing.get("situations", []) if r["situation"] not in redone] + results
        rejected = existing.get("rejected", []) + [x for x in rejected if x not in existing.get("rejected", [])]
    doc = {"artifact-type": "hunsu/judgments@1", "skills-fingerprint": skills_fingerprint(manifest, s), "skills": judged_ids(manifest, s),
           "judged-by": args.by or judged_by(results), "situations": results, "rejected": rejected}
    save_json(os.path.join(target, JUDGMENTS), doc)
    write_conflicts_doc(target, doc, manifest)
    n_find = sum(len(r["findings"]) for r in results)
    n_settled = sum(finding_class(f) in SETTLED for r in results for f in r["findings"])
    print("judgments: %d situations · %d findings (%d settled by the members' text, %d for a person) · %d rejected (no quote / quote not in the text / a mode quote naming one state / unknown member / bad kind) -> %s, %s"
          % (len(results), n_find, n_settled, n_find - n_settled, len(rejected), JUDGMENTS, CONFLICTS_DOC))
    if kept:
        print("re-judged with the same findings — kept as they were (only what they were judged about is recorded anew): %s" % "; ".join(kept))
    if unread:
        print("findings changed under %d existing resolution(s) — marked reviewed: false until a human re-reads: %s" % (len(unread), "; ".join(unread)))
    if delegated:
        print("findings changed under %d existing resolution(s) — the judge confirmed each still fits; delegation recorded: %s" % (len(delegated), "; ".join(delegated)))
    print("next: for each situation with an authority finding, write resolutions[<situation>] in %s, then `hunsu check`" % MANIFEST)
    return 0


def write_conflicts_doc(target, doc, manifest):
    """The human-readable side of the judgments: what was found, the evidence, and what the judge proposed."""
    res = manifest.get("resolutions", {})
    lines = ["# hunsu — conflicts", "", "Judged by %s for skill set `%s`. A situation with findings needs an entry in `hunsu.json` `resolutions` "
             "keyed by the situation label when a finding is `authority`; `check` fails until it has one. A finding the members' own text settles "
             "(`sequenced`, `not-co-active`, `duplicate`, each on a sentence hunsu found in that text) needs none." % (doc.get("judged-by", "?"), doc["skills-fingerprint"]), ""]
    for r in doc["situations"]:
        authority = [f for f in r["findings"] if finding_class(f) == "authority"]
        state = "resolved" if r["situation"] in res else ("needs a resolution" if authority else ("settled by the members' text" if r["findings"] else "no conflict"))
        lines += ["## %s — %s" % (r["situation"], state), "", "members: " + ", ".join("`%s`" % m for m in r["members"]), ""]
        for f in r["findings"]:
            lines += ["- **%s** (%s) between %s" % (f["kind"], finding_class(f), ", ".join("`%s`" % m for m in f["members"])), "  - why: %s" % f["why"]]
            if f.get("class_quote"):
                lines.append("  - settled by `%s`: “%s”" % (f["class_quote"]["member"], str(f["class_quote"]["quote"]).strip()))
            if f.get("class-refused"):
                lines.append("  - class refused: %s" % f["class-refused"])
            for m, q in f["quotes"].items():
                lines.append("  - `%s`: “%s”" % (m, str(q).strip()))
            if f["proposed"]:
                lines.append("  - proposed: `%s`" % json.dumps(f["proposed"], ensure_ascii=False))
        if r["situation"] in res:
            lines.append("- resolution: `%s`" % json.dumps(res[r["situation"]], ensure_ascii=False))
        if r.get("resolution-verdict"):
            v = r["resolution-verdict"]
            lines.append("- resolution verdict: %s — judge %s: %s; quote (%s by hunsu): “%s”"
                         % (v["verdict"], v["judged-by"], v["why"],
                            "found" if v["quote-found"] else "not found", v["quote"]))
        lines.append("")
    if doc.get("rejected"):
        lines += ["## rejected by hunsu (no quote, unknown member, or bad kind)", ""] + ["- %s" % x for x in doc["rejected"]] + [""]
    with io.open(os.path.join(target, CONFLICTS_DOC), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


@mutates_project
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


@mutates_project
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
    linked = development_links(args.target, manifest)
    all_links = links(args.target)
    todo, blocked = [], []
    refresh = set(args.refresh or [])
    if refresh == {"all"}:
        refresh = set(linked)
    for name in sorted(refresh):
        copy_link = installed_copy_link(name, (manifest["plugins"].get(name) or {}).get("marketplace"), all_links[name]) if name in all_links else None
        if copy_link:
            host, cv, _ = copy_link
            raise SystemExit("plugin %s is linked to %s's installed copy %s, not a development source — nothing to re-snapshot; `hunsu upgrade %s` compares it with the pin (`hunsu link %s DIR` or `hunsu unlink %s` changes the link)"
                             % (name, host, cv, name, name, name))
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


OUTDATED_HELP = ("read-only, over the network: what each locked plugin's marketplace publishes now, against the lock and what is installed "
                 "here, with the steps to take a newer one. Nothing installed, cached or committed changes (a shallow clone into a temp dir)")
REMOTE_TIMEOUT = 60   # seconds per marketplace; a network that does not answer is said, never waited on for long


def _git(argv, cwd=None):
    """(ok, stdout or the reason) — git never prompts (a private marketplace without credentials fails, it does not hang)."""
    import subprocess
    try:
        done = subprocess.run(["git"] + argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=REMOTE_TIMEOUT,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS=""))
    except (OSError, subprocess.SubprocessError) as err:
        return False, "%s" % (type(err).__name__ if not isinstance(err, OSError) else err)
    if done.returncode:
        return False, ((done.stderr or done.stdout).strip().splitlines() or ["git exited %d" % done.returncode])[-1]
    return True, done.stdout


def marketplace_records():
    """{marketplace: {"source": {...}, "installLocation": path}} — Claude Code's own file; on Codex, config.toml's marketplaces
    in the same shape (a git source as a url, a local one as a path)."""
    if HOST != "codex":
        return load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json"))
    path = os.path.join(CODEX, "config.toml")
    try:
        import tomllib
        conf = tomllib.loads(io.open(path, encoding="utf-8").read()) if os.path.exists(path) else {}
    except (ModuleNotFoundError, ValueError):
        return {}
    out = {}
    for name, m in (conf.get("marketplaces") or {}).items():
        src = str((m or {}).get("source", ""))
        out[name] = {"source": {"source": "directory", "path": src} if m.get("source_type") == "local" else {"source": "git", "url": src}}
    return out


def published_url(market):
    """Where a marketplace is published: its local copy's `origin` (the host keeps a git marketplace as a clone), else its
    github repo or url. None for a directory marketplace: what is there is what is published."""
    market = market or {}
    src = market.get("source") or {}
    if src.get("source") in ("directory", "local"):
        return None
    loc = market.get("installLocation")
    if loc and os.path.isdir(os.path.join(loc, ".git")):
        ok, out = _git(["-C", loc, "remote", "get-url", "origin"])
        if ok and out.strip():
            return out.strip()
    if src.get("repo"):
        return "https://github.com/%s.git" % src["repo"]
    return src.get("url") or None


def published_versions(url, names):
    """({name: version or None}, None) for what the marketplace at `url` publishes now, or ({}, reason) when it cannot be asked.
    Read-only by construction: a shallow, blob-less clone into a temp dir, read and deleted — the marketplace's copy on this
    machine (what the host installs from), the plugin cache and the install records are not touched; `claude plugin
    marketplace update` stays the step that moves them. The plugin's own plugin.json at the published head wins over the
    listing's `version`, as for the local copy."""
    import posixpath, tempfile
    tmp = scratch_directory("hunsu-outdated-", "outdated's marketplace clone")
    try:
        ok, why = _git(["clone", "--quiet", "--depth", "1", "--no-checkout", "--filter=blob:none", url, os.path.join(tmp, "m")])
        if not ok:
            return {}, why
        repo = os.path.join(tmp, "m")
        def show(path):
            ok, out = _git(["-C", repo, "show", "HEAD:" + path])
            try:
                return json.loads(out) if ok else {}
            except ValueError:
                return {}
        listings = {cat: show(cat) for cat in catalogs()}
        out = {}
        for name in names:
            entry = catalog_entry(listings.get, name)
            if entry is None:
                out[name] = None
                continue
            v = None
            rel = catalog_path(entry)   # "./plugin" (or Codex's source.path) since 2.2.0, "./" before: confined to the repository
            base = posixpath.normpath(rel.replace("\\", "/")) if isinstance(rel, str) and rel.strip() else None
            if base is not None and not (posixpath.isabs(base) or base == ".." or base.startswith("../")):
                base = "" if base == "." else base
                for m in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "plugin.json"):
                    doc = show(posixpath.join(base, m) if base else m)
                    v = doc.get("version") if isinstance(doc, dict) else None
                    if v:
                        break
            out[name] = v or entry.get("version")
        return out, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_records_elsewhere(target, name, market):
    """The host's install records for `name` under another marketplace than the lock's: [(key, here, n other projects)] —
    `here` when one applies to this project (its own project-scope record, or a user-scope one), which is a second copy that
    can load here; else they are other projects' (an old local marketplace's), and say nothing about this one."""
    if HOST == "codex":
        return []
    installed = load_json(os.path.join(CLAUDE, "plugins", "installed_plugins.json")).get("plugins", {})
    want = os.path.realpath(target)
    out = []
    for key, entries in sorted(installed.items()):
        n, _, m = key.partition("@")
        if n != name or m == market or not entries:
            continue
        here = any(e.get("scope") == "user" or (e.get("projectPath") and os.path.realpath(e["projectPath"]) == want) for e in entries)
        out.append((key, here, sum(1 for e in entries if e.get("projectPath") and os.path.realpath(e["projectPath"]) != want)))
    return out


def cmd_outdated(args):
    """Per locked plugin: locked, installed for this project, this machine's marketplace copy, published now — and, when they
    differ, the steps in order. `check` is offline by design; this is the one command that asks the network, and it changes
    nothing. A marketplace that cannot be reached is said, and the rest are still asked: exit 0 whatever the network did."""
    target = args.target
    manifest = load_json(os.path.join(target, MANIFEST))
    lock = load_json(os.path.join(target, LOCK))
    plugins = lock.get("plugins") or manifest.get("plugins") or {}
    if not plugins:
        raise SystemExit("no %s or %s with plugins — nothing to compare" % (LOCK, MANIFEST))
    markets = marketplace_records()
    by_market = {}
    for name, p in sorted(plugins.items()):
        by_market.setdefault(p.get("marketplace"), []).append(name)
    asked = {}
    for market, names in sorted(by_market.items(), key=lambda kv: str(kv[0])):
        rec = markets.get(market)
        url = published_url(rec)
        if rec is None:
            asked[market] = ({}, "marketplace %s is not known on this machine — `hunsu install` adds it" % market)
        elif url is None:   # a directory marketplace: its tree is the release
            loc = (rec.get("source") or {}).get("path") or rec.get("installLocation") or ""
            asked[market] = ({n: source_version(linked_source(loc, n)) if linked_source(loc, n) else None for n in names}, None)
        else:
            asked[market] = published_versions(url, names)
    print("hunsu outdated — what each locked plugin's marketplace publishes now (asked over the network, read-only: nothing installed or cached here changes)")
    owed = 0
    for name, p in sorted(plugins.items()):
        market = p.get("marketplace")
        locked = ((lock.get("plugins") or {}).get(name) or {}).get("version")
        pinned = ((manifest.get("plugins") or {}).get(name) or {}).get("version")
        have = installed_version(target, name, market)
        copy = marketplace_version(markets.get(market), name) if HOST != "codex" else None
        got, why = asked.get(market, ({}, "not asked"))
        pub = got.get(name)
        parts = ["locked %s" % (locked or "-"), "installed %s" % (have or "-")]
        if copy:
            parts.append("marketplace copy here %s" % copy)
        parts.append("published %s" % (pub or "?"))
        if why:
            state = "could not ask the marketplace (%s) — nothing is said about what it publishes" % why
        elif pub is None:
            state = "the marketplace %s publishes no plugin %s now" % (market, name)
        else:
            state = "up to date"
        goal = None
        if not why and pub and _semver(pub) and (_semver(locked or pinned or "") is None or _semver(pub) > _semver(locked or pinned)):
            goal, state = pub, "%s %s is published" % (name, pub)
        elif have and (locked or pinned) and have != (locked or pinned):
            newer = _semver(have) and _semver(locked or pinned) and _semver(have) > _semver(locked or pinned)
            goal = have if newer else (locked or pinned)
            state = ("%s is installed here and not yet locked" if newer else "installed %s is behind the lock") % have
        print("  %-10s %s — %s" % (name, " · ".join(parts), state))
        if goal:
            owed += 1
            for i, st in enumerate(update_steps(name, market, goal, installed=have, pinned=pinned, locked=locked, copy=copy,
                                                scope=install_scope(target, name, market), linked=name in development_links(target, manifest)), 1):
                print("      %d. %s" % (i, st))
        for key, here, others in install_records_elsewhere(target, name, market):
            if here:
                print("      also installed as %s, and that record applies to this project — two copies of %s can load here: `claude plugin uninstall %s --scope project` (run in this project) keeps the lock's (%s@%s)"
                      % (key, name, key, name, market))
            else:
                print("      also recorded as %s in installed_plugins.json, for %d other project(s) — not this project's: what loads here is %s@%s" % (key, others, name, market))
    print("%d plugin(s) with a step owed · %d marketplace(s) asked · %d could not be asked"
          % (owed, len(asked), sum(1 for _, why in asked.values() if why)))
    return 0


def policy_line(key, res):
    """One situation's resolution as the SessionStart hook says it."""
    if isinstance(res, str):
        return "%s: %s" % (key, "denied" if res == "deny" else "%s's" % res)
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
    return line


def policy_lines(lock):
    """The lock's resolutions as the lines the host injects at SessionStart — the policy materialized where the agent reads,
    one line per situation. In a package manager, resolving is applying; here `deny` is applied by the sentinel and the rest by
    these lines. `reviewed: false` is shown as such, never hidden."""
    return [policy_line(key, res) for key, res in (lock.get("resolutions") or {}).items()]


def resolution_reasons(res):
    """What the hook's line cut: `accept` past 160 characters, `note` past 120, whole. use/deny/order are whole in the line."""
    if isinstance(res, str):
        return []
    return ["%s (whole): %s" % (field, res[field]) for field, cut in (("accept", 160), ("note", 120)) if len(res.get(field) or "") > cut]


def settled_by(res, short=False):
    """Who settled a resolution, as its `reviewed` records it; short: the name alone (a delegation's judge, not its reason)."""
    rv = None if isinstance(res, str) else res.get("reviewed")
    if rv is False:
        return "nobody yet — the judge's proposal, applied unread (reviewed: false)"
    if isinstance(rv, dict) and rv.get("by"):
        return "by %s%s" % (rv["by"], (" — " + rv["why"]) if rv.get("why") else "")
    if isinstance(rv, dict) and rv.get("delegated"):
        bound = [] if short else state_bound(rv["delegated"])
        return "delegated: %s%s" % (rv["delegated"].split(": ", 1)[0] if short else rv["delegated"],
                                    " [names one state (%s): true when it was judged, not now]" % "; ".join(bound) if bound else "")
    if rv is True:
        return "reviewed (no name recorded)"
    return "no `reviewed` record — written into %s by hand" % MANIFEST


def open_situations(manifest, judged):
    """Judged situations with a finding for a person that no resolution covers — check_judgments' rule, read from the file."""
    res = manifest.get("resolutions", {})
    covered = {finding_signature(f) for r in judged.get("situations", []) if r["situation"] in res for f in r["findings"]}
    out = []
    for r in judged.get("situations", []):
        if r["situation"] in res:
            continue
        open_ = [f for f in r["findings"] if finding_class(f) == "authority" and finding_signature(f) not in covered]
        if open_:
            out.append((r["situation"], len(open_)))
    return out


def conflict_lines(judged, situation):
    """What a person judges under an unreviewed resolution: the situation's findings for a person, each member's sentence."""
    r = next((r for r in (judged or {}).get("situations", []) if r["situation"] == situation), None)
    found = [f for f in (r or {}).get("findings", []) if finding_class(f) == "authority"]
    if not found:
        return ["the conflict it answers: no finding for a person under this situation in %s now — `hunsu check`" % JUDGMENTS]
    lines = ["the conflict it answers (%s):" % JUDGMENTS]
    for f in found:
        lines.append("  %s between %s%s" % (f.get("kind"), ", ".join(f.get("members", [])), (": " + f["why"]) if f.get("why") else ""))
        lines += ["    %s: “%s”" % (m, " ".join(str(q).split())) for m, q in (f.get("quotes") or {}).items()]
        if f.get("proposed"):
            lines.append("    the judge proposed: %s" % json.dumps(f["proposed"], ensure_ascii=False))
    return lines


POLICY_HELP = ("read-only: this project's resolutions by situation — the line the SessionStart hook prints (from hunsu.lock.json), each "
               "one's reason and who settled it (from hunsu.json; where the two differ it says so), and the judged situations that still need one")


def cmd_policy(args):
    """Read-only: the project's resolutions by situation — the line the SessionStart hook prints (from the lock: what was
    locked is what sessions get), and from hunsu.json, where declarations are read, each one's reason and who settled it;
    where the two differ, it says so. Then the judged situations that still need a resolution."""
    manifest = load_json(os.path.join(args.target, MANIFEST))
    lock = load_json(os.path.join(args.target, LOCK))
    if not manifest:
        print("no %s here — nothing declared, nothing locked (`hunsu init`, or the compose skill)" % MANIFEST)
        return 0
    declared, locked = manifest.get("resolutions", {}), (lock.get("resolutions") or {}) if lock else None
    judged = load_json(os.path.join(args.target, JUDGMENTS))
    unread = []
    if locked is None:
        print("hunsu policy — no %s: sessions get no policy yet. What %s declares (lines as the hook would print them after `hunsu lock`):"
              % (LOCK, MANIFEST))
    else:
        print("hunsu policy — resolutions by situation. Each line is the lock's (%s), as the SessionStart hook prints it — the rule is "
              "the reason; who settled it is %s's." % (LOCK, MANIFEST))
    for key in list(locked or {}) + [k for k in declared if k not in (locked or {})]:
        mine, theirs = declared.get(key), (locked or {}).get(key)
        print("- %s" % policy_line(key, theirs if theirs is not None else mine))
        if mine is None:
            print("    in the lock, gone from %s — sessions still get it until `hunsu lock`" % MANIFEST)
            mine = theirs
        elif locked is not None and theirs is None:
            print("    in %s, not in the lock — sessions do not get it until `hunsu lock`" % MANIFEST)
        elif theirs is not None and theirs != mine:
            rule = lambda r: r if isinstance(r, str) else {k: v for k, v in r.items() if k != "reviewed"}
            if rule(theirs) != rule(mine):
                print("    %s now says: %s — sessions get the lock's until `hunsu lock`" % (MANIFEST, policy_line(key, mine)))
            else:
                print("    settled again since the lock (the lock's: %s) — `hunsu lock` carries this one" % settled_by(theirs, short=True))
        for reason in resolution_reasons(mine):
            print("    %s" % reason)
        print("    settled: %s" % settled_by(mine))
        if isinstance(mine, dict) and mine.get("reviewed") is False:
            unread.append(key)
            for line in conflict_lines(judged, key):
                print("    " + line)
    if not declared and not locked:
        print("  (no resolutions)")
    if unread:   # asked "판정 알려줘", a session put the rules alone as yes/no questions, "맞다 (Recommended)", and recorded four 맞다 (sagoforce, 10-26)
        print("%d unreviewed (%s): a person judges the conflict, not the rule — show each one's members' sentences above and what the rule "
              "makes them do, then record what they said as resolutions[<situation>].reviewed = {\"by\": NAME, \"why\": what they read} "
              "(or the rule they chose instead), and `hunsu lock`. A delegated one was confirmed by the judge on a quote: it is not owed a "
              "person, and its record stays unless the person read that one too." % (len(unread), "; ".join(unread)))
    if manifest.get("judge") == "skip":
        print("judge: skipped (%s) — no judged situations to resolve" % MANIFEST)
    elif not judged:
        print("no conflict judgment yet (%s) — `hunsu judge request`" % JUDGMENTS)
    else:
        need = open_situations(manifest, judged)
        print("still need a resolution (%s): %s" % (JUDGMENTS, "; ".join("%s (%d finding(s) for a person)" % kv for kv in need) or "none")
              + (" — see %s" % CONFLICTS_DOC if need else "") + ". Whether the judgments are current: `hunsu check`.")
    return 0


# ---------------------------------------------------------------- roles: capability alternates

WORKER_HOSTS = ("claude", "codex")   # what the workers' `--host` takes (hacheong, dwitbuk); `claude-code` is read as `claude`
# Host facts — the environment's, so hunsu knows them: what a worker's sandbox on each host cannot do. A Codex worker runs
# sandboxed with no network and no local port to bind (a dev server like `wrangler dev`); a Claude worker can do both.
# Whether hired hands need these is the project's decision, asked once: hunsu.json `capabilities`.
CAPABILITIES = {"loopback": "bind a local port (a dev server like `wrangler dev`)", "network": "reach the network"}
SANDBOX_LACKS = {"codex": ("loopback", "network"), "claude": ()}
DECISIONS = ("alternates", "session")   # alternates: such work is hired on a `<role>@<cap>` role; session: the session does it
ALTERNATE = re.compile(r"^([A-Za-z0-9_.-]+)@([a-z0-9-]+(?:\+[a-z0-9-]+)*)$")
CAPABILITY_HELP = ("the project's answer, once: do hired hands need loopback (a local port: a dev server like `wrangler dev`) or the "
                   "network? The current routing table models Codex workers as lacking both; it is not a live capability probe. "
                   "`--session`: the session does such work; `--alternates`: it is hired "
                   "on a `<role>@<cap>` role. Writes hunsu.json `capabilities`, and `roles` when --host creates alternates; "
                   "lock new role providers before use")
ROLE_HELP = ("a role's capability alternates (`<role>@<cap>[+<cap>]`, read by chongdae from the lock): `add NAME@CAP --from ROLE --host H` "
             "copies ROLE's provider onto another host into hunsu.json; `list` (read-only) shows the roles and their alternates")


def argv_option(argv, flag):
    """(index of the flag, its value) for `--flag VALUE` or `--flag=VALUE` in an argv; (None, None) when absent."""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return i, argv[i + 1]
        if a.startswith(flag + "="):
            return i, a[len(flag) + 1:]
    return None, None


def drop_option(argv, flag):
    """argv without `--flag VALUE` / `--flag=VALUE`, and the value dropped (None if there was none)."""
    i, value = argv_option(argv, flag)
    if i is None:
        return argv, None
    return argv[:i] + argv[i + (1 if "=" in argv[i] else 2):], value


def alternate_argv(manifest, s, source, provider, host):
    """(argv on `host`, from-what note) for an alternate of a role whose provider is `provider` — or (None, why not).
    An argv is copied; `plugin:role` is materialized the way `lock` does (the argv that plugin declares, `{host}` filled with
    the new host). `session` and `native:` run in the host's own session, so there is no worker whose host could change."""
    if provider == "session":
        return None, "role %s is performed by the session itself (`session`): there is no worker whose --host could change" % source
    if isinstance(provider, str) and provider.startswith("native:"):
        return None, ("role %s is %r — the host's own subagent at the session's hand, not a spawned worker: it runs where the session runs, "
                      "with no --host to change" % (source, provider))
    if isinstance(provider, list):
        return [str(a) for a in provider], "copied from %s" % source
    argv, why = role_argv(manifest, s, provider, host=host)
    if not argv:
        return None, "role %s: provider %r — %s" % (source, provider, why)
    return argv, "materialized from %s (%s), as `hunsu lock` does" % (source, provider)


def worker_host(provider):
    """The host a worker role runs on: an argv on the request/response protocol with a `--host` — else None."""
    if not isinstance(provider, list) or "{request}" not in [str(a) for a in provider]:
        return None
    host = argv_option([str(a) for a in provider], "--host")[1]
    return {"claude-code": "claude"}.get(host, host)


def undecided_capabilities(manifest):
    """[(host, [caps], [roles])] — worker roles whose host's sandbox lacks a capability the project has not decided about
    (hunsu.json `capabilities`) and that no `<role>@<cap>` alternate of theirs covers. Empty when everything is settled."""
    roles, decided = manifest.get("roles") or {}, manifest.get("capabilities") or {}
    out = {}
    for name, provider in sorted(roles.items()):
        if ALTERNATE.match(name):
            continue
        host = worker_host(provider)
        covered = set().union(*([set(m.group(2).split("+")) for k in roles for m in [ALTERNATE.match(k)] if m and m.group(1) == name] or [set()]))
        for cap in SANDBOX_LACKS.get(host, ()):
            if decided.get(cap) not in DECISIONS and cap not in covered:
                caps, names = out.setdefault(host, ([], []))
                caps.append(cap) if cap not in caps else None
                names.append(name) if name not in names else None
    return [(host, caps, names) for host, (caps, names) in sorted(out.items())]


def capable_host(caps):
    return next((h for h in WORKER_HOSTS if not set(caps) & set(SANDBOX_LACKS.get(h, ()))), None)


def capability_question(host, caps, names):
    """The one line `check` warns with and `compose` asks: what the host cannot do, which roles run there, both answers."""
    other, caps_arg = capable_host(caps), " ".join(caps)
    return ("roles on %s cannot %s (%s): decide once — `hunsu capability %s --alternates --host %s` (such work hired on %s) or "
            "`hunsu capability %s --session` (the session does it)"
            % (host, " or ".join(CAPABILITIES[c] for c in caps), ", ".join(names), caps_arg, other, other, caps_arg))


def check_capabilities(manifest, found):
    errors, warnings, info = found
    # 5b. capabilities — hired hands on a host whose sandbox lacks a capability: the project decides once (hunsu.json
    # `capabilities`, or a `<role>@<cap>` alternate), so a run never stops mid-way to ask. A warning: lock is not blocked.
    for cap, value in sorted((manifest.get("capabilities") or {}).items()):
        if cap not in CAPABILITIES or value not in DECISIONS:
            warnings.append("capabilities.%s: %r — capabilities are %s, each `alternates` or `session`" % (cap, value, ", ".join(sorted(CAPABILITIES))))
    for host, caps, names in undecided_capabilities(manifest):
        warnings.append(capability_question(host, caps, names))


def alternates_covering(roles, role):
    """[set of caps] of `role`'s existing `<role>@<cap>` alternates."""
    return [set(m.group(2).split("+")) for k in roles for m in [ALTERNATE.match(k)] if m and m.group(1) == role]


@mutates_project
def cmd_capability(args):
    """Record the project's answer for capabilities: `session` (the session does such work) or `alternates` (hired on a
    `<role>@<cap>` role). With `--alternates --host H`, the alternates themselves too: `<role>@<caps>` on H for every worker
    role on a host lacking those capabilities — the setup answer is one command. hunsu.json only."""
    path = os.path.join(args.target, MANIFEST)
    manifest = load_json(path)
    if not manifest:
        raise SystemExit("no %s — run `hunsu init` first" % MANIFEST)
    caps = [c for c in CAPABILITIES if c in args.caps]   # one order, whatever order they were named in
    if args.session and (args.host or args.model or args.effort):
        raise SystemExit("--host/--model/--effort go with --alternates: they say where such work is hired; --session hires no one")
    if not args.host and (args.model or args.effort):
        raise SystemExit("--model/--effort need --host: the alternates' host")
    host = {"claude-code": "claude"}.get(args.host, args.host)
    value = "session" if args.session else "alternates"
    roles = dict(manifest.get("roles") or {})
    added = []
    if host:
        lacking = [c for c in caps if c in SANDBOX_LACKS.get(host, ())]
        if lacking:
            raise SystemExit("not recorded — %s's sandbox cannot %s either; name a host whose can (%s)"
                             % (host, " or ".join(CAPABILITIES[c] for c in lacking), capable_host(caps)))
        for role, provider in sorted(roles.items()):
            on = worker_host(provider)
            need = [c for c in caps if c in SANDBOX_LACKS.get(on, ())]
            if ALTERNATE.match(role) or not need or any(set(need) <= have for have in alternates_covering(roles, role)):
                continue   # not a worker, its host can, or an alternate already covers it
            name = "%s@%s" % (role, "+".join(need))
            argv, note, was, dropped = build_alternate(manifest, lambda: None, role, name, host, args.model, args.effort)
            roles[name] = argv
            added.append((name, argv, was, dropped))
        manifest["roles"] = roles
    manifest["capabilities"] = dict(manifest.get("capabilities") or {}, **{c: value for c in caps})
    save_json(path, manifest)
    for cap in caps:
        print("capabilities.%s = %s in %s — %s" % (cap, value, MANIFEST,
              "work a hired member's sandbox cannot do (%s) is done by the session" % CAPABILITIES[cap] if value == "session"
              else "such work is hired on a `<role>@%s` role" % cap))
    for name, argv, was, dropped in added:
        print('  added "%s": %s' % (name, json.dumps(argv, ensure_ascii=False)))
        if dropped:
            print("  " + dropped_line(dropped, was, host))
    if value == "alternates":
        for cap in caps:
            have = sorted(k for k in roles if ALTERNATE.match(k) and cap in ALTERNATE.match(k).group(2).split("+"))
            print("  alternates for %s: %s" % (cap, ", ".join(have) if have else "none yet — `hunsu capability %s --alternates --host %s`; "
                                                 "until then the session does it" % (cap, capable_host([cap]))))
    print("next: `hunsu lock` — chongdae hires the alternates from %s `roles`" % LOCK if added
          else "chongdae reads this from %s; no lock needed for it" % MANIFEST)
    return 0


def build_alternate(manifest, surveyed, source, name, host, model, effort):
    """(argv, from-what note, the host it was on, [dropped options]) for `name`, an alternate of `source` on `host` — or
    ValueError(why not). The argv is ROLE's, its --host replaced (or added to a worker), --model/--effort dropped unless
    given: a model named for one host is not valid on another. `surveyed` is called only for a `plugin:role` provider."""
    provider = (manifest.get("roles") or {})[source]
    needs_survey = isinstance(provider, str) and provider != "session" and not provider.startswith("native:")
    argv, note = alternate_argv(manifest, surveyed() if needs_survey else None, source, provider, host)
    if argv is None:
        raise ValueError(note)
    i, was = argv_option(argv, "--host")
    if i is None:
        # a worker on the request/response protocol takes --host (hacheong's, dwitbuk's); anything else is a command whose
        # arguments hunsu cannot know — appending a flag it never declared would break it, not move it
        if "{request}" not in argv:
            raise ValueError("role %s's provider has no --host argument and is not a worker (no {request}): %s. hunsu cannot "
                             "tell how to run it on another host; if it can, write %s by hand in %s `roles`"
                             % (source, json.dumps(argv, ensure_ascii=False), name, MANIFEST))
        argv = argv + ["--host", host]
    else:
        argv = argv[:i] + (["--host", host] + argv[i + 2:] if argv[i] == "--host" else ["--host=" + host] + argv[i + 1:])
    dropped = []
    for flag, given in (("--model", model), ("--effort", effort)):
        argv, value = drop_option(argv, flag)
        if value is not None and given is None:
            dropped.append("%s %s" % (flag, value))
        if given is not None:
            argv += [flag, given]
    return argv, note, was, dropped


def dropped_line(dropped, was, host):
    return ("  dropped %s: %s Give --model/--effort to set them (or edit %s)"
            % (" ".join(dropped), "a model named for %s is not valid on %s." % (was, host) if was and was != host
               else "a model named for one host is not valid on another, so none is carried over.", MANIFEST))


def cmd_role(args):
    return cmd_role_add(args) if args.action == "add" else cmd_role_list(args)


@mutates_project
def cmd_role_add(args):
    """Write `NAME@CAP` into hunsu.json `roles`: ROLE's provider as argv, on another host. Read where it is written: hunsu.json."""
    path = os.path.join(args.target, MANIFEST)
    manifest = load_json(path)
    if not manifest:
        raise SystemExit("no %s — run `hunsu init` first" % MANIFEST)
    m = ALTERNATE.match(args.name)
    if not m:
        raise SystemExit("%r is not an alternate: write `<role>@<cap>[+<cap>]` (e.g. nitpick@loopback, implementer@loopback+gpu) — "
                         "capabilities in lower case, joined by `+`" % args.name)
    source = args.source or m.group(1)
    host = {"claude-code": "claude"}.get(args.host, args.host)
    roles = manifest.get("roles") or {}
    if source not in roles:
        raise SystemExit("no role %s in %s `roles` — it declares: %s" % (source, MANIFEST, ", ".join(sorted(roles)) or "none"))
    if args.name in roles and not args.force:
        raise SystemExit("%s is already in %s `roles`: %s — `--force` to replace it" % (args.name, MANIFEST, json.dumps(roles[args.name], ensure_ascii=False)))
    if args.name == source:
        raise SystemExit("--from %s is the alternate itself; name the role it is an alternate of" % source)
    try:
        argv, note, was, dropped = build_alternate(manifest, lambda: survey(args.target), source, args.name, host, args.model, args.effort)
    except ValueError as why:
        raise SystemExit("not added — %s" % why)
    roles = dict(roles)
    replaced = args.name in roles
    roles[args.name] = argv
    manifest["roles"] = roles
    caps = m.group(2).split("+")
    # adding an alternate is the project's answer for these capabilities: such work is hired there
    manifest["capabilities"] = dict(manifest.get("capabilities") or {}, **{c: "alternates" for c in caps if c in CAPABILITIES})
    save_json(path, manifest)
    print("%s role %s in %s — %s, on %s%s:" % ("replaced" if replaced else "added", args.name, MANIFEST, note, host,
                                            " (was %s)" % was if was is not None and was != host else ""))
    print('  "%s": %s' % (args.name, json.dumps(argv, ensure_ascii=False)))
    if dropped:
        print(dropped_line(dropped, was, host))
    if was == host:
        print("  note: %s already runs on %s — an alternate on the same host gets the same sandbox" % (source, host))
    lacking = [c for c in caps if c in SANDBOX_LACKS.get(host, ())]
    if lacking:
        print("  note: %s's sandbox cannot %s — this alternate will lack it too" % (host, " or ".join(CAPABILITIES[c] for c in lacking)))
    recorded = [c for c in caps if c in CAPABILITIES]
    if recorded:
        print("  capabilities: %s = alternates in %s (such work is hired on an alternate)" % (", ".join(recorded), MANIFEST))
    print("next: `hunsu lock` — chongdae reads the alternates from %s `roles`; %s is hired for a task that requires %s"
          % (LOCK, args.name, " and ".join(m.group(2).split("+"))))
    return 0


def describe_provider(provider):
    """One short line for a role's provider: who, on which host and model — not the whole argv."""
    if not isinstance(provider, list):
        return str(provider) if not isinstance(provider, dict) else json.dumps(provider, ensure_ascii=False)
    parts = [next((a for a in provider if "{plugin:" in a), " ".join(provider[:3]))]
    for flag in ("--member", "--host", "--model", "--effort"):
        value = argv_option(provider, flag)[1]
        if value is not None:
            parts.append("%s %s" % (flag, value))
    return " ".join(parts)


def cmd_role_list(args):
    """Read-only: hunsu.json `roles`, each with its capability alternates; where the lock differs, it says so."""
    manifest = load_json(os.path.join(args.target, MANIFEST))
    if not manifest:
        print("no %s here — nothing declared (`hunsu init`, or the compose skill)" % MANIFEST)
        return 0
    roles = manifest.get("roles") or {}
    lock = load_json(os.path.join(args.target, LOCK))
    locked = lock.get("roles-declared") if lock else None
    base = lambda k: ALTERNATE.match(k).group(1) if ALTERNATE.match(k) else k
    print("hunsu roles — %s `roles`; an alternate `<role>@<cap>` is hired for a task that requires those capabilities (chongdae, from %s)"
          % (MANIFEST, LOCK))
    if not roles:
        print("  (no roles)")
    for name in sorted({base(k) for k in roles}):
        print("- %s: %s" % (name, describe_provider(roles[name]) if name in roles else "(no plain role — only alternates)"))
        for key in sorted(k for k in roles if k != name and base(k) == name):
            print("    @%s: %s" % (key.split("@", 1)[1], describe_provider(roles[key])))
    if locked is None:
        print("not locked yet (no %s) — `hunsu lock`" % LOCK)
    else:
        diff = sorted(k for k in set(roles) | set(locked) if roles.get(k) != locked.get(k))
        if diff:
            print("differs from %s: %s — chongdae hires from the lock's until `hunsu lock`" % (LOCK, ", ".join(diff)))
    return 0


@mutates_project
def cmd_lock(args):
    """Snapshot of the resolved environment. Written only when check has no errors; warnings are recorded, not hidden.
    Recorded warnings are check's once this lock and, on Codex, its hook groups are in place."""
    dev = load_json(os.path.join(args.target, LOCAL)).get("dev", {})
    if dev:
        # a lock names released content; a working source on one machine is neither released nor the team's
        raise SystemExit("not locked — in development here: %s. The lock names what the team installs; `hunsu dev --off %s` first"
                         % (", ".join("%s (from %s)" % kv for kv in sorted(dev.items())), " ".join(sorted(dev))))
    errors, warnings, _ = check(args.target)
    manifest = load_json(os.path.join(args.target, MANIFEST))
    s = survey(args.target)
    errors = lock_blocking(args.target, manifest, s, errors)   # on Codex, the generated hooks this lock's regeneration repairs
    if errors:
        for line in errors:
            print("  error %s" % line)
        raise SystemExit("not locked — %d error(s); fix them or run `hunsu check`" % len(errors))
    linked = {}
    for name, link in links(args.target).items():
        copy_link = installed_copy_link(name, (manifest["plugins"].get(name) or {}).get("marketplace"), link)
        linked[name] = {"installed-copy": copy_link[0], "version": copy_link[1]} if copy_link else True
    skip = unprobed_hosts(args.target, manifest, s)
    lock = {"artifact-type": "hunsu/lock@1", "host": HOST,
            # what this machine had; a host engine its `hosts` leaves out and nothing requires is not run: null, not probed (as check says)
            "engines": {e: None if e in skip else version_of(HOST_EXE.get(e, e)) for e in manifest.get("engines", {})},
            "plugins": {name: {"version": enabled(s, name, want)["version"], "source": want["source"], "marketplace": want.get("marketplace"),
                               "fingerprint": tree_fingerprint(enabled(s, name, want)["path"]) if os.path.isdir(enabled(s, name, want).get("path") or "") else None,
                               **({"linked": linked[name]} if name in linked else {})}   # the content this version named when it was locked
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
            # how each product's records are read (`reads` in its plugin.json: kind -> argv) — a reader takes them from here, never from a sibling
            "reads": {name: r for name, want in manifest["plugins"].items() for r in [enabled(s, name, want).get("reads") or {}] if r},
            "judge": "skipped" if manifest.get("judge") == "skip" else ("none" if judgments_status(manifest, s, args.target)[0] is None else "judged"),
            "warnings": warnings}
    save_json(os.path.join(args.target, LOCK), lock)
    if HOST == "codex":
        materialize_codex_hooks(args.target, manifest, s)
    _, warnings, _ = check(args.target)
    if warnings != lock["warnings"]:
        lock["warnings"] = warnings
        save_json(os.path.join(args.target, LOCK), lock)
    print("locked %d plugins · %d skills · %d hooks -> %s (%d warnings recorded)"
          % (len(lock["plugins"]), len(lock["skills"]), len(lock["hooks"]), LOCK, len(warnings)))
    return 0


def plugin_version_at(root):
    """The version a plugin root declares — what a hook running from that root is."""
    for m in (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"), "plugin.json"):
        v = load_json(os.path.join(root, m)).get("version") if root else None
        if v:
            return v
    return None


def by_version(names):
    """Directory names in version order, oldest first: a name sort puts 1.9.1 after 1.12.0 (a session in guin-site took
    `ls -d …/dwitbuk/* | tail -1` for the newest and ran 1.9.1 for days). Names that are not versions go first, by name."""
    return sorted(names, key=lambda v: (_semver(v) is not None, _semver(v) or (), v))


def _installed(target, name, market=None):
    """(root, the version the install record names) for what this host has installed for `target` — the copy a new session
    would load — or (None, None). Two small reads, no survey. The root is written with `/` on every platform."""
    if HOST == "codex":
        cache = os.path.join(CODEX, "plugins", "cache", market or "", name)
        versions = by_version(os.listdir(cache)) if market and os.path.isdir(cache) else []
        return (os.path.join(cache, versions[-1]).replace("\\", "/"), versions[-1]) if versions else (None, None)
    installed = load_json(os.path.join(CLAUDE, "plugins", "installed_plugins.json")).get("plugins", {})
    key = "%s@%s" % (name, market) if market else next((k for k in sorted(installed) if k.partition("@")[0] == name), None)
    if not key or not installed.get(key):
        return None, None
    inst = install_entry(installed[key], target)
    markets = load_json(os.path.join(CLAUDE, "plugins", "known_marketplaces.json"))
    root, _ = host_plugin_root(markets.get(key.partition("@")[2]), name, inst)
    return (root.replace("\\", "/") if root else None), (inst or {}).get("version")


def installed_root(target, name, market=None):
    """The root this host has installed for `target` — the copy a new session would load — or None."""
    return _installed(target, name, market)[0]


def installed_version(target, name, market=None):
    """What this host has installed for `target` — the copy a new session would load."""
    root, recorded = _installed(target, name, market)
    return (plugin_version_at(root) or recorded) if root else None


def stale_session_line(target, root):
    """A session loads its plugins once, at start; an update mid-session says "restart to apply", and `/clear` does not reload.
    `check` compares the lock with what is installed — not with what this session runs. hunsu's own hook knows the latter: the
    version at its plugin root. One line when it differs from the lock's hunsu (or from what is installed here), else None.
    Cheap enough for every prompt: the lock, the host's install record, a plugin.json or two."""
    running = plugin_version_at(root)
    if not running or "hunsu" in (load_json(os.path.join(target, LOCAL)).get("dev") or {}):   # a working source is run on purpose
        return None
    locked = ((load_json(os.path.join(target, LOCK)).get("plugins") or {}).get("hunsu") or {})
    tail = " — the plugins updated since this session started are not loaded: restart the session (a /clear does not reload plugins)"
    have = installed_version(target, "hunsu", locked.get("marketplace"))
    if locked.get("version") and have and have != locked["version"]:
        # a restart alone loads what is installed, and that is not the lock's either: say the whole way, in order
        newer = _semver(have) and _semver(locked["version"]) and _semver(have) > _semver(locked["version"])
        goal = have if newer else locked["version"]
        pinned = (load_json(os.path.join(target, MANIFEST)).get("plugins") or {}).get("hunsu", {}).get("version")
        steps = update_steps("hunsu", locked.get("marketplace"), goal, installed=have, pinned=pinned, locked=locked["version"],
                             scope=install_scope(target, "hunsu", locked.get("marketplace")), linked="hunsu" in development_links(target, load_json(os.path.join(target, MANIFEST))))
        if goal == have and running != have:
            steps.insert(0, "restart the session (a /clear does not reload plugins)")
        return ("hunsu: this session runs hunsu %s while hunsu.lock.json pins %s and hunsu %s is installed for this project — %s"
                % (running, locked["version"], have, steps_text(steps)))
    if locked.get("version") and locked["version"] != running:
        return "hunsu: this session runs hunsu %s while hunsu.lock.json pins %s" % (running, locked["version"]) + tail
    if have and have != running:
        return "hunsu: this session runs hunsu %s while hunsu %s is installed for this project" % (running, have) + tail
    return None


def locked_roots(target):
    """{name: (locked version, root on this machine)} for the lock's plugins — the copy a session should call an engine from:
    the one installed for this project, or the working source while `hunsu dev` has it. The root is None when it is not
    there, or when what is installed is not the version the lock pins (then no copy here is the lock's: `check` says so)."""
    try:
        plugins = load_json(os.path.join(target, LOCK)).get("plugins") or {}
        dev = load_json(os.path.join(target, LOCAL)).get("dev") or {}
    except (ValueError, OSError, AttributeError):
        return {}
    if not isinstance(plugins, dict) or not isinstance(dev, dict):
        return {}
    out = {}
    for name, p in sorted(plugins.items()):
        if not isinstance(p, dict):
            continue
        root = installed_root(target, name, dev.get(name) or p.get("marketplace"))
        if root and not os.path.isdir(root):
            root = None
        if root and name not in dev and p.get("version") and plugin_version_at(root) not in (None, p.get("version")):
            root = None
        out[name] = (p.get("version"), root)
    return out


def roots_line(target):
    """The SessionStart line naming where the lock's plugins are on this machine. A session that calls an engine by hand
    finds it with a glob over the plugin cache, and a glob finds every version kept there (guin-site, 10-08 → 10-10: mangsang
    1.11.0 ran for two days under a 1.16.0 lock, its version written into 40 sources). None without a lock."""
    roots = [(n, v, r) for n, (v, r) in locked_roots(target).items() if r]
    if not roots:
        return None
    return ("hunsu: the lock's plugins on this machine — call an engine at these roots, never through a glob over the plugin cache "
            "(it holds every version this machine kept): " + " · ".join("%s %s `%s`" % (n, v or "?", r) for n, v, r in roots))


CACHE_PATH = re.compile(r"plugins/cache/([^/\s\"'`;|&()<>]+)/([^/\s\"'`;|&()<>]+)/([^/\s\"'`;|&()<>]+)")
ENGINE_EXTS = (".py", ".js", ".mjs", ".cjs", ".sh", ".ps1")   # a plugin's own top-level file a command can run
INTERPRETER = re.compile(r"^(?:python[\d.]*|py|pypy3?|node|deno|bun|bash|sh|zsh|dash|pwsh|powershell)(?:\.exe)?$")
NO_FILE = ("-m", "-c", "-e", "--eval", "-p", "--print", "-Command")   # an interpreter told to run no file
PREFIXES = ("env", "exec", "time", "nohup", "command", "do", "then", "else", "{", "(", "!")
ASSIGNMENT = re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+)?([A-Za-z_]\w*)=(.*?)\s*$", re.S)
FOR_LOOP = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\s+(.*?)\s*$", re.S)
ONE_WORD = re.compile(r"^(?:\$\(.*\)|`.*`|\"[^\"]*\"|'[^']*'|\S*)$", re.S)
VAR_HEAD = re.compile(r"^\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))(/|$)")


def shell_segments(command):
    """A command line cut into simple commands at `;`, `&&`, `||`, `|`, `&` and newlines — outside quotes, `$(…)` and
    backquotes, so `D=$(ls -d … | tail -1)` stays one segment. Rough on purpose: what it cannot read stays whole."""
    segs, cur, quote, depth, i = [], [], None, 0, 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(ch)
                i += 1
                ch = command[i]
        elif ch in "'\"`":
            quote = ch
        elif ch == "$" and command[i + 1:i + 2] == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif not depth and (ch in ";|&\n"):
            segs.append("".join(cur))
            cur = []
            while i + 1 < len(command) and command[i + 1] in "|&":
                i += 1
            i += 1
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def _words(seg):
    import shlex
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def _runs(words):
    """The script a simple command runs — an interpreter's first operand, or the command word itself when it names an
    engine file — or None. `python3 -m mod`, `node -e …` run no file."""
    while words and (ASSIGNMENT.match(words[0]) or words[0] in PREFIXES):
        words = words[1:]
    if words[:2] == ["uv", "run"]:
        words = [w for w in words[2:3] if not w.startswith("-")] + words[3:]
    if not words:
        return None
    head = os.path.basename(words[0])
    if INTERPRETER.match(head):
        rest = words[1:]
        if head in ("deno", "bun") and rest[:1] == ["run"]:
            rest = rest[1:]
        while rest and rest[0].startswith("-"):
            if rest[0] in NO_FILE:
                return None
            rest = rest[1:]
        return rest[0] if rest else None
    return words[0] if words[0].endswith(ENGINE_EXTS) else None


def stray_plugin_paths(target, command):
    """[(plugin, what the command names, locked version, right root)] for each place a shell command runs a copy of a locked
    plugin other than the one the lock pins — a version in the plugin cache that is not the lock's (or another marketplace's
    copy), or a glob in the version's place. Only what a simple command runs is read: the script an interpreter is given,
    an engine file run as the command, a script run after `cd` into a copy, or `$D/…` where an earlier `D=` (or `for D in`)
    took a copy (`D=$(ls -d …/dwitbuk/* | tail -1); python3 $D/dwitbuk.py`). Listing, reading, comparing old copies, or
    passing one to a project's own script, harms nothing and passes. A path that names no plugin of the lock passes."""
    if not isinstance(command, str) or "/" not in command.replace("\\", "/"):
        return []
    roots = {n: (v, r) for n, (v, r) in locked_roots(target).items() if r}
    if not roots:
        return []
    tops = {}
    for name, (_, r) in roots.items():
        try:
            tops[name] = {f for f in os.listdir(r) if f.endswith(ENGINE_EXTS)}
        except OSError:
            tops[name] = set()

    def stray(text):
        """[(name, token)] where `text` reaches a copy that is not the lock's."""
        out = []
        for m in CACHE_PATH.finditer(text):
            market, name, seg = m.groups()
            if name in roots and not roots[name][1].rstrip("/").endswith("/".join(m.groups())):
                out.append((name, m.group(0)))
        return out

    def stray_script(path, assigned):
        if not path:
            return []
        path = path.replace("\\", "/")
        found = stray(path)
        if found:
            return found
        head = VAR_HEAD.match(path)
        if head and assigned.get(head.group(1) or head.group(2)):
            found = assigned[head.group(1) or head.group(2)]
            if found:
                return [(n, "%s (%s)" % (path, t)) for n, t in found]
        parts = path.split("/")
        if len(parts) < 3:
            return []
        for name, files in tops.items():
            if parts[-1] not in files:
                continue
            if name in parts[:-1]:
                way = parts[parts.index(name) + 1:-1]
            elif head and os.path.splitext(parts[-1])[0] == name:   # `$C/<market>/*/*/<name>.py`: the cache's prefix in a variable
                way = parts[1:-1]
            else:
                continue
            right = os.path.basename(roots[name][1].rstrip("/"))
            if any(re.search(r"[*?\[]", x) or (_semver(x) and x != right) for x in way):
                found.append((name, path))
        return found

    command = command.replace("\\", "/")
    assigned, cd_into, found = {}, [], []
    for seg in shell_segments(command):
        a = ASSIGNMENT.match(seg)
        a = a if a and ONE_WORD.match(a.group(2)) else None   # `X=1 python3 …` is a command with its environment
        f = FOR_LOOP.match(seg)
        if a or f:
            name, value = (a or f).groups()
            assigned[name] = stray(value)
            continue
        words = _words(seg)
        if words[:1] == ["cd"]:
            cd_into = stray_script(words[1], assigned) if len(words) > 1 else []
            continue
        script = _runs(words)
        if not script:
            continue
        hit = stray_script(script, assigned)
        if not hit and cd_into and not script.startswith(("/", "~", "$")):
            hit = cd_into
        found += [(n, t, roots[n][0], roots[n][1]) for n, t in hit]
    seen, out = set(), []
    for x in found:
        if x[:2] not in seen:
            seen.add(x[:2])
            out.append(x)
    return out


def stray_paths_reason(found):
    return "\n".join("hunsu: `%s` is not the %s this project runs — hunsu.lock.json pins %s %s, which this machine has at `%s`. Call the "
                     "engine there: the plugin cache keeps every version this machine installed, and a glob over it picks by name order "
                     "(1.9.1 after 1.12.0)." % (tok, name, name, v or "?", root)
                     for name, tok, v, root in found)


CODEX_HOOK_PROVENANCE = "written by hunsu lock; managed hooks: "
# said in a stale-generated-hook error that a Codex `lock` repairs by regenerating: lock and compose on Codex do not stop for it
REGENERATES = "`hunsu upgrade --hooks-only` (on either host) or, on Codex (`AGENT_HOST=codex` from a shell), `hunsu lock` regenerates hunsu's groups from the installed copies and keeps the project's own"
SCRIPT_EXTS = (".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".rb", ".pl", ".bat", ".cmd")


def codex_hook_provenance(doc):
    """(record, legacy) for a .codex/hooks.json document: record is hunsu's provenance (`{"hooks": {event: [group]}}`, and the
    project's own `description` when it had one) or None; legacy is True for a file marked as hunsu's with no provenance (before
    1.15.1). A provenance that does not parse raises ValueError, KeyError or TypeError."""
    description = doc.get("description", "")
    if not isinstance(description, str) or not description.startswith("written by hunsu lock"):
        return None, False
    if not description.startswith(CODEX_HOOK_PROVENANCE):
        return None, True
    record = json.loads(description[len(CODEX_HOOK_PROVENANCE):])
    managed = record["hooks"]
    if not isinstance(managed, dict) or any(not isinstance(groups, list) for groups in managed.values()):
        raise ValueError("invalid managed hooks")
    return record, False


SHELL_EVAL = re.compile(r"^(?:-[A-Za-z]*c|/[cC]|-Command|-command|--command|-EncodedCommand|-e|--eval)$")   # the next word is code, not a file
OPAQUE = re.compile(r"[$`*?\[\]{};|&<>()\n]")   # a variable, a glob, a substitution, a separator: what the word names is a shell's to decide


def hook_paths(command):
    """The files a hook command names that must exist: its words as a shell would split them (shlex — a tokenizer, never a
    shell), with only a leading `~`, `$HOME` or `${HOME}` expanded; an absolute word that is the first word (the program) or ends
    in a script extension — a quoted path with spaces is one word, and one file. Nothing is run. Opaque, never judged: a word
    with any other variable, a glob, a substitution or a separator; the word after `-c` (and the like), which is code; and the
    whole command when its quotes do not close."""
    import shlex
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    home, out = os.path.expanduser("~"), []
    for i, word in enumerate(words):
        if i and SHELL_EVAL.match(words[i - 1]):
            continue
        word = re.sub(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)", lambda m: home, word)
        if word and not OPAQUE.search(word) and os.path.isabs(word) and (i == 0 or word.lower().endswith(SCRIPT_EXTS)):
            out.append(word)
    return out


def codex_cache_copy(path):
    """(name, version, the newest version the cache holds now or None, whether that version's copy is still there) when `path`
    lies in a plugin copy in this machine's Codex plugin cache (`<cache>/<market>/<name>/<version>/…`), else None."""
    cache = os.path.join(CODEX, "plugins", "cache").replace("\\", "/").rstrip("/") + "/"
    parts = path.replace("\\", "/")[len(cache):].split("/") if path.replace("\\", "/").startswith(cache) else []
    if len(parts) < 4:
        return None
    market, name, version = parts[:3]
    folder = os.path.join(CODEX, "plugins", "cache", market, name)
    versions = by_version([v for v in os.listdir(folder) if os.path.isdir(os.path.join(folder, v))]) if os.path.isdir(folder) else []
    return name, version, (versions[-1] if versions else None), version in versions


def codex_lock_render(target, manifest, s):
    """What `hunsu lock` on Codex would write as hunsu's groups now — [(plugin, version, event, group, versioned_group)] from the copies Codex has installed
    for the manifest's plugins, rendered as `materialize_codex_hooks` renders them — or None when this machine's Codex registry
    cannot be read from here. On Codex the survey is Codex's; on Claude Code, Codex's registry is read for it (read-only)."""
    if HOST == "codex":
        codex = s
    else:
        try:
            codex = survey_codex(target)
        except (SystemExit, OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    out = []
    for name, want in (manifest.get("plugins") or {}).items():
        plugin = enabled(codex, name, want)
        direct = codex_plugin_hooks([plugin]) if plugin else {}
        for event, groups in (codex_plugin_hooks([plugin], target).items() if plugin else ()):
            out += [(name, plugin.get("version"), event, g, old) for g, old in zip(groups, direct[event])]
    return out


def codex_hooks_shape(doc):
    """Why a .codex/hooks.json document is not a hooks file hunsu can read, or None: an object whose `hooks` (absent or null:
    none) is an object of event -> list of group objects, each group's `hooks` a list of hook objects whose `command`, when
    there, is a string. One rule on both hosts."""
    if not isinstance(doc, dict):
        return "not a JSON object"
    events = doc.get("hooks")
    events = {} if events is None else events
    if not isinstance(events, dict):
        return "`hooks` is not an object"
    for event, groups in events.items():
        if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
            return "`hooks.%s` is not a list of groups" % event
        if not all(isinstance(g.get("hooks", []), list) and all(isinstance(h, dict) for h in g.get("hooks", [])) for g in groups):
            return "a group in `hooks.%s` has `hooks` that is not a list of hooks" % event
        if not all(isinstance(h.get("command", ""), str) for g in groups for h in g.get("hooks", [])):
            return "a hook in `hooks.%s` has a `command` that is not a string" % event
    return None


def codex_hooks_unreadable(why):
    return "codex hooks: .codex/hooks.json is not a hooks file hunsu can read (%s) — restore it; `lock` on Codex refuses until then" % why


def group_paths(group):
    """hook_paths of every command in a hooks.json group."""
    hooks = group.get("hooks") if isinstance(group, dict) and isinstance(group.get("hooks"), list) else []
    return [p for h in hooks for p in hook_paths(str(h.get("command", "")) if isinstance(h, dict) else "")]


def codex_hook_findings(target, manifest, s):
    """[(severity, text, repaired)] for the project's .codex/hooks.json as Codex will run it, read from either host when Codex runs
    anything here: a hook whose program or script is gone fails on every event it is on (guin-site 11-01: generated groups still
    named a removed 1.x copy, and Codex prompts were blocked until a Codex `lock` regenerated them; the Codex survey reads each
    plugin's own hooks/codex.json, never these materialized commands, so nothing else saw it). `repaired` is decided from what
    a Codex `lock` would write next (codex_lock_render): only a generated path that render no longer names, while every path the
    render names is here — never from a message's wording."""
    path = os.path.join(target, ".codex", "hooks.json")
    if "codex" not in hosts_in_use(target, manifest, s):
        return []
    home, out, reported, seen = os.path.expanduser("~"), [], set(), {}

    def render():
        """Once: {path: (plugin, version, event)} a Codex `lock` would write now, and whether that could be read here."""
        if not seen:
            groups = codex_lock_render(target, manifest, s)
            seen["groups"] = groups or []
            seen["readable"], seen["names"] = groups is not None, {}
            for name, version, event, g, direct in groups or []:
                for p in group_paths(direct):
                    seen["names"].setdefault(p, (name, version, event))
            seen["missing"] = [p for p in seen["names"] if not os.path.exists(p)]
        return seen["readable"], seen["names"], seen["missing"]

    doc, owned = {}, {}
    dispatchers, versioned, skew = {}, [], set()
    if os.path.isfile(path):
        try:
            doc = load_json(path)
        except ValueError:
            doc = None
        why = "not JSON" if doc is None else codex_hooks_shape(doc)
        if not why:
            try:
                record, legacy = codex_hook_provenance(doc)
            except (ValueError, KeyError, TypeError):
                why = "its hunsu provenance, the `description` hunsu lock wrote, does not parse"
        if why:
            return [("error", codex_hooks_unreadable(why), False)]
        owned = (record or {}).get("hooks") or {}
        for event, groups in (doc.get("hooks") or {}).items():
            mine = list(owned.get(event, []))
            for group_index, group in enumerate(groups):
                generated = group in mine
                if generated:
                    mine.remove(group)   # one owned occurrence, as `lock` counts them
                where = "%s%s" % (event, " [%s]" % group.get("matcher") if group.get("matcher") else "")
                dispatched = [d for h in group.get("hooks", []) if (d := dispatched_command(h.get("command", "")))]
                for dispatcher, key, words in dispatched:
                    dispatchers.setdefault(dispatcher, set()).add((event, group_index))
                    name = key.partition("@")[0]
                    DISPATCH = load_dispatch()
                    try:
                        root, version = DISPATCH.resolve(CODEX, key)
                    except DISPATCH.Absent as why:
                        repaired = generated and name not in manifest.get("plugins", {})
                        how = REGENERATES if repaired else "install it for Codex (`codex plugin add %s`)" % key
                        out.append(("error" if generated else "warning", "codex hook %s: dispatches to %s, which is not installed for Codex on this machine (%s) — the dispatcher stops this hook (exit 2) on every %s event; %s" % (where, key, why, event, how), repaired))
                        continue
                    if generated and HOST != "codex":
                        pin = load_json(os.path.join(target, LOCK)).get("plugins", {}).get(name, {}).get("version")
                        if pin and pin != version:
                            skew.add((name, version, pin))
                    for word in words:
                        if "{PLUGIN_ROOT}" not in word:
                            continue
                        script = word.replace("{PLUGIN_ROOT}", root)
                        if os.path.exists(script):
                            continue
                        readable, names, missing = render()
                        repaired = generated and readable and script not in names and not missing
                        if script in names:
                            reported.add(script)
                        how = REGENERATES if repaired else "reinstall it (`codex plugin remove` + `add`); a lock cannot repair it"
                        out.append(("error" if generated else "warning", "codex hook %s: dispatches to %s %s's %s, which that copy lacks — the dispatcher stops this hook (exit 2) on every %s event; %s" % (where, name, version, word.replace("{PLUGIN_ROOT}/", ""), event, how), repaired))
                direct_paths = [p for p in group_paths(group) if p not in [d[0] for d in dispatched]]
                for _, _, words in dispatched:
                    for script in hook_paths(shlex.join(words)):
                        if script not in direct_paths:
                            direct_paths.append(script)
                origins = [(p, codex_cache_copy(p)) for p in direct_paths if os.path.exists(p) and codex_cache_copy(p)]
                if origins:
                    if generated and all(os.path.exists(p) for p in direct_paths):
                        versioned.append((event, group, origins))
                    elif not generated:
                        script, copy = origins[0]
                        out.append(("warning", "codex hook %s: the project's own group runs %s, a versioned copy in the Codex plugin cache (%s %s) — it fails once Codex replaces that copy; hunsu keeps the project's own groups: fix it in .codex/hooks.json" % (where, script.replace(home, "~"), copy[0], copy[1]), False))
                for script in direct_paths:
                    if os.path.exists(script):
                        continue
                    shown, copy = script.replace(home, "~"), codex_cache_copy(script)
                    if not generated:
                        out.append(("warning", "codex hook %s: runs %s, which is not on this machine — the project's own group (%s), so `lock` keeps it: "
                                               "fix or remove it in .codex/hooks.json%s"
                                    % (where, shown, "the file predates hunsu's ownership record" if legacy else "not in hunsu's provenance",
                                       "; it names %s %s's copy in the Codex plugin cache — likely a group generated before hunsu recorded which "
                                       "groups it wrote (1.15.1): remove it by hand" % copy[:2] if copy else ""), False))
                        continue
                    readable, names, still_missing = render()
                    repaired = False
                    if not readable:
                        how = "whether a Codex `lock` would repair it cannot be told here (this machine's Codex registry does not read) — `hunsu check` on Codex"
                    elif script in names:
                        reported.add(script)
                        name, version, _ = names[script]
                        if copy and copy[1] == version:
                            how = "%s %s, installed for Codex, has no such file — reinstall it (`codex plugin remove` + `add`), then `hunsu lock` on Codex" % (name, version)
                        else:
                            how = ("a Codex `lock` would write it again: %s %s's hooks/codex.json names it — `lock` cannot repair it; install it on this "
                                   "machine, or the plugin's hook changes" % (name, version))
                    elif still_missing:
                        how = ("a Codex `lock` would drop it, but the groups it would write name %s, which is not on this machine either — `lock` cannot "
                               "repair that" % ", ".join(p.replace(home, "~") for p in still_missing))
                    else:
                        repaired = True
                        if not copy:
                            gone = ""
                        elif copy[3]:
                            gone = "it was generated from %s %s, whose copy in the Codex plugin cache lacks it; " % copy[:2]
                        elif copy[2]:
                            gone = "it was generated from %s %s, a copy no longer in the Codex plugin cache (which holds %s now); " % copy[:3]
                        else:
                            gone = "it was generated from %s %s, a copy gone, and the Codex plugin cache holds none of %s now; " % (copy[0], copy[1], copy[0])
                        how = "%sa Codex `lock` would not write it again: %s" % (gone, REGENERATES)
                    out.append(("error", "codex hook %s: runs %s, which is not on this machine — a group hunsu lock generated, so Codex fails this hook on "
                                         "every %s event; %s" % (where, shown, event, how), repaired))
    for dispatcher, groups in dispatchers.items():
        shown = dispatcher.replace(home, "~")
        if not os.path.exists(dispatcher):
            render()
            repaired = (os.path.abspath(dispatcher) == os.path.join(os.path.abspath(target), ".codex", "hunsu-dispatch.py")
                        and any(dispatched_command(h.get("command", "")) for _, _, _, g, _ in seen["groups"] for h in g.get("hooks", [])))
            how = REGENERATES + ", writing the dispatcher with the groups" if repaired else "fix or remove the groups that name it"
            out.append(("error", "codex hook dispatcher: %s is missing; %d group(s) run it and Codex fails each of them on every event; %s" % (shown, len(groups), how), repaired))
        else:
            with open(dispatcher, "rb") as stream, open(DISPATCH_SOURCE, "rb") as source:
                differs = stream.read() != source.read()
            if differs:
                out.append(("warning", "codex hook dispatcher: %s is not the one this hunsu writes; %s, writing the dispatcher with the groups" % (shown, REGENERATES), True))
    if versioned:
        render()
        labels = sorted({"%s %s (%s)" % (copy[0], copy[1], event) for event, _, origins in versioned for _, copy in origins})
        staying = sum(any(e == event and g == group for _, _, e, g, _ in seen["groups"]) for event, group, _ in versioned)
        text = "codex hooks: %d generated group(s) name versioned copies in the Codex plugin cache — %s; Codex replaces a copy when it takes a release and each such group then fails on every event; %s, rewriting them through .codex/hunsu-dispatch.py" % (len(versioned), ", ".join(labels), REGENERATES)
        if staying:
            text += "; %d group(s) stay versioned because their commands are not dispatchable simple commands" % staying
        out.append(("warning", text, False))
    if skew:
        out.append(("warning", "codex hooks: the dispatched groups run the copies installed for Codex — %s; the dispatcher runs them and says so at each hook" % ", ".join("%s %s (lock pins %s)" % entry for entry in sorted(skew)), False))
    # what a Codex `lock` would write next must run: on Codex (where `lock` writes, and the survey is Codex's) always — the first
    # lock too, with no file yet; on Claude Code when the render was read for a generated group above
    if HOST == "codex" or seen:
        readable, names, still_missing = render()
        for p in still_missing:
            if p in reported:
                continue
            name, version, event = names[p]
            copy = codex_cache_copy(p)
            out.append(("error", "codex hook %s: a Codex `lock` would write a group that runs %s, which is not on this machine — %s %s's hooks/codex.json "
                                 "names it%s; `lock` on Codex refuses until it is here"
                        % (event, p.replace(home, "~"), name, version,
                           " and its installed copy lacks the file: reinstall it (`codex plugin remove` + `add`)" if copy and copy[1] == version else ""), False))
    render()
    available = {event: list(groups) for event, groups in (doc.get("hooks") or {}).items()}
    absent = []
    for name, version, event, group, direct in seen["groups"]:
        remaining = available.setdefault(event, [])
        if group in remaining or direct in remaining:
            remaining.remove(group if group in remaining else direct)
        else:
            absent.append((name, version, event))
    if absent:
        plugins = {}
        for name, version, event in absent:
            events = plugins.setdefault((name, version), [])
            if event not in events:
                events.append(event)
        text = "codex hooks: %d hook group(s) the copies installed for Codex declare are not active in this project — " % len(absent)
        text += ", ".join("%s %s (%s)" % (name, version, ", ".join(events)) for (name, version), events in plugins.items())
        text += (": .codex/hooks.json lacks them" if os.path.isfile(path) else
                 ": .codex/hooks.json does not exist (hunsu generates it per machine and it is not committed, so a new clone or worktree has none)")
        current = {}
        for _, _, event, group, direct in seen["groups"]:
            current.setdefault(event, []).extend([group, direct])
        dropped, copies = 0, []
        for event, groups in owned.items():
            remaining = list(current.get(event, []))
            present = list((doc.get("hooks") or {}).get(event, []))
            for group in groups:
                if group not in present:
                    continue
                present.remove(group)
                if group in remaining:
                    remaining.remove(group)
                elif all(os.path.exists(p) for p in group_paths(group)):
                    dropped += 1
                    origins = [codex_cache_copy(p) for p in group_paths(group)]
                    labels = ["%s %s" % origin[:2] for origin in origins if origin] or ["another copy"]
                    for label in labels:
                        if label not in copies:
                            copies.append(label)
        if dropped:
            text += "; it also holds %d generated group(s) from %s that a regeneration drops" % (dropped, ", ".join(copies))
        text += " — `hunsu upgrade --hooks-only` writes them on either host (or `hunsu lock` on Codex), keeping the project's own groups"
        out.append(("error", text, HOST == "codex"))
    return out


def check_codex_hooks(target, manifest, s, found):
    errors, warnings, info = found
    for severity, text, _ in codex_hook_findings(target, manifest, s):
        (errors if severity == "error" else warnings).append(text)


def lock_blocking(target, manifest, s, errors):
    """The errors that stop `lock`: all of them, except — on Codex, where this lock regenerates hunsu's hook groups — the exact
    findings codex_hook_findings marks repaired by that regeneration."""
    if HOST != "codex":
        return errors
    repaired = {text for _, text, fixed in codex_hook_findings(target, manifest, s) if fixed}
    return [e for e in errors if e not in repaired]


def codex_plugin_hooks(plugins, target=None):
    """Render plugin hook groups exactly as Codex will run them on this machine."""
    merged = {}
    for p in plugins:
        if not p:
            continue
        declared = load_json(os.path.join(p["path"], "hooks", "codex.json")).get("hooks") or {}
        for event, groups in declared.items():
            for g in groups:
                # the interpreter the command names is resolved to the one this machine has (macOS ships python3 only; some hosts only python)
                text = json.dumps(g).replace("${PLUGIN_ROOT}", p["path"].replace("\\", "/"))
                py = "python3" if shutil.which("python3") else "python"
                text = re.sub(r'(?<=")python3? (?=\\")', py + " ", text)
                rendered = json.loads(text)
                if target is not None:
                    for original, hook in zip(g.get("hooks", []), rendered.get("hooks", [])):
                        command = original.get("command", "")
                        try:
                            words = shlex.split(command)
                        except ValueError:
                            words = []
                        if "${PLUGIN_ROOT}" in command and len(shell_segments(command)) == 1 and words and not ASSIGNMENT.match(words[0]):
                            command = re.sub(r"^python3?\s", py + " ", command).replace("${PLUGIN_ROOT}", "{PLUGIN_ROOT}")
                            dispatcher = os.path.join(os.path.abspath(target), ".codex", "hunsu-dispatch.py").replace("\\", "/")
                            quoted = shlex.quote(dispatcher) if any(c in dispatcher for c in '\"$`\\!') else '"%s"' % dispatcher
                            hook["command"] = '%s %s %s %s' % (py, quoted, shlex.quote(p["name"] + "@" + p["marketplace"]), command)
                merged.setdefault(event, []).append(rendered)
    return merged


def codex_project_hooks(target, plugins):
    """Remove only previously generated groups, preserving the project's own additions and edits.

    Provenance lives in Codex's supported description field so hooks and ownership change in one atomic write. Older
    files have only a marker: subtract exact matches to known plugin groups, retaining anything we cannot identify.
    """
    try:
        doc = load_json(os.path.join(target, ".codex", "hooks.json"))
    except ValueError:
        raise SystemExit(codex_hooks_unreadable("not JSON"))
    if codex_hooks_shape(doc):
        raise SystemExit(codex_hooks_unreadable(codex_hooks_shape(doc)))
    try:
        record, legacy = codex_hook_provenance(doc)
    except (ValueError, KeyError, TypeError):
        raise SystemExit(codex_hooks_unreadable("its hunsu provenance, the `description` hunsu lock wrote, does not parse"))
    if record is None and not legacy:
        return doc
    if record is not None:
        managed = record["hooks"]
        if "description" in record:
            doc["description"] = record["description"]
        else:
            doc.pop("description", None)
    else:
        managed = codex_plugin_hooks(plugins.values())
        doc.pop("description", None)
    own = {}
    for event, groups in (doc.get("hooks") or {}).items():
        remaining = list(groups)
        for generated in managed.get(event, []):
            if generated in remaining:
                remaining.remove(generated)   # one owned occurrence; identical project duplicates remain the project's
        if remaining:
            own[event] = remaining
    doc["hooks"] = own
    return doc


@mutates_project
def materialize_codex_hooks(target, manifest, s):
    """Keep project hooks and replace only hunsu's generated plugin groups."""
    doc = codex_project_hooks(target, s["plugins"])
    managed = codex_plugin_hooks((enabled(s, name, want) for name, want in manifest["plugins"].items()), target)
    record = {"hooks": managed}
    if "description" in doc:
        record["description"] = doc["description"]
    merged = doc.setdefault("hooks", {})
    for event, groups in managed.items():
        merged.setdefault(event, []).extend(groups)
    doc["description"] = CODEX_HOOK_PROVENANCE + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    out = os.path.join(target, ".codex", "hooks.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    ignored = [".codex/hooks.json"]
    if any(dispatched_command(h.get("command", "")) for groups in managed.values() for g in groups for h in g.get("hooks", [])):
        dispatcher = os.path.join(target, ".codex", "hunsu-dispatch.py")
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(out), prefix=".hunsu-dispatch.")
        try:
            with os.fdopen(fd, "wb") as stream, open(DISPATCH_SOURCE, "rb") as source:
                stream.write(source.read())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, dispatcher)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        ignored.append(".codex/hunsu-dispatch.py")
    save_json(out, doc)
    ensure_ignored(target, ignored)
    n = sum(len(v) for v in managed.values())
    print("codex: %d hook group(s) from the installed copies -> .codex/hooks.json (this machine's; run `codex` with hook trust, or exec with --dangerously-bypass-hook-trust)" % n)


def shareable(source):
    return source.startswith(("github:", "http"))


def installed_entry(found):
    """The shareable manifest entry for a copy installed on this host."""
    return {"version": found["version"], "source": found["source"] if shareable(found["source"]) else "unpublished",
            "marketplace": found["marketplace"]}


@mutates_project
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
    manifest["plugins"][name] = installed_entry(found)
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


@mutates_project
def cmd_upgrade(args):
    target = args.target
    path = os.path.join(target, MANIFEST)
    manifest = load_json(path)
    if not manifest:
        raise SystemExit("no %s — run `hunsu init` first" % MANIFEST)
    selected = sorted(set(args.plugins or manifest.get("plugins", {})))
    for name in selected:
        if name not in manifest.get("plugins", {}):
            raise SystemExit("%s is not in hunsu.json — nothing written" % name)
    s = survey(target)
    linked, changed = development_links(target, manifest), False
    all_links, moves = links(target), []
    if not args.hooks_only:
        for name in selected:
            want = manifest["plugins"][name]
            if name in linked:
                print("%s: linked to a development source here — not taken (`hunsu install --refresh %s`, then `hunsu add %s`)" % (name, name, name))
                continue
            if name in (s.get("dev") or {}):
                print("%s: in development here — not taken" % name)
                continue
            found = enabled(s, name, want)
            if found is None:
                print("%s: not installed on this host (%s) — not taken" % (name, HOST))
                continue
            installed, pinned = found.get("version"), want.get("version")
            iv, pv = _semver(installed), _semver(pinned)
            if iv is None or pv is None:
                print("%s: installed %s, pinned %s — not release versions, not taken (`hunsu add %s` decides)" % (name, installed, pinned, name))
            elif iv > pv:
                if not shareable(found["source"]):
                    print("%s: installed from a local source — not taken (`hunsu add %s` decides)" % (name, name))
                    continue
                copy_link = installed_copy_link(name, want.get("marketplace"), all_links[name]) if name in all_links else None
                if copy_link:
                    host, cv, _ = copy_link
                    destination = os.path.join(CLAUDE if host == "claude-code" else CODEX,
                                               "plugins", "cache", want["marketplace"], name, installed).replace(os.sep, "/")
                    doc = plugin_manifest(destination) if os.path.isdir(destination) else None
                    if not doc or doc.get("name") != name or doc.get("version") != installed:
                        print("%s: pinned %s, installed %s for %s — not taken: this machine links %s to %s's installed copy %s (%s), and %s has no copy of %s %s — install %s for %s (then `hunsu upgrade %s`), or `hunsu unlink %s`"
                              % (name, pinned, installed, HOST, name, host, cv, all_links[name], host, name, installed, installed, host, name, name))
                        continue
                    if os.path.realpath(all_links[name]) != os.path.realpath(destination):
                        moves.append((name, host, installed, destination))
                manifest["plugins"][name] = installed_entry(found)
                changed = True
                print("%s %s -> %s (installed for %s)" % (name, pinned, installed, HOST))
            elif iv == pv:
                print("%s %s: at the pin" % (name, pinned))
            else:
                print("%s: pinned %s, installed %s — not taken: upgrade takes only a newer release installed here (`hunsu check` says how to install %s)" % (name, pinned, installed, pinned))
    hosts = hosts_in_use(target, manifest, s)
    codex_s = None
    if "codex" in hosts:
        try:
            codex_s = s if HOST == "codex" else survey_codex(target)
        except SystemExit as why:
            if str(why).startswith("codex hooks:"):
                raise
            raise SystemExit("Codex's registry does not read here — run `hunsu upgrade` on Codex — nothing written")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise SystemExit("Codex's registry does not read here — run `hunsu upgrade` on Codex — nothing written")
        for name, want in sorted(manifest.get("plugins", {}).items()):
            found = enabled(codex_s, name, want)
            if not found:
                print("codex: %s is not installed for Codex — no groups for it" % name)
                continue
            for groups in codex_plugin_hooks([found]).values():
                for group in groups:
                    for script in group_paths(group):
                        if not os.path.exists(script):
                            raise SystemExit("upgrade: nothing written — the generated hooks would run %s, which is not on this machine: %s %s's installed Codex copy lacks it — reinstall it" %
                                             (script.replace(os.path.expanduser("~"), "~"), name, found["version"]))
            if found["version"] != want["version"]:
                print("codex: %s's groups come from the installed %s; the pin is %s" % (name, found["version"], want["version"]))
    else:
        print("codex: not a host here — nothing is written for Codex")
    if "claude-code" in hosts:
        print("claude-code: a plugin's hooks run from its installed copy (hooks/hooks.json); nothing is generated")
    if changed:
        save_json(path, manifest)
    for name, host, installed, destination in moves:
        set_link(target, name, destination)
        print("%s: the link moves to %s's installed copy %s (%s)" % (name, host, installed, destination))
    if codex_s is not None:
        managed = codex_plugin_hooks(enabled(codex_s, name, want) for name, want in manifest["plugins"].items())
        if not any(managed.values()) and not os.path.exists(os.path.join(target, ".codex", "hooks.json")):
            print("codex: no copy installed for Codex declares a hook and this project has no .codex/hooks.json — nothing is written for Codex")
        else:
            materialize_codex_hooks(target, manifest, codex_s)
    steps = []
    if manifest.get("judge") != "skip":
        _, stale = judgments_status(manifest, s, target)
        situations = stale.get("situations", [])
        if situations:
            steps.append("judge the %d situation(s) whose members' text changed — %s: `hunsu judge request --out DIR --stale`, judge those packets, `hunsu judge consume --dir DIR`" % (len(situations), "; ".join(situations)))
        if stale.get("unclustered"):
            steps.append("skills no situation has seen — %s: a cluster round `hunsu judge request --out DIR`" % ", ".join(stale["unclustered"]))
    if changed:
        steps.append("restart the session — a session keeps the plugins it started with (a /clear does not reload them)")
    steps.append("`hunsu lock` (it runs check first), and commit hunsu.json, hunsu.lock.json (and hunsu-judgments.json when it changed) inside a run")
    print("next:")
    for i, step in enumerate(steps, 1):
        print("%d. %s" % (i, step))
    return 0


# ---------------------------------------------------------------- links (this machine only)

LOCAL = "hunsu.local.json"
LOCAL_SETTINGS = ".claude/settings.local.json"   # the host's own machine-local settings: `hunsu dev` enables a working source there


def links(target):
    return load_json(os.path.join(target, LOCAL)).get("links", {})


def installed_copy_link(name, market, link):
    """Identify a link by the actual cache directory it names, on either host."""
    if not market or not os.path.isdir(link):
        return None
    real = os.path.realpath(link)
    for host, base in (("claude-code", CLAUDE), ("codex", CODEX)):
        cache = os.path.join(base, "plugins", "cache", market, name)
        try:
            versions = sorted(os.listdir(cache))
        except OSError:
            continue
        for version in versions:
            root = os.path.join(cache, version)
            if os.path.isdir(root) and os.path.realpath(root) == real:
                doc = plugin_manifest(root)
                if doc is not None and doc.get("name") == name:
                    return host, doc.get("version") or version, root.replace(os.sep, "/")
    return None


def development_links(target, manifest):
    """Machine-local links excluding copies installed in the declared marketplace's caches."""
    return {name: link for name, link in links(target).items()
            if installed_copy_link(name, (manifest.get("plugins", {}).get(name) or {}).get("marketplace"), link) is None}


PLUGIN_MANIFESTS = (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"), "plugin.json")
NESTED_MARKETPLACES = 2   # how deep a marketplace inside a marketplace is read (an umbrella directory holding a repository root)


def catalogs():
    """The marketplace catalogs a directory may hold, this host's first: Claude Code reads `.claude-plugin/marketplace.json`,
    Codex `.agents/plugins/marketplace.json`. Paths are `/`-separated, as inside a git tree."""
    claude, codex = ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"
    return (codex, claude) if HOST == "codex" else (claude, codex)


def catalog_entry(read, name):
    """The entry for `name` in the first catalog, in this host's order, that lists it — `read(catalog path)` gives a catalog's
    document ({} when there is none) — or None when no catalog lists it."""
    for cat in catalogs():
        try:
            doc = read(cat)
        except (OSError, ValueError):
            continue
        for entry in (doc.get("plugins") or []) if isinstance(doc, dict) and isinstance(doc.get("plugins"), list) else []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
    return None


URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")   # `https:`, `file:`, `git+ssh:` … — an address, never a path here


def catalog_path(entry):
    """The directory a catalog entry gives its plugin, as written: a string source (Claude Code's `"./plugin"`), or a local
    object's `path` (Codex's `{"source": "local", "path": "./plugin"}`). None for a source that is no directory of this
    marketplace (`github`, `url`, `git-subdir`, …) — and for one written as a URI (`"https://…"`, `"file:…"`), which
    normalized as a path would name a local `https:/…` directory a catalog never meant."""
    src = (entry or {}).get("source")
    if isinstance(src, dict) and src.get("source") == "local":
        src = src.get("path")
    elif isinstance(src, dict):
        return None
    if not isinstance(src, str) or URI_SCHEME.match(src.strip()):
        return None
    return src


def confined(base, rel):
    """`rel` joined to the directory `base` when it stays inside it, else None: not absolute, not leaving with `..`, and its
    real path (symlinks resolved) inside the real path of `base`. A catalog or a guess never takes a plugin from elsewhere."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    norm = os.path.normpath(rel.replace("\\", "/"))
    if os.path.isabs(norm) or os.path.splitdrive(norm)[0] or norm == os.pardir or norm.startswith(os.pardir + os.sep):
        return None
    path = os.path.normpath(os.path.join(base, norm))
    real, inside = os.path.realpath(path), os.path.realpath(base)
    return path if real == inside or real.startswith(inside.rstrip(os.sep) + os.sep) else None


def plugin_manifest(root):
    """The first plugin manifest `root` holds (Claude Code's, Codex's, the root plugin.json) as a dict, or None when it holds none."""
    for m in PLUGIN_MANIFESTS:
        path = os.path.join(root, m)
        if os.path.isfile(path):
            try:
                doc = load_json(path)
            except (OSError, ValueError):
                return {}
            return doc if isinstance(doc, dict) else {}
    return None


def linked_source(link, name, _depth=0):
    """A link is a marketplace directory (a `hunsu link`, a `directory` marketplace, this machine's copy of a git one). The
    plugin's source inside it — the payload a host installs, never the development checkout around it:

    - a catalog that lists `name` (this host's first) decides alone: its entry's directory (a string source, or Codex's
      `source.path`), confined to the link. When that directory holds no plugin manifest the answer is None — never the root
      that declared it: a repository whose `plugin/` is missing must not be read as the plugin, with its hunsu.json, tests
      and records (2.2.0: the payload moved to `plugin/`, the root kept the development checkout);
    - no catalog lists it: `<link>/<name>`, else the link itself (the root layout of 2.1.0 and earlier), confined too;
    - a candidate whose manifest names another plugin is not this one; a candidate with no manifest that is a marketplace
      itself (a repository root inside an umbrella directory) is read the same way, NESTED_MARKETPLACES levels at most.

    None when nothing resolves (then there is nothing to compare)."""
    if not isinstance(link, str) or not os.path.isdir(link):
        return None
    entry = catalog_entry(lambda cat: load_json(os.path.join(link, *cat.split("/"))), name)
    if entry is not None:
        candidates = [confined(link, catalog_path(entry))]
    else:
        candidates = [confined(link, name), link]
    for c in candidates:
        if not c or not os.path.isdir(c):
            continue
        manifest = plugin_manifest(c)
        if manifest is not None:
            if manifest.get("name") in (None, name):
                return c
            continue
        if _depth < NESTED_MARKETPLACES and os.path.realpath(c) != os.path.realpath(link):
            inner = linked_source(c, name, _depth + 1)
            if inner:
                return inner
    return None


def source_version(root):
    """The version a plugin source declares, from whichever manifest copy it has."""
    for m in PLUGIN_MANIFESTS:
        v = load_json(os.path.join(root, m)).get("version")
        if v:
            return v
    return None


def tree_fingerprint(root):
    """Identity of a plugin's content: every file's path and bytes, minus what the interpreter, the OS and the host leave
    behind (Claude Code marks a cached copy it runs with `.in_use`; Codex writes the root file
    `.codex-marketplace-install.json` as an installer receipt). A version is the name of one content; this is how two
    copies under one name are told apart."""
    import hashlib
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", ".in_use"))   # the host's marker is a file on some versions, a folder of pids on others
        for f in sorted(files):
            if f.endswith(".pyc") or f in (".DS_Store", ".in_use"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
            if rel == ".codex-marketplace-install.json":
                continue
            h.update(rel.encode("utf-8") + b"\0")
            with open(os.path.join(dirpath, f), "rb") as fh:
                h.update(fh.read().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()[:12]


@mutates_project
def set_link(target, plugin, path):
    doc = load_json(os.path.join(target, LOCAL)) or {"links": {}}
    linked = doc.setdefault("links", {})
    if path is None:
        linked.pop(plugin, None)
    else:
        linked[plugin] = path.replace(os.sep, "/")
    save_json(os.path.join(target, LOCAL), doc)


@mutates_project
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
        # a directory marketplace that resolves the plugin the way the host would: a repository root (its catalog's ./plugin),
        # an umbrella holding the repository at <dir>/<name> (either layout) — not one whose only claim leaves its directory
        market = args.market or next((m for m, rec in markets.items() if (rec.get("source") or {}).get("source") == "directory"
                                      and linked_source((rec.get("source") or {}).get("path", ""), name)), None)
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


@mutates_project
def ensure_ignored(target, entries):
    path = os.path.join(target, ".gitignore")
    lines = io.open(path, encoding="utf-8").read().split("\n") if os.path.exists(path) else []
    missing = [e for e in entries if e not in lines]
    if missing:
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(("" if not lines or lines[-1] == "" else "\n") + "\n".join(missing) + "\n")


@mutates_project
def cmd_link(args):
    manifest = load_json(os.path.join(args.target, MANIFEST))
    path = args.path or (enabled(survey(args.target), args.plugin, manifest.get("plugins", {}).get(args.plugin)) or {}).get("source")
    if not path or shareable(path):
        raise SystemExit("%s: no local path to link — give one, or it is not installed from a local marketplace" % args.plugin)
    set_link(args.target, args.plugin, path)
    print("linked %s -> %s (%s, not committed)" % (args.plugin, path, LOCAL))
    return 0


@mutates_project
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


@mutates_project
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
    errors = lock_blocking(target, manifest, s, errors)
    if errors:
        return stop("resolve these errors (resolutions[<situation>]; install missing plugins; raise engines; re-run judge if stale), then re-run compose", *errors)
    undecided = undecided_capabilities(manifest)
    if undecided:
        # asked once, at setup — so the harness never stops mid-run to ask whether a hired hand may bind a port
        return stop("do hired hands need a local port (loopback: a dev server like `wrangler dev`) or the network? answer once with one of "
                    "the commands below for each line, then re-run compose",
                    *(capability_question(*u) for u in undecided))
    judged, _ = judgments_status(manifest, s, target)
    if judged is None and manifest.get("judge") != "skip":
        return stop("no conflict judgment yet: run `hunsu judge request --out DIR`, the judge on each packet (judge_worker.py), `hunsu judge consume --dir DIR`, then resolutions — or set \"judge\": \"skip\" in hunsu.json to lock without one (recorded)")
    user_hooks = [w for w in warnings if w.startswith(("user ", "unmanaged plugin "))]
    if user_hooks:
        # Acknowledged = listed in local-hooks-ok. One mechanism; no separate "reviewed" flag.
        # sagoforce 10-26: an Orca agent hook that prints `{}` stopped the session twice for the person, who said "ignore it if
        # it can be ignored" — then the agent read its 86 lines and listed it. Reading it is the session's; deciding is the
        # person's only where something can change what the session is told
        return stop("user-level hooks and skills, and plugins outside the manifest, run here and are not the project's. Read the file each user hook "
                    "below runs on this machine: one that adds nothing to the session (prints nothing or `{}` — no context, no permission decision) "
                    "is this machine's own tool — list it in `local-hooks-ok` in hunsu.local.json (only theirs, not committed) and tell the person in "
                    "one line what it is and where it sends what it reads. Ask the person only about the rest — a hook that can change the session, a "
                    "user skill, a plugin outside the manifest: `local-hooks-ok` in hunsu.json (the whole team runs it), in hunsu.local.json, or "
                    "removed from their settings — then re-run compose",
                    *user_hooks)
    return cmd_lock(args)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hunsu", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("survey", "init", "add", "upgrade", "remove", "check", "policy", "link", "unlink", "dev", "lock", "compose", "install", "judge", "outdated"):
        p = sub.add_parser(name, **({"help": POLICY_HELP, "description": POLICY_HELP} if name == "policy" else
                                    {"help": OUTDATED_HELP, "description": OUTDATED_HELP} if name == "outdated" else {}))
        if name == "check":
            p.add_argument("--findings", action="store_true", help="print drift and unread resolutions as dwitbuk/findings@1 JSON")
            p.add_argument("--remote", action="store_true", help="then `outdated`: ask each locked plugin's marketplace what it publishes now (network, read-only)")
        p.add_argument("--target", default=".")
        if name == "upgrade":
            p.add_argument("plugins", nargs="*")
            p.add_argument("--hooks-only", action="store_true")
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
    p = sub.add_parser("role", help=ROLE_HELP, description=ROLE_HELP)
    rsub = p.add_subparsers(dest="action", required=True)
    pa = rsub.add_parser("add", help="NAME@CAP[+CAP] --from ROLE --host HOST: ROLE's provider on HOST, as a new role in hunsu.json")
    pa.add_argument("name", metavar="NAME@CAP[+CAP]")
    pa.add_argument("--from", dest="source", default=None, metavar="ROLE", help="the role to copy (default: NAME)")
    pa.add_argument("--host", required=True, choices=WORKER_HOSTS + ("claude-code",), help="the host the alternate runs on")
    pa.add_argument("--model", default=None, help="the model on HOST (ROLE's is dropped: a model named for one host is not valid on another)")
    pa.add_argument("--effort", default=None, help="the effort on HOST (ROLE's is dropped)")
    pa.add_argument("--force", action="store_true", help="replace NAME@CAP if it exists")
    pa.add_argument("--target", default=".")
    pl = rsub.add_parser("list", help="read-only: the roles and their alternates")
    pl.add_argument("--target", default=".")
    p = sub.add_parser("capability", help=CAPABILITY_HELP, description=CAPABILITY_HELP)
    p.add_argument("caps", nargs="+", choices=sorted(CAPABILITIES), metavar="CAP", help="loopback (bind a local port) or network")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", action="store_true", help="the session does work a hired member's sandbox cannot")
    g.add_argument("--alternates", action="store_true", help="such work is hired on a `<role>@<cap>` role")
    p.add_argument("--host", default=None, choices=WORKER_HOSTS + ("claude-code",), help="with --alternates: add `<role>@<caps>` on HOST for every worker role on a host lacking CAP (as `role add`)")
    p.add_argument("--model", default=None, help="the alternates' model on HOST (the roles' own are dropped)")
    p.add_argument("--effort", default=None, help="the alternates' effort on HOST")
    p.add_argument("--target", default=".")
    args = parser.parse_args(argv)
    try:
        return {"role": cmd_role, "capability": cmd_capability, "survey": cmd_survey, "init": cmd_init, "add": cmd_add, "upgrade": cmd_upgrade, "check": cmd_check,
                "link": cmd_link, "unlink": cmd_unlink, "dev": cmd_dev, "lock": cmd_lock, "compose": cmd_compose,
                "install": cmd_install, "judge": cmd_judge, "remove": cmd_remove, "policy": cmd_policy, "outdated": cmd_outdated}[args.cmd](args)
    except ScratchUnavailable as why:
        command = args.cmd + (" " + args.mode if args.cmd == "judge" else "")
        print("hunsu: %s: %s" % (command, why), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
