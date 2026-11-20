#!/usr/bin/env python3
"""Self-contained frozen Memory content comparison; no source-repo imports."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlparse
import backend as builtin_backend

sys.dont_write_bytecode = True

BACKEND = {"id": builtin_backend.VERSION, "files": {
    name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
    for name in ["backend.py", "model_client.py", "memory_format.py", "evaluate.py", "l1.py", "l3.py"]}}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def backend():
    return builtin_backend, builtin_backend


def manifest(root):
    result = {}
    total = 0
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError("snapshot cannot contain symbolic links")
        if path.is_file():
            total += path.stat().st_size
            if total > 128 * 1024 * 1024:
                raise ValueError("snapshot exceeds 128 MiB")
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("snapshot file exceeds 32 MiB")
            result[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    if len(result) > 20000:
        raise ValueError("snapshot has too many files")
    return result


def copy_memory(source, target):
    source, target = Path(source).resolve(), Path(target)
    before = manifest(source)
    if not (source / "index.md").is_file():
        raise ValueError("Memory reading pack needs index.md")
    target.mkdir()
    # Evaluation needs the reading pack only. Cards, source traces and store
    # history are intentionally never included in the worker prompt.
    for name in before:
        if name.endswith(".md"):
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, dest)
    if manifest(source) != before:
        raise ValueError("Memory changed while preparing")


def prepare(config_path, output):
    c, output = read(config_path), Path(output).resolve()
    if output.exists():
        raise ValueError("prepare output must be a new directory")
    runner, lib = backend()
    tasks_root, project = Path(c["tasks"]).resolve(), Path(c["project"]).resolve()
    tasks, ids = [], set()
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".memory-eval-", dir=output.parent))
    try:
        copy_memory(c["memory"], tmp / "candidate-memory")
        if c.get("baselineMemory"):
            copy_memory(c["baselineMemory"], tmp / "baseline-memory")
        for path in sorted([*tasks_root.glob("*.yaml"), *tasks_root.glob("*.json")]):
            original = path.read_bytes()
            task = json.loads(original) if path.suffix == ".json" else lib.load_yaml(path)
            task_id = task.get("id", "")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", task_id) or task_id in ids:
                raise ValueError("invalid or duplicate task id")
            ids.add(task_id)
            targets, checks = task.get("targets"), task.get("checks")
            if not targets or len(set(targets)) != len(targets) or not checks or not task.get("prompt", "").strip():
                raise ValueError("task needs prompt, unique targets and checks")
            check_ids = [x.get("id") for x in checks]
            if any(not x for x in check_ids) or len(set(check_ids)) != len(check_ids):
                raise ValueError("check ids must be unique")
            for check in checks:
                if check.get("target", targets[0]) not in targets:
                    raise ValueError("check target is not declared")
            roots = (tasks_root.parent, tasks_root.parent.parent)
            # Every check is calibrated against actual fail/pass files. This
            # closes an upstream CLI gap where only one check had to discriminate.
            calibration = []
            for target in targets:
                base = lib.baseline_for(task, target, c["baseline"], project, roots)
                ref = lib.reference_for(task, target, project, roots)
                b, r = base.read_bytes(), ref.read_bytes()
                for check in lib.checks_for_target(task, target):
                    if lib.run_check(check, b.decode("utf-8")) or not lib.run_check(check, r.decode("utf-8")):
                        raise ValueError("check does not discriminate historical failure/reference: " + check["id"])
                calibration.append({"target": target, "baselineSha256": sha(b), "referenceSha256": sha(r)})
            frozen = {k: task[k] for k in ("id", "prompt", "targets", "checks", "lessons", "baselineLessons", "cases") if k in task}
            frozen["inputs"] = []
            for index, item in enumerate(task.get("inputs", [])):
                resolved = lib.resolve(item["path"], project, roots)
                data = resolved.read_bytes()
                dest = tmp / "inputs" / task_id / (str(index) + ".txt")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                frozen["inputs"].append({**item, "path": str(output / "inputs" / task_id / dest.name)})
            for lesson in frozen.get("lessons", []):
                if not runner.find_lesson_text(tmp / "candidate-memory", lesson):
                    raise ValueError("candidate lesson missing: " + lesson)
            if c.get("baselineMemory"):
                for lesson in frozen.get("baselineLessons", frozen.get("lessons", [])):
                    if not runner.find_lesson_text(tmp / "baseline-memory", lesson):
                        raise ValueError("baseline lesson missing; declare baselineLessons explicitly for added lessons")
            tasks.append({"task": frozen, "sourceSha256": sha(original), "calibration": calibration})
        if not tasks:
            raise ValueError("empty task suite")
        settings = {"model": c["model"], "baseUrl": c["baseUrl"], "runs": c.get("runs", 1),
                    "maxTokens": c.get("maxTokens", 8192), "temperature": c.get("temperature", 0.2),
                    "minValid": c.get("minValid", 1), "stallTimeout": c.get("stallTimeout", 180)}
        endpoint = urlparse(settings["baseUrl"])
        if endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("model endpoint must be HTTPS without embedded credentials")
        for key, low, high in (("runs", 1, 10), ("maxTokens", 256, 98304), ("minValid", 1, 10000), ("stallTimeout", 10, 600)):
            if type(settings[key]) is not int or not low <= settings[key] <= high:
                raise ValueError("invalid setting: " + key)
        if not isinstance(settings["temperature"], (int, float)) or not math.isfinite(settings["temperature"]) or not 0 <= settings["temperature"] <= 2:
            raise ValueError("invalid temperature")
        if c.get("maxRequests") is not None and (type(c["maxRequests"]) is not int or c["maxRequests"] < 1 or len(tasks) * settings["runs"] * 2 > c["maxRequests"]):
            raise ValueError("L2 task/runs count exceeds this experiment's request budget")
        suite = {"schemaVersion": "hmbot.rsi.memory-eval/2", "backend": BACKEND,
                 "settings": settings, "tasks": tasks,
                 "baseline": "memory" if c.get("baselineMemory") else "no-memory", "scope": "L2 content transfer; development set"}
        write(tmp / "suite.json", suite)
        write(tmp / "frozen.json", manifest(tmp))
        os.rename(tmp, output)
        return {"suite": str(output / "suite.json"), "tasks": len(tasks), "scope": suite["scope"], "suiteSha256": sha((output / "suite.json").read_bytes())}
    except BaseException:
        shutil.rmtree(tmp)
        raise


def load_suite(directory):
    root = Path(directory).resolve()
    frozen = read(root / "frozen.json")
    actual = {p: sha((root / p).read_bytes()) for p in frozen}
    if actual != frozen:
        raise ValueError("frozen evaluation inputs changed")
    for sub in ("candidate-memory", "baseline-memory", "inputs"):
        if (root / sub).exists() and set(manifest(root / sub)) != {p[len(sub) + 1:] for p in frozen if p.startswith(sub + "/")}:
            raise ValueError("frozen evaluation files added or removed")
    suite = read(root / "suite.json")
    if suite.get("schemaVersion") != "hmbot.rsi.memory-eval/2" or suite.get("backend") != BACKEND:
        raise ValueError("Legacy or changed evaluation backend; keep old results read-only and freeze a new suite")
    runner, lib = backend()
    return root, suite, runner, lib


def run(directory, replies):
    root, suite, runner, _ = load_suite(directory)
    replies = Path(replies).resolve()
    if replies.is_relative_to(root) or root.is_relative_to(replies):
        raise ValueError("reply output must be separate from the frozen suite")
    if replies.exists():
        raise ValueError("reply output must be new; no implicit paid retries or cross-version cache")
    replies.mkdir(parents=True)
    settings = suite["settings"]
    suite_hash = sha((root / "suite.json").read_bytes())
    # A worker is one isolated Messages request. Only prompt inputs and selected
    # lessons are sent; the model cannot browse traces, checks or reference files.
    for entry in suite["tasks"]:
        task = entry["task"]
        for repetition in range(settings["runs"]):
            arms = ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")
            for arm in arms:
                memory = root / (arm + "-memory")
                with_memory = arm == "candidate" or suite["baseline"] == "memory"
                arm_task = {**task, "lessons": task.get("baselineLessons", task.get("lessons", []))} if arm == "baseline" else task
                prompt = runner.build_prompt(arm_task, root, memory, with_memory)
                name = f"{task['id']}.{arm}.{repetition}"
                # Prompt is a useful, auditable artifact, but has no key.
                (replies / (name + ".prompt.txt")).write_text(prompt, encoding="utf-8")
                try:
                    reply, meta = runner.call_model(settings["baseUrl"], settings["model"], prompt, settings["maxTokens"], settings["temperature"], "off", settings["stallTimeout"])
                    (replies / (name + ".txt")).write_text(reply, encoding="utf-8")
                    write(replies / (name + ".meta.json"), {**meta, "suiteSha256": suite_hash, "promptSha256": sha(prompt.encode()), "replySha256": sha(reply.encode()), "model": settings["model"]})
                except Exception:
                    # Do not print provider response bodies, headers or keys.
                    write(replies / (name + ".error.json"), {"error": "MODEL_REQUEST_FAILED", "suiteSha256": suite_hash})
                load_suite(directory)
    return score(directory, replies)


def score(directory, replies):
    root, suite, runner, lib = load_suite(directory)
    replies = Path(replies)
    suite_hash = sha((root / "suite.json").read_bytes())
    rows, improved, regressed, invalid = [], 0, 0, 0
    totals = {"baseline": 0, "candidate": 0}
    for entry in suite["tasks"]:
        task = entry["task"]
        for repetition in range(suite["settings"]["runs"]):
            pair = {}
            for arm in totals:
                name = f"{task['id']}.{arm}.{repetition}"
                try:
                    body, meta = (replies / (name + ".txt")).read_text(encoding="utf-8"), read(replies / (name + ".meta.json"))
                    arm_task = {**task, "lessons": task.get("baselineLessons", task.get("lessons", []))} if arm == "baseline" else task
                    prompt = runner.build_prompt(arm_task, root, root / (arm + "-memory"), arm == "candidate" or suite["baseline"] == "memory")
                    if meta.get("suiteSha256") != suite_hash or meta.get("promptSha256") != sha(prompt.encode()) or meta.get("replySha256") != sha(body.encode()) or meta.get("stop_reason") != "end_turn" or meta.get("model") != suite["settings"]["model"]:
                        raise ValueError("incomplete or unbound reply")
                    # Require every exact target, not upstream's single-file
                    # fallback, which could score the wrong file as a success.
                    extracted = runner.extract_files(body)
                    if not all(target in extracted for target in task["targets"]):
                        raise ValueError("reply missing a target file")
                    checks = [{"id": c["id"], "pass": bool(lib.run_check(c, extracted[c.get('target', task['targets'][0])]))} for c in task["checks"]]
                    pair[arm] = {"valid": True, "pass": all(c["pass"] for c in checks), "checks": checks}
                except (OSError, ValueError, KeyError, TypeError):
                    pair[arm] = {"valid": False, "pass": None}
            valid = all(p["valid"] for p in pair.values())
            if valid:
                for arm in totals:
                    totals[arm] += int(pair[arm]["pass"])
                improved += int(pair["candidate"]["pass"] and not pair["baseline"]["pass"])
                regressed += int(pair["baseline"]["pass"] and not pair["candidate"]["pass"])
            else:
                invalid += 1
            rows.append({"task": task["id"], "run": repetition, "valid": valid, **pair})
    count, valid_count = len(rows), len(rows) - invalid
    complete = invalid == 0 and valid_count >= suite["settings"]["minValid"]
    accepted = complete and improved > 0 and regressed == 0
    result = {"schemaVersion": "hmbot.rsi.memory-evaluation-result/1", "scope": suite["scope"], "suiteSha256": suite_hash,
              "sampleCount": count, "validCount": valid_count, "improved": improved, "regressed": regressed,
              "baseline": totals["baseline"] / count, "candidate": totals["candidate"] / count,
              "gatePassed": complete and regressed == 0, "accepted": accepted,
              "verdict": "improved" if accepted else "inconclusive" if not complete else "regressed" if regressed else "no-improvement", "rows": rows}
    write(replies / "report.json", result)
    return result


def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["prepare", "run", "score"])
    ap.add_argument("--config")
    ap.add_argument("--suite")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.action == "prepare":
        if not args.config:
            ap.error("prepare requires --config")
        result = prepare(args.config, args.out)
    else:
        if not args.suite:
            ap.error("run/score requires --suite directory")
        result = (run if args.action == "run" else score)(args.suite, args.out)
    summary = {k: v for k, v in result.items() if k != "rows"}
    if args.action != "prepare":
        summary["report"] = str(Path(args.out).resolve() / "report.json")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
