#!/usr/bin/env python3
"""Native agent-replay bridge. Preparing/scoring is offline; run starts real agents."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from urllib.parse import urlparse

sys.dont_write_bytecode = True
from evaluate import read, write, sha, manifest, load_suite, upstream

REPLAY_COMMIT = "39d623771d1abfb7a0c31454c49decc9cc110f48"
ARMS = ("B0", "B1", "B2")
_active = None


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stop(signum=None, frame=None):
    if _active and _active.poll() is None:
        if os.name == "posix":
            os.killpg(_active.pid, signal.SIGTERM)
        else:
            _active.terminate()
    raise SystemExit(130)


def native(spec, args, log, timeout=120):
    global _active
    source = Path(spec["directory"]).resolve()
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != REPLAY_COMMIT or spec["commit"] != REPLAY_COMMIT:
        raise ValueError("agent-replay needs the pinned checkout")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise ValueError("agent-replay checkout changed")
    log = Path(log); log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "AGENT_REPLAY_CACHE": str(log.parent / ".native-cache")}
    with log.open("wb") as stream:
        _active = subprocess.Popen([sys.executable, "-B", "-m", "agent_replay", *map(str, args)], env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
        try:
            code = _active.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix": os.killpg(_active.pid, signal.SIGTERM)
            else: _active.terminate()
            try: _active.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix": os.killpg(_active.pid, signal.SIGKILL)
                else: _active.kill()
                _active.wait()
            raise ValueError("L3 native step timed out; original evidence retained")
        finally:
            _active = None
    if code:
        raise ValueError("L3 native step failed; inspect the local native log")


def target_path(root, name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or any(p.startswith(".") for p in path.parts):
        raise ValueError("L3 target must be an ordinary relative artifact path")
    target = Path(root) / path
    if any(p.is_symlink() for p in [target, *target.parents]) or not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("L3 target escapes its replay workspace")
    return target


def verify_suite(directory):
    root = Path(directory).resolve(); suite = read(root / "suite.json")
    for name, expected in read(root / "frozen.json").items():
        if digest(root / name) != expected: raise ValueError("L3 frozen input changed: " + name)
    if manifest(root / "candidate-memory") != suite["memoryFiles"]:
        raise ValueError("L3 reading pack changed")
    return root, suite


def replay(root, suite, destination, config_dir, log):
    args = ["replay", root / "recording.zip", "--agent", suite["agent"], "--at", suite["at"], "--out", destination,
            "--config-dir", config_dir, "--deny-rules", root / "deny-rules.json", "--stop-at", "Agent"]
    for old, new in suite["maps"].items(): args += ["--map", old + "=" + new]
    if suite["thinkingAsText"]: args.append("--thinking-as-text")
    native(suite["agentReplay"], args, log)
    meta = read(Path(destination) / ".agent-replay/replay.json")
    if not meta.get("guard") or meta.get("tree_only") or not meta.get("agent") or meta.get("point") != suite["at"]:
        raise ValueError("Native replay is not a guarded historical agent decision")
    return meta


def prepare(config, output):
    c = read(config); output = Path(output).resolve()
    if output.exists(): raise ValueError("L3 suite output must be new")
    if c.get("runs", 3) != 3: raise ValueError("L3 requires exactly three runs per arm")
    if c.get("at", "first-write") not in ("first-write", "dispatch") or not str(c.get("agent", "")).strip():
        raise ValueError("Select one historical agent at first-write or dispatch")
    if not re.fullmatch("[a-f0-9]{64}", c.get("memoryRevision", "")): raise ValueError("L3 requires the exact Memory revision")
    l2_root, l2, runner, lib = load_suite(c["l2Suite"])
    entries = [v for v in l2["tasks"] if v["task"]["id"] == c.get("taskId")]
    if len(entries) != 1: raise ValueError("Select one frozen calibrated L2 task")
    task = entries[0]["task"]
    recording = Path(c["recording"]).resolve()
    if recording.is_symlink() or not recording.is_file() or recording.suffix.lower() != ".zip" or recording.stat().st_size > 4 * 1024**3:
        raise ValueError("L3 needs an agent-replay ZIP recording with historical file snapshots (at most 4 GiB)")
    model = str(c.get("model", "")).strip()
    if not model: raise ValueError("Select the replay model explicitly")
    base_url = c.get("baseUrl", ""); endpoint = urlparse(base_url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("L3 needs the explicit HTTPS model endpoint without credentials")
    maps = c.get("maps", {})
    if not isinstance(maps, dict) or any(not Path(k).is_absolute() or not Path(v).is_absolute() for k, v in maps.items()):
        raise ValueError("Historical source/SDK maps need absolute old and new paths")
    max_turns = c.get("maxTurns", 8); seconds = c.get("timeoutSeconds", 900)
    if type(max_turns) is not int or not 1 <= max_turns <= 30 or type(seconds) is not int or not 30 <= seconds <= 1800:
        raise ValueError("L3 needs bounded turns and timeout")
    output.mkdir(parents=True, mode=0o700)
    recording_sha = digest(recording); shutil.copyfile(recording, output / "recording.zip")
    if digest(recording) != recording_sha or digest(output / "recording.zip") != recording_sha: raise ValueError("Recording changed during freezing")
    memory = l2_root / "candidate-memory"; memory_files = manifest(memory)
    shutil.copytree(memory, output / "candidate-memory")
    if manifest(output / "candidate-memory") != memory_files: raise ValueError("Reading pack changed while freezing")
    lessons = task.get("lessons", [])
    texts = [runner.find_lesson_text(memory, lesson) for lesson in lessons]
    if not texts or any(not text for text in texts): raise ValueError("B2 needs the task-linked frozen lessons")
    (output / "b2.md").write_text("\n\n".join(texts), encoding="utf-8")
    # Do not allow replay agents to launch new workers or browse the network.
    write(output / "deny-rules.json", [{"tool": tool, "reason": "Frozen L3 unit: delegation/network is outside scope"} for tool in ["Agent", "Task", "WebFetch", "WebSearch"]])
    suite = {"schemaVersion": "hmbot.memory.l3/1", "agentReplay": c["agentReplay"], "upstreamPath": l2["upstreamPath"],
             "recordingSha256": recording_sha, "memoryRevision": c["memoryRevision"], "memoryFiles": memory_files,
             "l2SuiteSha256": digest(l2_root / "suite.json"), "task": task, "agent": str(c["agent"]), "at": c.get("at", "first-write"),
             "model": model, "baseUrl": base_url, "maps": maps, "thinkingAsText": bool(c.get("thinkingAsText", False)), "runs": 3,
             "maxTurns": max_turns, "timeoutSeconds": seconds, "scope": "historical-agent-replay; one development unit; text checks"}
    # Probe the real snapshot without starting Claude or invoking a model.
    meta = replay(output, suite, output / "probe", output / "probe-config", output / "probe.log")
    # The pre-write snapshot can still be correct: the replayed agent is about
    # to introduce the deviation. Calibrate against its recorded END artifact.
    end = output / "historical-end"
    native(suite["agentReplay"], ["replay", output / "recording.zip", "--agent", suite["agent"], "--at", "end", "--tree-only", "--out", end], output / "historical-end.log")
    historical = {}
    for target in task["targets"]:
        path = target_path(end, target)
        if not path.is_file():
            raise ValueError("L3 task target is absent from the recorded agent output; prepare a task calibrated on the actual historical artifact (L1/L2 remain usable)")
        historical[target] = path.read_text(encoding="utf-8")
    if any(lib.run_check(check, historical[check.get("target", task["targets"][0])]) for check in task["checks"]):
        raise ValueError("L3 checks do not discriminate this recorded agent's actual failure; recalibrate the task")
    suite.update(sourceModel=meta.get("cut", {}).get("model"), fidelityNotes=meta.get("notes", []), sourceCommit=meta["commit"], sourceAgent=meta["agent"], historicalArtifactSha256={target: digest(target_path(end, target)) for target in task["targets"]})
    write(output / "suite.json", suite)
    frozen = {"suite.json": digest(output / "suite.json"), "recording.zip": recording_sha, "b2.md": digest(output / "b2.md"), "deny-rules.json": digest(output / "deny-rules.json")}
    for name, value in memory_files.items(): frozen["candidate-memory/" + name] = value
    write(output / "frozen.json", frozen)
    return {"suite": str(output / "suite.json"), "suiteSha256": frozen["suite.json"], "memoryRevision": suite["memoryRevision"],
            "agentRuns": 9, "arms": list(ARMS), "sourceModel": suite["sourceModel"], "model": model, "fidelityNotes": suite["fidelityNotes"], "modelCalls": 0}


def run(directory, output):
    root, suite = verify_suite(directory); output = Path(output).resolve()
    if output.exists() or output.is_relative_to(root) or root.is_relative_to(output): raise ValueError("L3 results must be a new separate directory; no implicit retries")
    if not shutil.which(os.environ.get("CLAUDE_BIN", "claude")): raise ValueError("L3 execution needs Claude Code; prepare/score do not")
    os.environ["ANTHROPIC_BASE_URL"] = suite["baseUrl"]
    output.mkdir(parents=True, mode=0o700)
    write(output / "input.json", {"suiteSha256": digest(root / "suite.json"), "memoryRevision": suite["memoryRevision"]})
    for iteration in range(1, 4):
        for arm in ARMS:
            label = arm + "-" + str(iteration); work = output / label
            meta = replay(root, suite, work, output / (label + "-config"), output / (label + "-prepare.log"))
            if meta["commit"] != suite["sourceCommit"] or meta["agent"]["id"] != suite["sourceAgent"]["id"]: raise ValueError("Replay decision changed")
            if arm == "B1":
                shutil.copytree(root / "candidate-memory", work / "rsi-memory")
                with (work / "CLAUDE.md").open("a", encoding="utf-8") as stream:
                    stream.write("\n\nFor guidance applicable to the current task, consult rsi-memory/index.md and choose relevant lessons.\n")
            elif arm == "B2":
                # Keep the original agent definition; --system-file alone replaces it.
                append = work / ".agent-replay" / (meta.get("append_file") or "append-system-prompt.md")
                original = append.read_text(encoding="utf-8") if append.exists() else ""
                append.write_text(original + "\n\nMemory guidance for this task:\n" + (root / "b2.md").read_text(encoding="utf-8"), encoding="utf-8")
                meta["append_file"] = append.name; write(work / ".agent-replay/replay.json", meta)
            native(suite["agentReplay"], ["run", work, "--print", "--model", suite["model"], "--", "--max-turns", str(suite["maxTurns"])], output / (label + "-run.log"), suite["timeoutSeconds"])
            last = read(work / ".agent-replay/last-run.json"); trace = work / ".agent-replay/stream.jsonl"
            rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
            finals = [row for row in rows if row.get("type") == "result"]
            valid = last.get("exit") == 0 and bool(finals) and not finals[-1].get("is_error") and finals[-1].get("subtype") == "success"
            artifacts = {target: digest(target_path(work, target)) for target in suite["task"]["targets"] if target_path(work, target).is_file()}
            write(output / (label + ".json"), {"arm": arm, "iteration": iteration, "suiteSha256": digest(root / "suite.json"), "valid": valid,
                  "traceSha256": digest(trace), "lastRunSha256": digest(work / ".agent-replay/last-run.json"), "replaySha256": digest(work / ".agent-replay/replay.json"),
                  "artifacts": artifacts, "exit": last.get("exit"), "stopped": last.get("stopped", False)})
            if not valid: raise ValueError("L3 agent did not finish normally; partial evidence retained, no retry")
    return score(directory, output)


def aggregate(rows, process_complete, comparable, fidelity_complete):
    complete = len(rows) == 9 and all(row["valid"] for row in rows)
    baseline = [r for r in rows if r["arm"] == "B0"]
    unstable = any(row["passed"] for row in baseline)
    arms = {}
    for arm in ARMS:
        selected = [r for r in rows if r["arm"] == arm]
        arms[arm] = {"valid": sum(r["valid"] for r in selected), "passed": sum(r["passed"] for r in selected),
                     "receivedAndApplied": sum(r.get("processEffective", False) for r in selected)}
    regression = False
    for row in rows:
        if row["arm"] == "B0": continue
        base = next((b for b in baseline if b["iteration"] == row["iteration"]), None)
        if row.get("misused") or base and any(ok and not row["checks"].get(key, False) for key, ok in base["checks"].items()): regression = True
    result_gain = any(arms[arm]["passed"] >= 2 for arm in ("B1", "B2"))
    process_gain = any(arms[arm]["receivedAndApplied"] >= 2 for arm in ("B1", "B2"))
    accepted = complete and not unstable and not regression and result_gain and process_gain and process_complete and comparable and fidelity_complete
    return {"complete": complete, "accepted": bool(accepted), "arms": arms, "originUnstable": unstable, "regressed": regression,
            "mechanicalImprovement": complete and not unstable and result_gain and not regression, "processReviewComplete": process_complete,
            "sameHistoricalModel": comparable, "fidelityReviewed": fidelity_complete, "scope": "one historical decision; not held-out generalization or build/device acceptance"}


def score(directory, output):
    root, suite = verify_suite(directory); output = Path(output).resolve(); suite_sha = digest(root / "suite.json")
    if read(output / "input.json") != {"suiteSha256": suite_sha, "memoryRevision": suite["memoryRevision"]}: raise ValueError("L3 result belongs to a different frozen version")
    _, lib = upstream(suite["upstreamPath"]); task = suite["task"]; rows = []; template = []
    review_path = output / "process-review.json"; review = read(review_path) if review_path.exists() else {}
    decisions = review.get("decisions", []); seen = set()
    if review and (review.get("suiteSha256") != suite_sha or review.get("memoryRevision") != suite["memoryRevision"]): raise ValueError("L3 process review version mismatch")
    for iteration in range(1, 4):
        for arm in ARMS:
            label = arm + "-" + str(iteration); receipt = output / (label + ".json")
            if not receipt.exists(): continue
            item = read(receipt); work = output / label; trace = work / ".agent-replay/stream.jsonl"
            if item["suiteSha256"] != suite_sha or item["arm"] != arm or item["iteration"] != iteration or digest(trace) != item["traceSha256"]: raise ValueError("L3 raw receipt/trace binding changed")
            for name, key in [("last-run.json", "lastRunSha256"), ("replay.json", "replaySha256")]:
                if digest(work / ".agent-replay" / name) != item.get(key): raise ValueError("L3 native completion metadata changed")
            last = read(work / ".agent-replay/last-run.json")
            finals = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip() and json.loads(line).get("type") == "result"]
            raw_valid = last.get("exit") == 0 and bool(finals) and not finals[-1].get("is_error") and finals[-1].get("subtype") == "success"
            if bool(item["valid"]) != bool(raw_valid): raise ValueError("L3 validity disagrees with the raw completion")
            current_targets = {target for target in task["targets"] if target_path(work, target).is_file()}
            if set(item["artifacts"]) != current_targets: raise ValueError("L3 target set changed after completion")
            for target, value in item["artifacts"].items():
                if digest(target_path(work, target)) != value: raise ValueError("Replay artifact changed after completion")
            checks = {}
            for check in task["checks"]:
                target = target_path(work, check.get("target", task["targets"][0]))
                checks[check["id"]] = target.is_file() and bool(lib.run_check(check, target.read_text(encoding="utf-8")))
            row = {**item, "checks": checks, "passed": item["valid"] and all(checks.values()), "processEffective": False}
            if arm != "B0":
                matching = [d for d in decisions if d.get("trial") == label]
                if len(matching) > 1: raise ValueError("Duplicate L3 process decisions")
                if matching:
                    d = matching[0]; text = trace.read_text(encoding="utf-8")
                    if d.get("traceSha256") != item["traceSha256"] or any(type(d.get(k)) is not bool for k in ["received", "applied", "misused"]) or not d.get("reason", "").strip() or not d.get("traceQuote") or d["traceQuote"] not in text:
                        raise ValueError("L3 process decision needs the exact trace quote and explicit received/applied/misused verdicts")
                    seen.add(label); row["misused"] = d["misused"]
                    row["processEffective"] = row["passed"] and d["received"] and d["applied"] and not d["misused"]
                template.append({"trial": label, "traceSha256": item["traceSha256"], "received": None, "applied": None, "misused": None, "traceQuote": "", "reason": ""})
            rows.append(row)
    process_complete = len(seen) == 6 and bool(review.get("reviewer", "").strip()) and review.get("method") == "explicit-human-review"
    fidelity_complete = not suite["fidelityNotes"] or all(note in review.get("acknowledgedFidelityNotes", []) for note in suite["fidelityNotes"])
    report = {"schemaVersion": "hmbot.memory.l3-report/1", **aggregate(rows, process_complete, bool(suite.get("sourceModel")) and suite["sourceModel"] == suite["model"], fidelity_complete),
              "suiteSha256": suite_sha, "memoryRevision": suite["memoryRevision"], "l2SuiteSha256": suite["l2SuiteSha256"], "trials": rows}
    write(output / "process-review-template.json", {"suiteSha256": suite_sha, "memoryRevision": suite["memoryRevision"], "reviewer": "", "method": "explicit-human-review", "acknowledgedFidelityNotes": [], "fidelityNotes": suite["fidelityNotes"], "decisions": template})
    write(output / "report.json", report); return report


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("action", choices=["prepare", "run", "score"])
    parser.add_argument("--config"); parser.add_argument("--suite"); parser.add_argument("--out", required=True); args = parser.parse_args()
    try:
        result = prepare(args.config, args.out) if args.action == "prepare" else run(args.suite, args.out) if args.action == "run" else score(args.suite, args.out)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr); sys.exit(1)
