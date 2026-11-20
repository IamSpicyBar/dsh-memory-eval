#!/usr/bin/env python3
"""Frozen executor validation. No L2 run or external source checkout required."""
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

sys.dont_write_bytecode = True
import backend
from evaluate import read, write, manifest, copy_memory, BACKEND

ARMS = ("B0", "B1", "B2")
_active = None


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stop(signum=None, frame=None):
    if _active and _active.poll() is None:
        os.killpg(_active.pid, signal.SIGTERM) if os.name == "posix" else _active.terminate()
    raise SystemExit(130)


def core_call(spec, action, *, request=None, prepared=None, ledger=None, limit=None, timeout=150):
    global _active
    if not Path(spec["cli"]).is_absolute() or not Path(spec["config"]).is_absolute():
        raise ValueError("Executor needs explicit installed core CLI/config paths")
    argv = [spec.get("node", "node"), spec["cli"], action, "--config", spec["config"]]
    if request: argv += ["--request", str(request)]
    if prepared: argv += ["--prepared", str(prepared)]
    if ledger: argv += ["--ledger", str(ledger), "--limit", str(limit)]
    _active = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=os.name == "posix")
    try:
        out, err = _active.communicate(timeout=timeout)
        if _active.returncode: raise ValueError("Core executor failed: " + err[-1500:])
        return json.loads(out)
    except subprocess.TimeoutExpired:
        os.killpg(_active.pid, signal.SIGKILL) if os.name == "posix" else _active.kill()
        _active.communicate()
        raise ValueError("Executor timed out; no implicit retry")
    finally:
        _active = None


def target_path(root, name):
    backend.relative_target(name)
    target = Path(root) / name
    if any(p.is_symlink() for p in [target, *target.parents]) or not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Validation target escapes its workspace")
    return target


def verify_suite(directory):
    root = Path(directory).resolve(); suite = read(root / "suite.json")
    if suite.get("schemaVersion") != "hmbot.memory.l3/2" or suite.get("backend") != BACKEND:
        raise ValueError("Legacy/changed evaluation backend: retain old reports read-only and prepare a new suite")
    for name, expected in read(root / "frozen.json").items():
        if digest(root / name) != expected: raise ValueError("Frozen validation input changed: " + name)
    if manifest(root / "candidate-memory") != suite["memoryFiles"]: raise ValueError("Reading pack changed")
    return root, suite


def prepare(config, output):
    c = read(config); output = Path(output).resolve()
    if output.exists(): raise ValueError("Validation suite output must be new")
    if not re.fullmatch("[a-f0-9]{64}", c.get("memoryRevision", "")): raise ValueError("Exact Memory revision required")
    reading_manifest = Path(c["memory"]) / "manifest.json"
    if reading_manifest.exists() and read(reading_manifest).get("memory_revision") != c["memoryRevision"]:
        raise ValueError("Reading pack belongs to another Memory revision")
    task = read(c["task"]) if isinstance(c.get("task"), str) else c.get("task")
    if not isinstance(task, dict) or not task.get("prompt", "").strip() or not task.get("targets") or not task.get("checks"):
        raise ValueError("Supply an independent frozen task with targets and calibrated checks; no L2 suite needed")
    if len(set(task["targets"])) != len(task["targets"]): raise ValueError("Duplicate targets")
    check_ids = [v.get("id") for v in task["checks"]]
    if not all(check_ids) or len(set(check_ids)) != len(check_ids): raise ValueError("Unique check IDs required")
    calibration = {}
    for target in task["targets"]:
        backend.relative_target(target)
        failure = backend.baseline_for(task, target, c["baseline"], c["project"]).read_text(encoding="utf-8")
        correct = backend.reference_for(task, target, c["project"]).read_text(encoding="utf-8")
        checks = backend.checks_for_target(task, target)
        if not checks or any(backend.run_check(v, failure) or not backend.run_check(v, correct) for v in checks):
            raise ValueError("Each check must fail the recorded failure and pass its correct reference")
        calibration[target] = {"failure": hashlib.sha256(failure.encode()).hexdigest(), "reference": hashlib.sha256(correct.encode()).hexdigest()}
    if any(v.get("target", task["targets"][0]) not in task["targets"] for v in task["checks"]): raise ValueError("Unknown check target")
    execution = c["execution"]
    if execution.get("mode") not in ("task-rerun", "historical-replay"): raise ValueError("Explicit executor mode required")
    budget = execution.get("budget", {})
    if type(budget.get("maxRequests")) is not int or not 1 <= budget["maxRequests"] <= 256: raise ValueError("Bounded executor requests required")
    if c.get("runs", 3) != 3: raise ValueError("Full L3 acceptance requires three runs per arm")
    output.mkdir(parents=True, mode=0o700)
    copy_memory(c["memory"], output / "candidate-memory")
    texts = [backend.find_lesson_text(output / "candidate-memory", lesson) for lesson in task.get("lessons", [])]
    if not texts or any(not v for v in texts): raise ValueError("B2 needs explicit task-linked lesson IDs")
    (output / "b2.md").write_text("Applicable Memory guidance; check its conditions against the task:\n" + "\n\n".join(texts), encoding="utf-8")
    (output / "b1.md").write_text("For relevant experience consult rsi-memory/index.md and select applicable lessons.\n", encoding="utf-8")
    # Only this object reaches the executing Agent. Checks/references stay here.
    visible = {"schemaVersion": "hmbot.rsi.execution-task/1", "prompt": task["prompt"]}
    if execution["mode"] == "task-rerun":
        source = Path(c["workspace"]).resolve()
        manifest(source)  # reject symbolic links and excessive input
        shutil.copytree(source, output / "workspace")
        visible["workspace"] = str(output / "workspace")
    write(output / "task.json", visible)
    memory_files = manifest(output / "candidate-memory")
    suite = {"schemaVersion": "hmbot.memory.l3/2", "backend": BACKEND, "core": c["core"], "task": task,
             "taskDigest": digest(output / "task.json"), "calibration": calibration, "execution": execution,
             "memoryRevision": c["memoryRevision"], "memoryFiles": memory_files, "runs": 3,
             "ledger": c["ledger"], "requestLimit": c["requestLimit"], "scope": "one frozen task; text checks; no build/device or held-out generalization claim"}
    # Capability validation is offline and does not consume the run directory.
    probe = {**execution, "taskRef": str(output / "task.json"), "taskDigest": suite["taskDigest"], "output": str(output / "probe")}
    write(output / "inspect-request.json", probe)
    inspected = core_call(suite["core"], "inspect", request=output / "inspect-request.json")
    if not inspected.get("available"): raise ValueError(inspected.get("reason", "Core executor unavailable"))
    write(output / "suite.json", suite)
    write(output / "frozen.json", {name: value for name, value in manifest(output).items() if name != "frozen.json"})
    return {"suite": str(output / "suite.json"), "suiteSha256": digest(output / "suite.json"), "memoryRevision": c["memoryRevision"], "agentRuns": 9, "arms": list(ARMS), "mode": execution["mode"], "modelCalls": 0}


def run(directory, output):
    root, suite = verify_suite(directory); output = Path(output).resolve()
    if output.exists() or output.is_relative_to(root) or root.is_relative_to(output): raise ValueError("Results need a new separate directory; no implicit retries")
    if not os.environ.get("HMBOT_EXECUTOR_MODEL_KEY"): raise ValueError("Configure the core executor model credential explicitly")
    output.mkdir(parents=True, mode=0o700)
    write(output / "input.json", {"suiteSha256": digest(root / "suite.json"), "memoryRevision": suite["memoryRevision"]})
    for iteration in range(1, 4):
        for arm in ARMS:
            label = arm + "-" + str(iteration); work = output / label
            request = {**suite["execution"], "taskRef": str(root / "task.json"), "taskDigest": suite["taskDigest"], "output": str(work), "assetDigest": suite["memoryRevision"] if arm != "B0" else None}
            if arm != "B0":
                request["instructionRef"] = str(root / ("b1.md" if arm == "B1" else "b2.md"))
                request["instructionDigest"] = digest(request["instructionRef"])
            if arm == "B1":
                # Core computes/verifies the directory digest, independent of
                # this plugin's file-manifest serialization.
                request["bindings"] = [{"source": str(root / "candidate-memory"), "target": "rsi-memory", "files": suite["memoryFiles"]}]
            path = output / (label + "-request.json"); write(path, request)
            core_call(suite["core"], "prepare", request=path)
            receipt = core_call(suite["core"], "run", prepared=work, ledger=suite["ledger"], limit=suite["requestLimit"], timeout=request["budget"]["timeoutSeconds"] + 180)
            write(output / (label + ".json"), {"arm": arm, "iteration": iteration, "suiteSha256": digest(root / "suite.json"), "receiptSha256": digest(work / "receipt.json")})
            if receipt.get("status") != "completed": raise ValueError("Agent execution incomplete; inspect receipt, no automatic retry")
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
    if read(output / "input.json") != {"suiteSha256": suite_sha, "memoryRevision": suite["memoryRevision"]}: raise ValueError("Result version mismatch")
    task = suite["task"]; rows = []; template = []; notes = set(); models = set(); observed = set()
    review_path = output / "process-review.json"; review = read(review_path) if review_path.exists() else {}
    if review and (review.get("suiteSha256") != suite_sha or review.get("memoryRevision") != suite["memoryRevision"]): raise ValueError("Review version mismatch")
    seen = set()
    for iteration in range(1, 4):
        for arm in ARMS:
            label = arm + "-" + str(iteration); entry = output / (label + ".json"); work = output / label
            if not entry.exists(): continue
            item = read(entry); receipt_path = work / "receipt.json"; receipt = read(receipt_path)
            if item["suiteSha256"] != suite_sha or item["receiptSha256"] != digest(receipt_path) or receipt["taskDigest"] != suite["taskDigest"]: raise ValueError("Executor receipt binding changed")
            if receipt.get("assetDigest") != (suite["memoryRevision"] if arm != "B0" else None): raise ValueError("Memory arm binding changed")
            trace = Path(receipt["trace"])
            if digest(trace) != receipt["traceDigest"]: raise ValueError("Raw trace changed")
            artifacts = Path(receipt["artifactDirectory"]); current = manifest(artifacts)
            if current != receipt["artifacts"]: raise ValueError("Executed artifact changed")
            checks = {}
            for check in task["checks"]:
                target = target_path(artifacts, check.get("target", task["targets"][0]))
                checks[check["id"]] = target.is_file() and backend.run_check(check, target.read_text(encoding="utf-8"))
            row = {**item, "valid": receipt["status"] == "completed" and receipt["modelRequests"] > 0, "checks": checks, "passed": receipt["status"] == "completed" and all(checks.values()), "traceSha256": receipt["traceDigest"], "processEffective": False}
            notes.update(receipt.get("fidelityNotes", []))
            if receipt.get("sourceModel"): models.add(receipt["sourceModel"])
            observed.update(receipt.get("reportedModels", []))
            if arm != "B0":
                matching = [d for d in review.get("decisions", []) if d.get("trial") == label]
                if len(matching) > 1: raise ValueError("Duplicate process decisions")
                if matching:
                    d = matching[0]; text = trace.read_text(encoding="utf-8")
                    if d.get("traceSha256") != receipt["traceDigest"] or any(type(d.get(k)) is not bool for k in ["received", "applied", "misused"]) or not d.get("reason", "").strip() or not d.get("traceQuote") or d["traceQuote"] not in text: raise ValueError("Explicit process review requires exact trace quotations")
                    seen.add(label); row["misused"] = d["misused"]; row["processEffective"] = row["passed"] and d["received"] and d["applied"] and not d["misused"]
                template.append({"trial": label, "traceSha256": receipt["traceDigest"], "received": None, "applied": None, "misused": None, "traceQuote": "", "reason": ""})
            rows.append(row)
    process_complete = len(seen) == 6 and bool(review.get("reviewer", "").strip()) and review.get("method") == "explicit-human-review"
    comparable = len(observed) == 1 and (suite["execution"]["mode"] == "task-rerun" or models == observed)
    fidelity_complete = all(note in review.get("acknowledgedFidelityNotes", []) for note in notes)
    report = {"schemaVersion": "hmbot.memory.l3-report/2", **aggregate(rows, process_complete, comparable, fidelity_complete), "suiteSha256": suite_sha, "memoryRevision": suite["memoryRevision"], "mode": suite["execution"]["mode"], "scope": suite["scope"], "reportedModels": sorted(observed), "trials": rows}
    write(output / "process-review-template.json", {"suiteSha256": suite_sha, "memoryRevision": suite["memoryRevision"], "reviewer": "", "method": "explicit-human-review", "acknowledgedFidelityNotes": [], "fidelityNotes": sorted(notes), "decisions": template})
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
