"""Runs one judge request in a fresh, read-only host session and saves the structured response.

  python judge_worker.py --request FILE --response FILE [--host claude|codex] [--model M] [--effort E]

The judge is the host's model. It gets the packet on stdin, may Read files under the plugin roots named in it,
and must answer with one JSON object matching the schema for the packet's stage. hunsu validates on consume.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMAS = {
    "cluster": {"type": "object", "additionalProperties": False, "required": ["groups"],
                "properties": {"groups": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["situation", "members", "why"],
                    "properties": {"situation": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}},
                                   "why": {"type": "string"}}}}}},
    "group": {"type": "object", "additionalProperties": False, "required": ["findings"],
              "properties": {"findings": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["kind", "members", "quotes", "why", "proposed"],
                  "properties": {"kind": {"enum": ["overlap", "contradiction", "premise"]},
                                 "members": {"type": "array", "items": {"type": "string"}},
                                 "quotes": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["member", "quote"],
                                                                        "properties": {"member": {"type": "string"}, "quote": {"type": "string"}}}},   # a list, not a map: Codex's strict schemas (0.155) refuse an object with open keys; the worker folds it back to {member: quote}
                                 "why": {"type": "string"},
                                 "proposed": {"type": "string"}}}}}},   # a JSON object as text: {"use": id} | {"deny": [ids]} | {"order": [ids]} | {"accept": why} — Codex's strict schemas refuse an open object
}
PROMPT = ("This is a hunsu judge request. Change no files. Follow the packet's `instructions` exactly. "
          "Read a member's `path` when the description is not enough. Output one JSON object only, no prose, no code fence.\n")


def claude(packet, schema, roots, args):
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config", "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
           "--max-turns", str(args.max_turns), "--json-schema", json.dumps(schema)]
    if args.model:
        cmd += ["--model", args.model]
    if args.effort:
        cmd += ["--effort", args.effort]
    for r in roots:
        cmd += ["--add-dir", r]
    done = subprocess.run(cmd, input=(PROMPT + json.dumps(packet, ensure_ascii=False)).encode("utf-8"), capture_output=True,
                          env=dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", AGENT_WORKER="1"))
    stdout = done.stdout.decode("utf-8", "replace")
    worker = worker_record(stdout, args.response, "claude-code", args.model)
    if done.returncode:
        raise SystemExit("claude exited %d: %s" % (done.returncode, done.stderr.decode("utf-8", "replace")[-500:]))
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
        raise SystemExit("no result event from claude")
    if result.get("is_error"):
        raise SystemExit("claude error: %s" % result.get("subtype"))
    out = result.get("structured_output")
    out = out if out is not None else json.loads(result["result"].strip())
    out["worker"] = worker
    return out


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


def codex(packet, schema, roots, args):
    tmp = tempfile.mkdtemp(prefix="hunsu-judge-")
    schema_path, out_path = os.path.join(tmp, "schema.json"), os.path.join(tmp, "last.txt")
    Path(schema_path).write_text(json.dumps(schema), encoding="utf-8")
    cmd = [shutil.which("codex") or "codex", "exec", "--json", "-s", os.environ.get("AGENT_CODEX_SANDBOX", "read-only"), "--skip-git-repo-check", "--output-schema", schema_path, "-o", out_path,
           "-C", packet.get("target", os.getcwd())]
    if args.model:
        cmd += ["-m", args.model]
    if args.effort:
        cmd += ["-c", "model_reasoning_effort=%s" % json.dumps(args.effort)]
    for r in roots:
        cmd += ["--add-dir", r]
    cmd.append("-")
    done = subprocess.run(cmd, input=(PROMPT + json.dumps(packet, ensure_ascii=False)).encode("utf-8"), capture_output=True, env=dict(os.environ, AGENT_WORKER="1"))
    if done.returncode:
        raise SystemExit("codex exited %d: %s" % (done.returncode, done.stderr.decode("utf-8", "replace")[-500:]))
    out = json.loads(Path(out_path).read_text(encoding="utf-8").strip())
    out["worker"] = worker_record(done.stdout.decode("utf-8", "replace"), args.response, "codex", args.model, done.stderr.decode("utf-8", "replace"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--request", required=True)
    ap.add_argument("--response", required=True)
    ap.add_argument("--host", choices=["claude", "codex"], default="claude")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--prompt-only", action="store_true", help="print the prompt this call would send and exit — for a session that dispatches the host's own subagent instead of this worker")
    args = ap.parse_args()
    packet = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if packet.get("artifact-type") != "hunsu/judge-request@1":
        raise SystemExit("not a hunsu judge request: %s" % args.request)
    schema = SCHEMAS[packet["stage"]]
    if packet.get("existing_resolution"):   # the situation is already resolved: the judge must also say whether that resolution still fits
        schema = json.loads(json.dumps(schema))   # a copy; SCHEMAS stays pristine
        schema["required"] = list(schema["required"]) + ["resolution_verdict"]
        schema["properties"]["resolution_verdict"] = {
            "type": "object", "additionalProperties": False, "required": ["verdict", "why", "quote"],
            "properties": {"verdict": {"enum": ["still-fits", "does-not-fit", "cannot-tell"]},
                           "why": {"type": "string"},
                           "quote": {"type": "string"}}}   # a verbatim sentence from the member texts; consume verifies it appears exactly
    if args.prompt_only:
        print(PROMPT + json.dumps(packet, ensure_ascii=False) + "\n\n# Answer\n\nYour whole final message is one JSON object, nothing else, matching this schema:\n" + json.dumps(schema))
        return 0
    roots = sorted({os.path.dirname(os.path.dirname(os.path.dirname(m["path"]))) for m in packet.get("skills", packet.get("members", []))})
    out = (claude if args.host == "claude" else codex)(packet, schema, roots, args)
    for f in out.get("findings", []) or []:   # quotes travel as a list of {member, quote}; consume reads a map
        if isinstance(f.get("quotes"), list):
            f["quotes"] = {q.get("member"): q.get("quote") for q in f["quotes"] if isinstance(q, dict)}
    Path(args.response).parent.mkdir(parents=True, exist_ok=True)
    with open(args.response + ".tmp", "w", encoding="utf-8", newline="\n") as fh:   # LF on every host; the response is diffed and fingerprinted
        fh.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    os.replace(args.response + ".tmp", args.response)   # whole or absent: the runner polls for this file
    print("response -> %s" % args.response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
