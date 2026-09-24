"""hostcall — one host session per worker call, for every worker in this family: hunsu's judge, mangsang's judge, dwitbuk's
eyes, hacheong's members.

Vendored, not imported across plugins: a product knows roles and artifact types, never another product, and each plugin is
installed on its own — so the same file sits in each plugin, and the umbrella checkout's `tools/same-file.py` fails when
the copies differ. Nothing here reads a request or judges an answer: it starts the host with a prompt, a schema and a
sandbox, returns what the host said, and keeps the whole stream next to the response as the call's own record.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def run_claude(prompt, schema, cwd=None, add_dirs=(), tools="Read,Grep,Glob", model=None, effort=None, max_turns=30, bypass=False):
    """`claude -p`: stream-json out, no session persistence, no user settings or MCP servers, exactly the tools named, the
    answer constrained by `schema`. Returns (returncode, stdout, stderr) as text. The env marks the session a worker's
    (AGENT_WORKER=1, so the project's hooks step aside inside it) and keeps auto-memory off."""
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config", "--tools", tools, "--allowedTools", tools,
           "--max-turns", str(max_turns), "--json-schema", json.dumps(schema)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    if bypass:
        cmd += ["--permission-mode", "bypassPermissions"]
    done = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, cwd=cwd,
                          env=dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", AGENT_WORKER="1"))
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


def claude_answer(stdout):
    """The answer in a claude stream: the last `result` event's `structured_output`, else its `result` text parsed as JSON.
    (answer, None) — or (None, why) when there is no result event, the result is an error, or the text is not JSON."""
    result = None
    for line in stdout.split("\n"):
        if line.strip():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result = event
    if not result:
        return None, "no result event from claude"
    if result.get("is_error"):
        return None, "claude error: %s" % result.get("subtype")
    out = result.get("structured_output")
    if out is None:
        try:
            out = json.loads(result.get("result", "").strip())
        except ValueError:
            return None, "no structured answer"
    return out, None


def run_codex(prompt, schema, cwd, add_dirs=(), sandbox="read-only", model=None, effort=None, beside=None):
    """`codex exec --json` with an output schema and the prompt on stdin. The schema and Codex's raw last message go to files:
    beside `beside` (a response path: `<response>.schema.json`, `<response>.last.txt` — the runner's ignore list knows them)
    or in a temporary directory. AGENT_CODEX_SANDBOX in the env overrides `sandbox`. No turn budget: `codex exec` has none
    to give (unknown `-c` keys are accepted silently, so none is pretended); the session ends on its own.
    Returns (returncode, stdout, stderr, last_text_or_None)."""
    if beside:
        schema_path, last_path = str(Path(beside).with_suffix(".schema.json")), str(Path(beside).with_suffix(".last.txt"))
    else:
        tmp = tempfile.mkdtemp(prefix="hostcall-")
        schema_path, last_path = os.path.join(tmp, "schema.json"), os.path.join(tmp, "last.txt")
    Path(schema_path).write_text(json.dumps(schema), encoding="utf-8")
    cmd = [shutil.which("codex") or "codex", "exec", "--json", "-C", cwd, "-s", os.environ.get("AGENT_CODEX_SANDBOX") or sandbox,
           "--skip-git-repo-check", "--output-schema", schema_path, "-o", last_path]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", "model_reasoning_effort=%s" % json.dumps(effort)]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    cmd.append("-")
    done = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, env=dict(os.environ, AGENT_WORKER="1"))
    try:
        last = Path(last_path).read_text(encoding="utf-8")
    except OSError:
        last = None
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"), last


def worker_record(stdout_text, response_path, host, model=None, stderr_text=None):
    """Who did this call, from the host's own account of the session — model, turns, cost — with the whole stream kept next
    to the response as `<response>.transcript.jsonl`. The runner copies this into `performed_by`; an answer whose procedure is
    not on disk cannot be audited (a verdict of "accept, no findings" says nothing about what was read).
    Claude Code says it in its stream (`init`: model; `result`: turns, cost, session). Codex says it in the header it prints
    on stderr (`model:`, `session id:`, `reasoning effort:`); its stream is `--json` items on stdout."""
    rec = {"host": host, "model": model}
    if host == "codex":
        # `--json` gives the item stream and the thread id, not the header; the model is in the rollout Codex keeps for
        # that thread (~/.codex/sessions/**/rollout-*-<thread id>.jsonl, `turn_context.model`)
        m = re.search(r'"thread_id":\s*"([^"]+)"', stdout_text or "")
        if m:
            rec["session"] = m.group(1)
            home = os.environ.get("HUNSU_CODEX_DIR") or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
            for dirpath, _, files in os.walk(os.path.join(home, "sessions")):
                for f in files:
                    if f.endswith(m.group(1) + ".jsonl"):
                        with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                mm = re.search(r'"turn_context".*?"model":\s*"([^"]+)"', line)
                                if mm:
                                    rec["model"] = rec["model"] or mm.group(1)
                                    ee = re.search(r'"effort":\s*"([^"]+)"', line)
                                    if ee:
                                        rec["effort"] = ee.group(1)
                                    break
        for key, name in (("model", "model"), ("session id", "session"), ("reasoning effort", "effort")):   # the header, when a host prints one
            mh = re.search(r"^%s:\s*(.+?)\s*$" % re.escape(key), stderr_text or "", re.M)
            if mh and not rec.get(name):
                rec[name] = mh.group(1)
        kept = "".join(x for x in (stderr_text, stdout_text) if x)
        if kept:
            path = re.sub(r"\.json$", "", response_path) + ".transcript.jsonl"
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(kept if kept.endswith("\n") else kept + "\n")
            rec["transcript"] = os.path.basename(path)
        return rec
    if stdout_text is None:
        return rec
    for line in stdout_text.split("\n"):
        try:
            event = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            rec["model"] = event.get("model") or model
        elif event.get("type") == "result":
            rec["turns"], rec["cost_usd"], rec["session"] = event.get("num_turns"), event.get("total_cost_usd"), event.get("session_id")
    path = re.sub(r"\.json$", "", response_path) + ".transcript.jsonl"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(stdout_text if stdout_text.endswith("\n") else stdout_text + "\n")
    rec["transcript"] = os.path.basename(path)
    return rec
