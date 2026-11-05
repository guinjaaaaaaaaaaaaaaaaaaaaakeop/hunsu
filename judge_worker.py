"""Source-checkout compatibility — not part of the installed plugin.

hunsu's implementation is the payload under plugin/, and only there. This root file keeps the checkout's existing entry
points working: `python3 hunsu.py …` and `python3 judge_worker.py …` run the payload's file of the same name, and
`import hunsu`, `import hostcall` or `import judge_worker` here gives the payload's module itself — one module object with
what plugin/'s hooks and workers import, so a monkeypatch on it reaches the runtime. plugin/ goes first on sys.path before
anything is loaded, so the payload's own sibling imports resolve inside it. The root hunsu.py, judge_worker.py and
hostcall.py are this same text; each finds its payload file by its own name.
"""
import os
import sys

_PLUGIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "plugin")
_PAYLOAD = os.path.join(_PLUGIN, os.path.basename(__file__))
if sys.path[:1] != [_PLUGIN]:
    sys.path.insert(0, _PLUGIN)

if __name__ == "__main__":
    import runpy
    runpy.run_path(_PAYLOAD, run_name="__main__")
else:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(__name__, _PAYLOAD)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[__name__] = _module   # in this module's place before it runs, as an import does: `import <name>` inside finds it
    _spec.loader.exec_module(_module)
