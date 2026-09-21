"""SessionStart hook — hunsu shows up on its own; nobody has to remember it exists.

Reads the hook payload (stdin JSON with `cwd`) and puts into the session's first context:
  no hunsu.json   -> one line: offer to lock the environment (the compose skill does it)
  hunsu.json      -> one line: run check; report errors/warnings so drift is seen before any work starts
  hunsu.lock.json -> the lock's resolutions, one line per situation: the conflict policy, materialized where the agent reads it
Never blocks. Never writes to the project.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "hunsu.py")
PY = "python3" if shutil.which("python3") else "python"   # the name that actually exists here — macOS ships only python3
sys.path.insert(0, os.path.dirname(HERE))
import hunsu  # noqa: E402


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    if not os.path.exists(os.path.join(cwd, "hunsu.json")):
        msg = ("hunsu: this project has no hunsu.json — its agent environment is not locked. "
               "Use the hunsu:compose skill to declare which plugins, hooks and engines this project uses (the engine is `%s \"%s\"`)." % (PY, ENGINE.replace(os.sep, "/")))
    else:
        done = subprocess.run([sys.executable, ENGINE, "check", "--target", cwd],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = [l for l in done.stdout.strip().split("\n") if l.strip()]
        summary = tail[-1] if tail else "check produced no output"
        state = "environment differs from hunsu.json" if done.returncode else "environment matches hunsu.json"
        msg = "hunsu: %s (%s). `%s \"%s\" check` for the list." % (state, summary, PY, ENGINE.replace(os.sep, "/"))
        lock = hunsu.load_json(os.path.join(cwd, "hunsu.lock.json"))
        lines = hunsu.policy_lines(lock)
        if lines:
            msg += ("\nhunsu policy — this project's resolutions between its skills, by situation (hunsu.json `resolutions`; the judge's evidence "
                    "is in hunsu-conflicts.md). Follow them when the situation applies:\n- " + "\n- ".join(lines))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
