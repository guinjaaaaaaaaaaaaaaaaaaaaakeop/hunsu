"""Self-check for hunsu. A fake host (settings, installed plugins, plugin cache) under a temp dir; every error branch fires once.

  python test_hunsu.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hunsu  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1) if not isinstance(data, str) else data)


def plugin(cache, market, name, version, skills, hooks=None, engines=None, roles=None):
    root = os.path.join(cache, market, name, version)
    write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": name, "version": version, **({"engines": engines} if engines else {}), **({"roles": roles} if roles else {})})
    for sk in skills:
        write(os.path.join(root, "skills", sk, "SKILL.md"), "---\nname: %s\ndescription: does %s\n---\n" % (sk, sk))
    if hooks:
        write(os.path.join(root, "hooks", "hooks.json"), {"hooks": hooks})
    return root


class Host:
    """A fake ~/.claude. Enter to point hunsu at it, exit to restore."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="hunsu-test-")
        self.claude = os.path.join(self.home, ".claude")
        self.cache = os.path.join(self.claude, "plugins", "cache")
        self.target = os.path.join(self.home, "project")
        os.makedirs(self.target)
        self.enabled, self.installed, self.markets, self.user_hooks = {}, {}, {}, {}

    def add_plugin(self, market, name, version, skills, hooks=None, source=None, engines=None, roles=None):
        root = plugin(self.cache, market, name, version, skills, hooks, engines, roles)
        key = "%s@%s" % (name, market)
        self.enabled[key] = True
        self.installed[key] = [{"installPath": root, "version": version}]
        self.markets[market] = {"source": source or {"source": "github", "repo": "org/" + market}}
        return root

    def flush(self):
        write(os.path.join(self.claude, "settings.json"), {"enabledPlugins": self.enabled, "hooks": self.user_hooks})
        write(os.path.join(self.claude, "plugins", "installed_plugins.json"), {"plugins": self.installed})
        write(os.path.join(self.claude, "plugins", "known_marketplaces.json"), self.markets)

    def __enter__(self):
        self.flush()
        self._old = hunsu.CLAUDE
        hunsu.CLAUDE = self.claude
        return self

    def __exit__(self, *a):
        hunsu.CLAUDE = self._old
        shutil.rmtree(self.home, ignore_errors=True)


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            code = hunsu.main(list(argv))
        except SystemExit as err:
            code = err.code if isinstance(err.code, int) else 1
            out.write(str(err) + "\n")
    return code, out.getvalue()


def manifest(target, **changes):
    path = os.path.join(target, hunsu.MANIFEST)
    doc = hunsu.load_json(path)
    doc.update(changes)
    hunsu.save_json(path, doc)



class Skip(Exception):
    """Raised by a test that cannot run on this host; the runner reports it as SKIP, never as PASS."""

def test_survey_lists_plugins_skills_hooks_and_modes():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "node x.js"}]}]})
        h.user_hooks = {"Stop": [{"hooks": [{"type": "command", "command": "python3 /home/FIXTURE/observe.py"}]}]}
        h.flush()
        s = hunsu.survey(h.target)
        assert s["plugins"]["alpha@m1"] == {"name": "alpha", "version": "1.0.0", "source": "github:org/m1", "marketplace": "m1", "path": s["plugins"]["alpha@m1"]["path"], "loaded-from": "install", "engines": {}, "roles": {}}
        assert [sk["name"] for sk in s["skills"]] == ["build"]
        assert {(x["event"], x["source"]) for x in s["hooks"]} == {("SessionStart", "plugin:alpha"), ("Stop", "user")}
        assert s["modes"] == ["plugin:alpha"]


def test_init_add_lock_happy_path_and_local_link():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.add_plugin("local", "beta", "0.2.0", ["verify"], source={"source": "local", "path": "C:/dev/beta"})
        h.flush()
        assert run("init", "--target", h.target)[0] == 0
        assert hunsu.LOCAL in io.open(os.path.join(h.target, ".gitignore"), encoding="utf-8").read()
        assert run("init", "--target", h.target)[0] != 0, "init must not overwrite"
        assert run("add", "alpha", "--target", h.target)[0] == 0
        assert run("add", "beta", "--target", h.target)[0] == 0
        assert run("add", "gamma", "--target", h.target)[0] != 0, "not installed -> refused"
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        assert m["plugins"]["alpha"]["source"] == "github:org/m1" and m["plugins"]["beta"]["source"] == "unpublished"
        assert hunsu.links(h.target) == {"beta": "C:/dev/beta"}
        code, out = run("lock", "--target", h.target)
        assert code == 0, out
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["plugins"]["beta"]["linked"] is True and "linked" not in lock["plugins"]["alpha"]
        assert lock["skills"] == ["alpha:build", "beta:verify"]
        assert any("unpublished" in w for w in lock["warnings"])
        assert run("unlink", "beta", "--target", h.target)[0] == 0 and hunsu.links(h.target) == {}
        assert run("link", "beta", "D:/other", "--target", h.target)[0] == 0 and hunsu.links(h.target) == {"beta": "D:/other"}


def test_a_linked_plugin_under_one_version_must_be_one_content_and_refresh_reinstalls_it():
    """The host copies a plugin at install and never looks again while the version stands. So a linked (development)
    plugin can be edited at the source while the installed copy — the one the host actually loads — stays behind, under the
    same version. check must call that out as an error (a version names one content: bump it), and `install --refresh`
    must be the one command that recopies. The host CLI is faked with a `claude` on PATH that records its argv."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        # a marketplace directory (the link) whose marketplace.json names the plugin's source folder, as the real one does
        link = os.path.join(h.home, "checkout")
        src = os.path.join(link, "alpha-src")
        write(os.path.join(link, ".claude-plugin", "marketplace.json"), {"name": "m1", "plugins": [{"name": "alpha", "source": "./alpha-src"}]})
        shutil.copytree(h.installed["alpha@m1"][0]["installPath"], src)
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        assert run("link", "alpha", link, "--target", h.target)[0] == 0
        manifest(h.target, engines={"claude-code": ">=0.0"})
        errors, _, info = hunsu.check(h.target)
        assert not errors and any("plugin alpha: linked to" in i for i in info), (errors, info)
        os.makedirs(os.path.join(src, "__pycache__")); write(os.path.join(src, "__pycache__", "x.pyc"), "junk")   # what interpreters leave behind is not content
        assert not hunsu.check(h.target)[0]
        write(os.path.join(src, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, better\n---\n")
        errors, _, _ = hunsu.check(h.target)
        assert len(errors) == 1 and "plugin alpha: the linked source" in errors[0] and "same version 1.0.0" in errors[0] and "--refresh alpha" in errors[0], errors
        # the comparison is between the link and the install, whatever the manifest says: with the manifest behind the host
        # (drift, info for a linked plugin) the same edit is still an error — this is the case that hid live
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST)); m["plugins"]["alpha"]["version"] = "0.9.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, _, info = hunsu.check(h.target)
        assert len(errors) == 1 and "same version 1.0.0" in errors[0] and any("linked to" in i and "(manifest 0.9.0)" in i for i in info), (errors, info)
        # the source bumped past the install: a warning naming both versions, no content error (the content is expected to differ)
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.1"})
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors and any("linked source is at 1.0.1, the installed copy at 1.0.0" in w for w in warnings), (errors, warnings)
        write(os.path.join(src, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0"})
        m["plugins"]["alpha"]["version"] = "1.0.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        # `install --refresh alpha`: uninstall then install at project scope, from the target's cwd; a plugin that is not linked is refused
        bindir = os.path.join(h.home, "bin"); os.makedirs(bindir)
        log = os.path.join(h.home, "claude.log")
        fake = os.path.join(bindir, "claude")
        write(fake, "#!/bin/sh\necho \"$PWD $*\" >> %s\n" % log.replace("\\", "/"))
        os.chmod(fake, 0o755)
        path = os.environ["PATH"]; os.environ["PATH"] = bindir + os.pathsep + path
        try:
            assert run("install", "--refresh", "beta", "--target", h.target)[0] != 0
            code, out = run("install", "--refresh", "alpha", "--target", h.target)
        finally:
            os.environ["PATH"] = path
        assert code == 0 and "refresh alpha@m1" in out, out
        calls = io.open(log, encoding="utf-8").read().strip().split("\n")
        assert calls == ["%s plugin uninstall alpha@m1 --scope project" % os.path.realpath(h.target), "%s plugin install alpha@m1 --scope project" % os.path.realpath(h.target)], calls


def test_survey_reads_this_projects_install_record_not_the_first_projects():
    """installed_plugins.json holds one record per (scope, project). Once two projects hold different versions of one plugin,
    entries[0] is whichever project installed first — survey must take this project's own record, then a user-scope one."""
    with Host() as h:
        old = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        new = plugin(h.cache, "m1", "alpha", "1.1.0", ["build", "deploy"])
        other = os.path.join(h.home, "other-project"); os.makedirs(other)
        h.installed["alpha@m1"] = [{"scope": "project", "projectPath": other, "installPath": old, "version": "1.0.0"},
                                   {"scope": "project", "projectPath": h.target, "installPath": new, "version": "1.1.0"}]
        h.flush()
        s = hunsu.survey(h.target)
        assert s["plugins"]["alpha@m1"]["version"] == "1.1.0" and [sk["name"] for sk in s["skills"]] == ["build", "deploy"], s["plugins"]
        assert hunsu.survey(other)["plugins"]["alpha@m1"]["version"] == "1.0.0"
        h.installed["alpha@m1"] = [{"scope": "project", "projectPath": other, "installPath": old, "version": "1.0.0"},
                                   {"scope": "user", "installPath": new, "version": "1.1.0"}]
        h.flush()
        assert hunsu.survey(h.target)["plugins"]["alpha@m1"]["version"] == "1.1.0", "a user-scope install applies to every project without its own record"


def test_a_role_provider_is_what_the_plugin_declares_and_the_lock_carries_argv():
    """A role's provider is `session`, an argv, or `plugin:role` — and the last only when that plugin's plugin.json declares
    the role's argv (`roles`). A skill id is not a provider: a skill is text the session agent reads; a runner spawns argv.
    `lock` materializes `plugin:role` to the declared argv with `{host}` filled, and keeps the declaration beside it."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], roles={"build": ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "{host}"]})
        h.add_plugin("m2", "beta", "1.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target); run("add", "beta", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip",
                 roles={"implementer": "alpha:build", "verifier": "beta:review", "planner": "session", "coherence": ["python3", "x.py", "{base}"], "odd": "nonsense"})
        errors, _, _ = hunsu.check(h.target)
        role_errs = sorted(e for e in errors if e.startswith("role "))
        assert len(role_errs) == 2, errors
        assert "role odd: provider 'nonsense' — not `plugin:role`, an argv, or `session`" in role_errs[0], role_errs
        assert "role verifier: provider 'beta:review' — plugin beta declares no role 'review' (review is a skill of beta — a skill is read by the session agent, not spawned" in role_errs[1], role_errs
        manifest(h.target, roles={"implementer": "alpha:build", "planner": "session", "coherence": ["python3", "x.py", "{base}"], "verifier": "native:alpha:build", "bad": "native:alpha:nope"})
        role_errs = [e for e in hunsu.check(h.target)[0] if e.startswith("role ")]
        assert len(role_errs) == 1 and "role bad: provider 'native:alpha:nope'" in role_errs[0], role_errs
        manifest(h.target, roles={"implementer": "alpha:build", "planner": "session", "coherence": ["python3", "x.py", "{base}"], "verifier": "native:alpha:build"})
        assert run("lock", "--target", h.target)[0] == 0
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["roles"]["implementer"] == ["python3", "{plugin:alpha}/worker.py", "--request", "{request}", "--response", "{response}", "--host", "claude"], lock["roles"]
        assert lock["roles"]["verifier"] == {"native": lock["roles"]["implementer"]} and lock["roles-declared"]["verifier"] == "native:alpha:build", lock["roles"]
        assert lock["roles"]["planner"] == "session" and lock["roles"]["coherence"] == ["python3", "x.py", "{base}"]
        assert lock["roles-declared"]["implementer"] == "alpha:build"


def test_survey_describes_what_the_host_runs_a_directory_marketplace_plugin_in_place():
    """The host loads a plugin from a `directory` marketplace in place (its init event says so), while the install record
    still points at a cache copy. survey must report the in-place path — engines, roles, skills and the same-version content
    check are then about what actually runs — and say which rule it applied."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        checkout = os.path.join(h.home, "checkout")
        write(os.path.join(checkout, ".claude-plugin", "marketplace.json"), {"name": "m1", "plugins": [{"name": "alpha", "source": "./alpha"}]})
        write(os.path.join(checkout, "alpha", ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.0", "roles": {"build": ["x"]}})
        write(os.path.join(checkout, "alpha", "skills", "build", "SKILL.md"), "---\nname: build\ndescription: the live one\n---\n")
        write(os.path.join(checkout, "alpha", "skills", "extra", "SKILL.md"), "---\nname: extra\ndescription: only in the source\n---\n")
        h.markets["m1"] = {"source": {"source": "directory", "path": checkout}, "installLocation": checkout}
        h.flush()
        s = hunsu.survey(h.target)
        a = s["plugins"]["alpha@m1"]
        assert a["loaded-from"] == "marketplace directory" and a["path"] == os.path.join(checkout, "alpha").replace(os.sep, "/"), a
        assert a["roles"] == {"build": ["x"]} and [sk["name"] for sk in s["skills"]] == ["build", "extra"], (a, s["skills"])
        # a directory marketplace whose directory is gone: back to the install record, and said so
        h.markets["m1"] = {"source": {"source": "directory", "path": checkout + "-moved"}, "installLocation": checkout + "-moved"}
        h.flush()
        a = hunsu.survey(h.target)["plugins"]["alpha@m1"]
        assert a["loaded-from"] == "install" and "roles" in a and a["roles"] == {}, a


def test_the_lock_names_content_so_an_unbumped_edit_is_caught_on_every_host():
    """Whether the host runs a plugin in place or from a copy, the lock records a fingerprint of the content each version
    named at lock time. An edit under the same version fails `check` everywhere; a bump (and a new lock) clears it."""
    with Host() as h:
        root = h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        fp = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]["fingerprint"]
        assert fp and len(fp) == 12
        assert not hunsu.check(h.target)[0]
        write(os.path.join(root, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, edited after the lock\n---\n")
        errors, _, _ = hunsu.check(h.target)
        assert len(errors) == 1 and "plugin alpha: content differs from what the lock recorded for version 1.0.0" in errors[0], errors
        write(os.path.join(root, ".claude-plugin", "plugin.json"), {"name": "alpha", "version": "1.0.1"})
        h.installed["alpha@m1"][0]["version"] = "1.0.1"; h.flush()
        errors, _, _ = hunsu.check(h.target)
        assert errors == ["plugin alpha: manifest 1.0.0, host 1.0.1"], errors   # the bump is drift for the manifest to take, not a lie
        run("add", "alpha", "--target", h.target)
        assert run("lock", "--target", h.target)[0] == 0 and not hunsu.check(h.target)[0]


def test_a_sandboxed_codex_session_is_told_its_workers_cannot_start():
    """Inside a sandboxed Codex session (CODEX_SANDBOX set) a lock whose roles are commands cannot be driven: nested `codex exec`
    does not start. check names it — a fact about this session, not the manifest — and only when a role is a command."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target); run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, roles={"planner": "session"})
        saved = os.environ.pop("CODEX_SANDBOX", None)
        try:
            os.environ["CODEX_SANDBOX"] = "seatbelt"
            assert not [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")], "no command roles: nothing to warn about"
            manifest(h.target, roles={"planner": "session", "implementer": ["python3", "w.py", "{request}", "{response}"]})
            warns = [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")]
            assert len(warns) == 1 and "(implementer)" in warns[0] and "--dangerously-bypass-approvals-and-sandbox" in warns[0], warns
            del os.environ["CODEX_SANDBOX"]
            assert not [w for w in hunsu.check(h.target)[1] if w.startswith("session: sandboxed")]
        finally:
            if saved is not None:
                os.environ["CODEX_SANDBOX"] = saved


def test_check_error_branches_each_fire():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build", "retro"], hooks={"Stop": [{"hooks": [{"type": "command", "command": "nonexistent-runtime-xyz go"}]}]})
        h.add_plugin("m2", "gamma", "3.0.0", ["retro"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "gamma", "--target", h.target)
        manifest(h.target, engines={"python": ">=99.0"}, roles={"implementer": "alpha:nope"})
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        m["plugins"]["alpha"]["version"] = "1.0.1"          # host has 1.0.0
        m["plugins"]["delta"] = {"version": "1.0.0", "source": "github:org/x", "marketplace": "x"}   # not installed
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, warnings, info = hunsu.check(h.target)
        text = "\n".join(errors)
        # a plugin missing on this host: the skill-set checks wait (they would be about a set that is not here)
        assert "plugin alpha: manifest 1.0.1, host 1.0.0" in text and "plugin delta: in manifest (from x), not enabled" in text, text
        assert "skill retro" not in text and any("checked once every plugin is here" in i for i in info), (text, info)
        del m["plugins"]["delta"]
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        errors, warnings, info = hunsu.check(h.target)
        text = "\n".join(errors)
        for needle in ("plugin alpha: manifest 1.0.1, host 1.0.0",
                       "engine python: requires >=99.0", "skill retro: provided by alpha, gamma", "role implementer: provider 'alpha:nope' — plugin alpha declares no role 'nope'"):
            assert needle in text, (needle, text)
        assert not any("nonexistent-runtime-xyz" in e for e in errors), "unknown first tokens are not runtimes we judge"
        code, out = run("lock", "--target", h.target)
        assert code != 0 and "not locked" in out, out
        manifest(h.target, resolutions={"retro": "gamma"})
        errors, _, _ = hunsu.check(h.target)
        assert not any("skill retro" in e for e in errors), "a resolution clears the overlap"


def test_a_plugins_own_engine_floor_is_checked_against_the_team_and_this_machine():
    """A plugin says what it needs (`engines` in plugin.json). check must (1) refuse a manifest whose floor does not
    cover it — a team floor below a plugin's is a promise the lock cannot keep — and (2) refuse this machine when it
    is below the plugin's floor, so the failure is one line at check time, not a traceback inside a worker later."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], engines={"python": ">=3.11"})
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        mine = "%d.%d.%d" % sys.version_info[:3]
        for team, expect in ((None, "manifest `engines` does not require it"), ({"python": ">=3.9"}, "manifest `engines` requires >=3.9"),
                             ({"python": "3.11"}, "manifest `engines` requires 3.11"), ({"python": ">=3.11"}, None), ({"python": ">=3.12"}, None)):
            manifest(h.target, engines={"claude-code": ">=0.0", **(team or {})})
            errors, _, _ = hunsu.check(h.target)
            cover = [e for e in errors if "raise it" in e]
            assert (cover == []) if expect is None else (len(cover) == 1 and "plugin alpha: needs python >=3.11" in cover[0] and expect in cover[0]), (team, errors)
        machine = [e for e in errors if "found" in e and "here" in e]
        # the machine floor is about the machine's python (what `version_of` finds on PATH — `python`, falling back
        # to `python3`), not the interpreter running this test; assert against the same measurement check uses
        found = hunsu.version_of("python")
        assert (machine == []) if hunsu.satisfies(found, ">=3.11") else ("plugin alpha: needs python >=3.11, found %s here" % (found or "nothing") in "\n".join(machine)), (found, errors)
        # compose, asked for floors, names what the plugins need next to what this machine has
        manifest(h.target, engines={})
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "plugins need: alpha >=3.11" in out, out


def test_runtime_missing_and_duplicate_and_user_hook_warnings():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], hooks={"Stop": [{"hooks": [{"type": "command", "command": "python \"${CLAUDE_PLUGIN_ROOT}/x.py\""}]}]})
        h.user_hooks = {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "python3 /home/FIXTURE/guard.py"}]},
                                        {"matcher": "Edit", "hooks": [{"type": "command", "command": "/home/FIXTURE-py/python.exe /home/FIXTURE/guard.py"}]}],
                        "SessionStart": [{"hooks": [{"type": "command", "command": "python /home/FIXTURE/watch.py"}]}]}
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"python": ">=3.0"})
        errors, warnings, info = hunsu.check(h.target)
        wt = "\n".join(warnings)
        assert "hook PostToolUse guard.py: registered 2 times (user)" in wt, wt
        assert "user hook PostToolUse (guard.py)" in wt and "user mode instruction SessionStart (watch.py)" in wt, wt
        assert any("absolute path" in i for i in info)
        manifest(h.target, **{"local-hooks-ok": ["guard.py", "watch.py"]})
        _, warnings, _ = hunsu.check(h.target)
        assert not any(w.startswith("user ") for w in warnings), warnings
        # A hook's runtime missing from PATH is an error — the hook would silently not run on this machine.
        path = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            errors, _, _ = hunsu.check(h.target)
        finally:
            os.environ["PATH"] = path
        assert any(e.startswith("runtime python: called by a hook, not on PATH") for e in errors), errors


def test_compose_stops_at_each_gate_then_locks():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"], hooks={"Stop": [{"hooks": [{"type": "command", "command": "python x.py"}]}]})
        h.flush()
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and out.strip().endswith("re-run compose") and "which of these plugins" in out, out
        run("add", "alpha", "--target", h.target)
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "engines" in out.split("decision:")[-1], out
        assert "floor versions does the team require for claude-code, python?" in out and '"claude-code": ">=' in out and '"python": ">=' in out, out
        manifest(h.target, engines={"python": ">=3.0"})
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "require for claude-code?" in out, out   # the one still missing is named, not the question repeated
        manifest(h.target, engines={"claude-code": ">=0.0", "python": ">=3.0"})
        code, out = run("compose", "--target", h.target)
        assert code == hunsu.DECISION and "no conflict judgment yet" in out, out
        manifest(h.target, judge="skip")
        code, out = run("compose", "--target", h.target)
        assert code == 0 and "locked" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["judge"] == "skipped"


def test_judge_packets_consume_and_situation_resolutions():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"], hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "node mode.js"}]}]})
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "beta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        code, out = run("judge", "request", "--target", h.target, "--out", d)
        assert code == 0 and os.path.exists(os.path.join(d, "cluster-request.json")), out
        packet = hunsu.load_json(os.path.join(d, "cluster-request.json"))
        assert {s["id"] for s in packet["skills"]} == {"alpha:critique", "alpha:build", "beta:review"} and packet["modes"][0]["source"] == "plugin:beta"
        # the judge's stage-1 answer
        write(os.path.join(d, "cluster-response.json"), {"groups": [
            {"situation": "reviewing a change", "members": ["alpha:critique", "beta:review"], "why": "both review"},
            {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        assert code == 0 and "2 group packets" in out, out
        g1 = hunsu.load_json(os.path.join(d, "group-01-request.json"))
        assert g1["situation"] == "reviewing a change" and [m["id"] for m in g1["members"]] == ["alpha:critique", "beta:review"]
        assert "does critique" in g1["members"][0]["text"], "full SKILL.md text travels with stage 2"
        # stage-2 answers: one good finding, one without quotes (must be rejected), one for the mode
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
             "why": "same work", "proposed": {"use": "alpha:critique"}},
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "x"}, "why": "no quote for beta", "proposed": {}}]})
        write(os.path.join(d, "group-02-response.json"), {"findings": [
            {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "build it", "plugin:beta": "don't"},
             "why": "mode says otherwise", "proposed": '{"order": ["alpha:build", "plugin:beta"]}'}]})   # as text, the way Codex's strict schema carries it
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")
        assert code == 0 and "2 findings" in out and "1 rejected" in out, out
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert next(r for r in j["situations"] if r["situation"] == "building")["findings"][0]["proposed"] == {"order": ["alpha:build", "plugin:beta"]}
        doc = io.open(os.path.join(h.target, hunsu.CONFLICTS_DOC), encoding="utf-8").read()
        assert "## reviewing a change — needs a resolution" in doc and "rejected by hunsu" in doc, doc
        errors, _, _ = hunsu.check(h.target)
        assert sum("no resolution" in e for e in errors) == 2, errors
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True},
                                        "building": {"order": ["alpha:build", "plugin:beta"]}})
        errors, warnings, _ = hunsu.check(h.target)
        assert not errors, errors
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": False}, "building": {"order": ["alpha:build", "plugin:beta"]}})
        code, out = run("check", "--findings", "--target", h.target)
        doc = json.loads(out)
        assert code == 0 and doc["source"] == "hunsu" and [f["kind"] for f in doc["findings"]] == ["unreviewed"], out
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}, "building": {"order": ["alpha:build", "plugin:beta"]}})
        manifest(h.target, engines={"claude-code": ">=0.0", "node": ">=0.0"})
        assert run("lock", "--target", h.target)[0] == 0
        lock = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))
        assert lock["denied"] == ["beta:review"] and lock["judge"] == "judged"
        # a judgment is about text: a version bump that changes no member's text stales nothing (a patch release costs no judge round)...
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        m["plugins"]["alpha"]["version"] = "1.1.0"
        hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        alpha11 = h.add_plugin("m1", "alpha", "1.1.0", ["critique", "build"]); h.flush()
        errors, _, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors), errors
        # ...while a changed SKILL.md stales exactly the situations that member is in, version or no version
        write(os.path.join(alpha11, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: does build, now also deploys\n---\n")
        errors, _, _ = hunsu.check(h.target)
        stale_errs = [e for e in errors if "judged about other text" in e]
        assert len(stale_errs) == 1 and "1 situation(s)" in stale_errs[0] and "building" in stale_errs[0], errors   # alpha:build is in one
        m["plugins"]["alpha"]["version"] = "1.0.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        # a mode's command changed: the mode is a member of every situation, so both go stale; `--stale` re-asks just those and consume merges
        m["plugins"]["beta"]["version"] = "2.1.0"; hunsu.save_json(os.path.join(h.target, hunsu.MANIFEST), m)
        h.add_plugin("m2", "beta", "2.1.0", ["review"], hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "node mode2.js"}]}]}); h.flush()
        errors, _, _ = hunsu.check(h.target)
        stale_errs = [e for e in errors if "judged about other text" in e]
        assert len(stale_errs) == 1 and "2 situation(s)" in stale_errs[0], errors
        code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
        assert code == 0 and "2 group packets" in out and not os.path.exists(os.path.join(d, "cluster-response.json")), out
        write(os.path.join(d, "group-01-response.json"), {"findings": []})   # the new beta no longer overlaps
        write(os.path.join(d, "group-02-response.json"), {"findings": [
            {"kind": "contradiction", "members": ["alpha:build", "plugin:beta"], "quotes": {"alpha:build": "build it", "plugin:beta": "still don't"},
             "why": "mode still says otherwise", "proposed": {"order": ["alpha:build", "plugin:beta"]}}]})
        code, out = run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test2")
        assert code == 0 and "2 situations" in out and "1 findings" in out, out
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert {r["situation"]: r["judged-by"] for r in j["situations"]} == {"reviewing a change": "test2", "building": "test2"}
        # the findings under both resolutions changed (one vanished, one requoted): the human's resolutions are marked unread, not dropped
        assert "findings changed under 2 existing resolution(s)" in out, out
        res = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]
        assert res["reviewing a change"]["reviewed"] is False and res["reviewing a change"]["deny"] == ["beta:review"] and res["building"]["reviewed"] is False
        errors, _, _ = hunsu.check(h.target)
        assert not any("judged about other text" in e for e in errors), errors
        j = hunsu.load_json(os.path.join(h.target, hunsu.JUDGMENTS))
        assert all("members-text-fingerprint" in r for r in j["situations"]), "a fresh judgment records what it read"
        # a new skill no situation has seen needs a cluster round, not a stale round
        h.add_plugin("m3", "gamma", "1.0.0", ["other"])
        h.flush()
        run("add", "gamma", "--target", h.target)
        errors, _, _ = hunsu.check(h.target)
        assert any("never seen gamma:other" in e for e in errors), errors
        assert run("judge", "request", "--target", h.target, "--out", d, "--stale")[0] != 0
        # remove drops the plugin and its link, and names resolutions that still mention it
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"]}})
        code, out = run("remove", "beta", "--target", h.target)
        assert code == 0 and "resolutions still mention it: reviewing a change" in out, out
        assert "beta" not in hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["plugins"]


def test_sentinel_allows_locked_denies_unlocked_and_resolved_away():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build", "retro"])
        h.add_plugin("m2", "gamma", "3.0.0", ["retro"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "gamma", "--target", h.target)
        manifest(h.target, resolutions={"retro": "gamma"})
        assert run("lock", "--target", h.target)[0] == 0
        hook = os.path.join(HERE, "hooks", "pre_skill.py")

        def call(skill):
            payload = json.dumps({"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": skill}})
            done = subprocess.run([sys.executable, hook], input=payload, capture_output=True, text=True, encoding="utf-8")
            return done.returncode, done.stderr
        assert call("alpha:build")[0] == 0
        assert call("gamma:retro")[0] == 0
        code, why = call("alpha:retro")
        assert code == 2 and "ruled out" in why, why
        assert call("zeta:anything")[0] == 2
        assert call("")[0] == 0, "no skill name -> not ours to judge"
        os.remove(os.path.join(h.target, hunsu.LOCK))
        assert call("zeta:anything")[0] == 0, "no lock -> allow"


def test_policy_lines_materialize_every_resolution_shape_and_the_session_hook_carries_them():
    lock = {"resolutions": {"retro": "dakdol", "old": "deny",
                            "reviewing a change": {"use": "a:eyes", "deny": ["b:review"], "reviewed": True},
                            "building": {"order": ["a:build", "plugin:b"], "note": "b shapes the code inside a's scope", "reviewed": False},
                            "auditing": {"accept": "both apply, different inputs"}}}
    lines = hunsu.policy_lines(lock)
    assert lines[0] == "retro: dakdol's" and lines[1] == "old: denied"
    assert lines[2] == "reviewing a change: use a:eyes; never b:review"
    assert lines[3].startswith("building: a:build first, then plugin:b; (b shapes") and lines[3].endswith("[unreviewed — applied from the judge's proposal]")
    assert lines[4] == "auditing: both apply: both apply, different inputs"
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        manifest(h.target, engines={"claude-code": ">=0.0"}, resolutions={"building": {"order": ["alpha:build", "plugin:beta"]}}, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        def hook(name, payload):
            done = subprocess.run([sys.executable, os.path.join(HERE, "hooks", name)], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env)
            return done.returncode, done.stdout + done.stderr
        code, out = hook("session_start.py", {"cwd": h.target})
        assert "hunsu policy" in out and "- building: alpha:build first, then plugin:beta" in out, out
        # the sentinel lets host built-ins through: nothing outside the manifest runs, honestly scoped to plugin skills
        assert hook("pre_skill.py", {"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": "simplify"}})[0] == 0
        assert hook("pre_skill.py", {"cwd": h.target, "tool_name": "Skill", "tool_input": {"skill": "gamma:other"}})[0] == 2


def test_a_namesake_from_two_marketplaces_is_two_plugins():
    with Host() as h:
        h.add_plugin("old", "alpha", "1.0.0", ["build", "retro"])
        h.add_plugin("new", "alpha", "2.0.0", ["build"])
        h.flush()
        s = hunsu.survey(h.target)
        assert set(s["plugins"]) == {"alpha@old", "alpha@new"} and {sk["marketplace"] for sk in s["skills"]} == {"old", "new"}
        run("init", "--target", h.target)
        code, out = run("add", "alpha", "--target", h.target)
        assert code != 0 and "enabled from alpha@new, alpha@old" in out, out
        assert run("add", "alpha@new", "--target", h.target)[0] == 0
        m = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))
        assert m["plugins"]["alpha"]["version"] == "2.0.0" and m["plugins"]["alpha"]["marketplace"] == "new"
        errors, warnings, info = hunsu.check(h.target)
        assert not any("skill build" in e for e in errors), errors   # the old alpha's build is not this project's
        assert any("alpha@old: enabled here but not in manifest" in i for i in info), info
        assert hunsu.locked_skill_ids(m, s) == ["alpha:build"]
        manifest(h.target, engines={"claude-code": ">=0.0"}, judge="skip")
        assert run("lock", "--target", h.target)[0] == 0
        locked = hunsu.load_json(os.path.join(h.target, hunsu.LOCK))["plugins"]["alpha"]
        assert {k: locked[k] for k in ("version", "source", "marketplace")} == {"version": "2.0.0", "source": "github:org/new", "marketplace": "new"} and locked["fingerprint"]


def test_codex_survey_and_lock_materialize_the_projects_hooks():
    import importlib, tempfile, shutil
    if sys.version_info < (3, 11):
        raise Skip("the Codex survey reads config.toml with tomllib (3.11+); this interpreter is %d.%d" % sys.version_info[:2])
    root = tempfile.mkdtemp(prefix="hunsu-codex-")
    try:
        codex = os.path.join(root, "codex")
        target = os.path.join(root, "proj")
        os.makedirs(target)
        plug = os.path.join(codex, "plugins", "cache", "m1", "alpha", "1.2.0")
        os.makedirs(os.path.join(plug, "skills", "build"))
        os.makedirs(os.path.join(plug, "hooks"))
        write(os.path.join(plug, ".codex-plugin", "plugin.json"), {"name": "alpha", "version": "1.2.0"})
        write(os.path.join(plug, "skills", "build", "SKILL.md"), "---\nname: build\ndescription: builds\n---\nbody\n")
        write(os.path.join(plug, "hooks", "codex.json"), {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "python \"${PLUGIN_ROOT}/hooks/s.py\""}]}]}})
        write(os.path.join(codex, "config.toml"), 'model = "x"\n[marketplaces.m1]\nsource_type = "local"\nsource = \'\\\\?\\%s\'\n[plugins."alpha@m1"]\nenabled = true\n[plugins."off@m1"]\nenabled = false\n[[hooks.SessionStart]]\nmatcher = "startup"\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "python3 mine.py"\n' % root)   # the way Codex writes a Windows path
        os.environ["HUNSU_CODEX_DIR"], os.environ["AGENT_HOST"] = codex, "codex"
        h2 = importlib.reload(hunsu)
        try:
            s = h2.survey(target)
            assert s["host"] == "codex" and list(s["plugins"]) == ["alpha@m1"] and s["plugins"]["alpha@m1"]["version"] == "1.2.0"
            assert s["plugins"]["alpha@m1"]["source"] == root.replace("\\", "/"), s["plugins"]["alpha@m1"]["source"]   # no \\?\\ prefix, no leading slash
            assert [sk["name"] for sk in s["skills"]] == ["build"] and {h["source"] for h in s["hooks"]} == {"plugin:alpha", "user"}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                h2.main(["init", "--target", target]); h2.main(["add", "alpha", "--target", target])
                manifest(target, engines={}, judge="skip", **{"local-hooks-ok": ["mine.py"]})
                code = h2.main(["lock", "--target", target])
            assert code == 0 and "codex: 1 hook group(s)" in out.getvalue(), out.getvalue()
            hooks = hunsu.load_json(os.path.join(target, ".codex", "hooks.json"))["hooks"]
            py = "python3" if shutil.which("python3") else "python"   # the plugin wrote `python`; the materialized file names what this machine has
            assert hooks["SessionStart"][0]["hooks"][0]["command"] == '%s "%s/hooks/s.py"' % (py, plug.replace("\\", "/")), hooks
            assert ".codex/hooks.json" in io.open(os.path.join(target, ".gitignore"), encoding="utf-8").read()
            # the materialized file is hunsu's own: a second survey must not count its hooks as the project's (each would register twice)
            again = h2.survey(target)
            assert {hk["source"] for hk in again["hooks"]} == {"plugin:alpha", "user"} and len(again["hooks"]) == len(s["hooks"]), again["hooks"]
            assert hunsu.load_json(os.path.join(target, hunsu.LOCK))["host"] == "codex"
        finally:
            del os.environ["HUNSU_CODEX_DIR"], os.environ["AGENT_HOST"]
            importlib.reload(hunsu)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_session_start_hook_reports_both_states():
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["build"])
        h.flush()
        hook = os.path.join(HERE, "hooks", "session_start.py")
        env = dict(os.environ, HUNSU_CLAUDE_DIR=h.claude)
        done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target}), capture_output=True, text=True, encoding="utf-8", env=env)
        assert "no hunsu.json" in json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"], done.stdout
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        done = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": h.target}), capture_output=True, text=True, encoding="utf-8", env=env)
        assert "matches hunsu.json" in json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"], done.stdout


def test_the_reviewed_gate_is_delegated_to_the_judge_and_fails_closed():
    """When a re-judged situation's findings change under an existing resolution, the stale packet carries the resolution
    (`existing_resolution` + `prior_findings`) and the judge answers `resolution_verdict`. consume keeps the resolution only
    for still-fits with a verbatim quote (a delegation record replaces `reviewed`); does-not-fit, or a quote not found in
    the member texts, falls back to `reviewed: false` — the human gate. check counts the delegated form as reviewed."""
    with Host() as h:
        h.add_plugin("m1", "alpha", "1.0.0", ["critique", "build"])
        h.add_plugin("m2", "beta", "2.0.0", ["review"])
        h.flush()
        run("init", "--target", h.target)
        run("add", "alpha", "--target", h.target)
        run("add", "beta", "--target", h.target)
        d = os.path.join(h.target, "judge")
        run("judge", "request", "--target", h.target, "--out", d)
        write(os.path.join(d, "cluster-response.json"), {"groups": [
            {"situation": "reviewing a change", "members": ["alpha:critique", "beta:review"], "why": "both review"},
            {"situation": "building", "members": ["alpha:build"], "why": "alone"}]})
        run("judge", "request", "--target", h.target, "--out", d, "--groups", os.path.join(d, "cluster-response.json"))
        # a group packet for a situation without a resolution carries no existing_resolution
        assert "existing_resolution" not in hunsu.load_json(os.path.join(d, "group-01-request.json"))
        write(os.path.join(d, "group-01-response.json"), {"findings": [
            {"kind": "overlap", "members": ["alpha:critique", "beta:review"], "quotes": {"alpha:critique": "does critique", "beta:review": "does review"},
             "why": "same work", "proposed": {"use": "alpha:critique"}}]})
        assert run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test")[0] == 0
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}})
        alpha = h.installed["alpha@m1"][0]["installPath"]

        def stale_round(new_desc, quote, response):
            """Change alpha:critique's text (stales 'reviewing a change'), request --stale, drop the judge's answer in."""
            write(os.path.join(alpha, "skills", "critique", "SKILL.md"), "---\nname: critique\ndescription: %s\n---\n" % new_desc)
            code, out = run("judge", "request", "--target", h.target, "--out", d, "--stale")
            assert code == 0 and "1 group packets" in out, out
            name = next(n for n in sorted(os.listdir(d)) if n.startswith("group-") and n.endswith("-request.json"))
            req = hunsu.load_json(os.path.join(d, name))
            response["findings"] = [{"kind": "overlap", "members": ["alpha:critique", "beta:review"],
                                     "quotes": {"alpha:critique": new_desc, "beta:review": "does review"}, "why": "still same work", "proposed": {}}]
            write(os.path.join(d, name.replace("-request", "-response")), response)
            return req, run("judge", "consume", "--target", h.target, "--dir", d, "--by", "test2")

        # 1) still-fits with a verbatim quote: the resolution is kept, `reviewed` becomes a delegation record
        req, (code, out) = stale_round("does critique, now with style", "does critique, now with style",
                                       {"worker": {"host": "claude-code", "model": "opus-x"},
                                        "resolution_verdict": {"verdict": "still-fits", "why": "the deny still holds: " + "x" * 300,
                                                               "quote": "does critique, now with style"}})
        assert req["existing_resolution"]["resolution"]["deny"] == ["beta:review"], req
        assert req["existing_resolution"]["prior_findings"][0]["why"] == "same work", req   # what the human's resolution was written about
        assert "still fits (quote verified); kept, delegation recorded" in out, out
        res = hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]
        assert res["deny"] == ["beta:review"], res
        assert res["reviewed"]["delegated"].startswith("judge claude-code opus-x: the deny still holds:") and len(res["reviewed"]["delegated"]) <= len("judge claude-code opus-x: ") + 200, res
        # check treats the delegated form as reviewed: no unreviewed finding
        code, out = run("check", "--findings", "--target", h.target)
        assert not [f for f in json.loads(out)["findings"] if f["kind"] == "unreviewed"], out
        # and compose's policy line names the delegation
        lines = hunsu.policy_lines({"resolutions": {"reviewing a change": res}})
        assert lines[0].endswith("[re-judged by delegation]"), lines
        # 2) does-not-fit: back to the human gate
        _, (code, out) = stale_round("does critique, differently", "irrelevant",
                                     {"resolution_verdict": {"verdict": "does-not-fit", "why": "the overlap moved", "quote": "does critique, differently"}})
        assert "marked reviewed: false (a human must re-read)" in out and "'does-not-fit'" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]["reviewed"] is False
        code, out = run("check", "--findings", "--target", h.target)
        assert [f["kind"] for f in json.loads(out)["findings"] if f["kind"] == "unreviewed"] == ["unreviewed"], out
        # 3) still-fits whose quote is not verbatim in the member texts: fail closed, human gate
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}})
        _, (code, out) = stale_round("does critique, a third way", "n/a",
                                     {"resolution_verdict": {"verdict": "still-fits", "why": "trust me", "quote": "a sentence nobody wrote"}})
        assert "quote not found verbatim in the member texts" in out and "reviewed: false" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]["reviewed"] is False
        # 4) no resolution_verdict at all: the human gate, as before
        manifest(h.target, resolutions={"reviewing a change": {"deny": ["beta:review"], "reviewed": True}})
        _, (code, out) = stale_round("does critique, a fourth way", "n/a", {})
        assert "no resolution_verdict" in out and "reviewed: false" in out, out
        assert hunsu.load_json(os.path.join(h.target, hunsu.MANIFEST))["resolutions"]["reviewing a change"]["reviewed"] is False


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Skip as why:
                print("SKIP", name, "--", why)
            except (Exception, SystemExit) as err:   # a self-check that dies between tests lies by omission
                failed += 1
                print("FAIL", name, "--", "%s: %s" % (type(err).__name__, err))
    print("all passed" if not failed else "%d failed" % failed)
    sys.exit(1 if failed else 0)
