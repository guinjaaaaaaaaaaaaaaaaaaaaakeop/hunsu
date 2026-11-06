"""Project-local Codex hook dispatcher; independent of any installed hunsu copy."""
import json
import os
import re
import subprocess
import sys


class Absent(Exception):
    """The declared Codex plugin cannot be resolved; `repair` says what makes it resolve."""

    def __init__(self, reason, repair=None):
        super().__init__(reason)
        self.repair = repair or "install it for Codex (`codex plugin add NAME@MARKET`)"


def resolve(codex_home, key):
    try:
        try:
            import tomllib
        except ImportError:
            raise Absent("Codex's config.toml is read with tomllib, which needs Python 3.11+; this python3 is %d.%d" % sys.version_info[:2],
                         "put Python 3.11+ first as python3 on the PATH Codex runs hooks with")
        name, separator, market = key.partition("@")
        if not separator or not name or not market:
            raise Absent("invalid plugin key", "`hunsu upgrade --hooks-only` rewrites the generated groups")
        install = "install it for Codex (`codex plugin add %s`)" % key
        stage = install   # the repair for a failure before the cache is read: config.toml absent, then not readable
        with open(os.path.join(codex_home, "config.toml"), "rb") as stream:
            stage = "repair Codex's config.toml"
            config = tomllib.load(stream)
        plugins = config.get("plugins", {})
        if not isinstance(plugins, dict):
            raise Absent("config.toml plugins is not a table", "repair Codex's config.toml")
        entry = plugins.get(key, {})
        if not isinstance(entry, dict):
            raise Absent("config.toml plugin entry is not a table", "repair Codex's config.toml")
        if not entry.get("enabled"):
            raise Absent("plugin is not enabled in config.toml", "install it for Codex (`codex plugin add %s`)" % key)
        cache = os.path.join(codex_home, "plugins", "cache", market, name)
        stage = install
        def order(value):
            version = tuple(int(x) for x in value.split(".")) if re.fullmatch(r"\d+(\.\d+){0,3}", value) else None
            return version is not None, version or (), value
        entries = sorted(os.listdir(cache), key=order)
        if not entries:
            raise Absent("no copy in the cache: %s" % cache, "install it for Codex (`codex plugin add %s`)" % key)
        root = os.path.abspath(os.path.join(cache, entries[-1])).replace("\\", "/")
        version = entries[-1]
        stage = "reinstall it (`codex plugin remove` + `add`)"
        if not os.path.isdir(root):
            raise Absent("%s at %s is not a directory copy" % (version, root), "reinstall it (`codex plugin remove` + `add`)")
        for relative in (".codex-plugin/plugin.json", "plugin.json"):
            try:
                with open(os.path.join(root, relative), encoding="utf-8") as stream:
                    manifest = json.load(stream)
            except FileNotFoundError:
                continue
            if not isinstance(manifest, dict):
                raise ValueError("%s is not a JSON object" % relative)
            if manifest:
                version = manifest.get("version") or version
                break
        return root, version
    except Absent:
        raise
    except BaseException as exc:
        context = "%s at %s: " % (version, root) if "root" in locals() else ""
        raise Absent(context + (str(exc) or type(exc).__name__), locals().get("stage")) from exc


def main(argv):
    key = argv[0] if argv else "(missing key)"
    version = None
    root = None
    try:
        if len(argv) < 2:
            raise ValueError("usage: KEY PROGRAM [ARG ...]")
        home = next((os.environ[n] for n in ("HUNSU_CODEX_DIR", "AGENT_CODEX_HOME", "CODEX_HOME") if os.environ.get(n)), os.path.expanduser("~/.codex"))
        root, version = resolve(home, key)
        words = []
        for word in argv[1:]:
            replaced = word.replace("{PLUGIN_ROOT}", root)
            if "{PLUGIN_ROOT}" in word and not os.path.exists(replaced):
                raise ValueError("missing %s" % word.replace("{PLUGIN_ROOT}/", ""))
            words.append(replaced)
        lock_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hunsu.lock.json")
        try:
            with open(lock_path, encoding="utf-8") as stream:
                lock = json.load(stream)
            if not isinstance(lock, dict) or not isinstance(lock.get("plugins", {}), dict):
                raise ValueError("lock or plugins is not an object")
            entry = lock.get("plugins", {}).get(key.partition("@")[0], {})
            if not isinstance(entry, dict):
                raise ValueError("plugin entry is not an object")
            pin = entry.get("version")
            if "version" in entry and not isinstance(pin, str):
                raise ValueError("version is not a string")
        except FileNotFoundError:
            pin = None
        except BaseException as exc:
            pin = None
            reason = " ".join(str(exc).splitlines()) or type(exc).__name__
            print("hunsu dispatch: %s: hunsu.lock.json does not read (%s); no version compared" % (key, reason), file=sys.stderr, flush=True)
        if pin and pin != version:
            print("hunsu dispatch: %s runs %s installed for Codex; hunsu.lock.json pins %s" % (key, version, pin), file=sys.stderr, flush=True)
        env = dict(os.environ, PLUGIN_ROOT=root)
        if os.name == "posix":
            sys.stderr.flush()
            os.execvpe(words[0], words, env)
        return subprocess.call(words, env=env)
    except BaseException as exc:
        repair = exc.repair if isinstance(exc, Absent) else "reinstall the copy / hunsu upgrade --hooks-only"
        reason = " ".join(str(exc).splitlines()) or type(exc).__name__
        print("hunsu dispatch: %s%s: %s; %s" % (key, (" " + str(version) if version else "") + (" at " + root if root else ""), reason, repair), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
