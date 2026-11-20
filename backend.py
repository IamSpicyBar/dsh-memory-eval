"""Repository-independent evaluation primitives owned by this plugin.

This module implements the published task/file protocol, not a dynamic import
of another evaluation repository. Network calls have no retry loop.
"""
import json
from pathlib import Path, PurePosixPath
import re

from model_client import call_model

VERSION = "studio-memory-backend/1"
TASK_SCHEMA = """# Frozen validation task

One JSON or YAML object per task. Required: id, prompt, targets (relative file
paths), checks (unique id, kind, optional target). Inputs: [{path, title?}].
lessons and baselineLessons contain exact lesson IDs. cases are evidence IDs.
Checks support regex_all, regex_none, contains_all, contains_none and exact.
Regex checks use a non-empty patterns array; contains checks use a non-empty
values array; exact uses value. All checks must distinguish the supplied
historical failure from its correct reference. baselines/references may map
target paths to explicit files; otherwise use the selected baseline/project
directories. Task inputs and Memory are model-visible. Checks and references
are grader-only. Output: ===FILE: relative/path===, file body, ===END===.
This task contract is shared by content comparison and executor validation;
preparing it does not require running an L2 model experiment.
"""


def load_yaml(path):
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def relative_target(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Target must be a relative POSIX file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts):
        raise ValueError("Unsafe validation target")
    return value


def resolve(value, project, roots=()):
    path = Path(value)
    if path.is_absolute():
        choices = [path]
    else:
        if ".." in path.parts:
            raise ValueError("Validation inputs cannot traverse parent directories")
        choices = [Path(root) / path for root in (project, *roots)]
    for candidate in choices:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    raise ValueError("Missing explicitly declared validation input: " + str(value))


def baseline_for(task, target, baseline, project, roots=()):
    relative_target(target)
    selected = task.get("baselines", {}).get(target) or (task.get("baseline_path") if len(task.get("targets", [])) == 1 else None)
    return resolve(selected, project, roots) if selected else resolve(target, baseline, ())


def reference_for(task, target, project, roots=()):
    relative_target(target)
    selected = task.get("references", {}).get(target) or (task.get("reference_path") if len(task.get("targets", [])) == 1 else None)
    return resolve(selected or target, project, roots)


def checks_for_target(task, target):
    return [check for check in task["checks"] if check.get("target", task["targets"][0]) == target]


def run_check(check, text):
    if not isinstance(text, str) or len(text) > 32 * 1024 * 1024:
        raise ValueError("Invalid validation artifact")
    kind = check.get("kind")
    if kind in {"regex_all", "regex_none"}:
        patterns = check.get("patterns")
        if not isinstance(patterns, list) or not patterns or any(not isinstance(p, str) or not p or len(p) > 10000 for p in patterns):
            raise ValueError("Regex checks require non-empty patterns")
        matches = [bool(re.search(pattern, text, re.MULTILINE)) for pattern in patterns]
        return all(matches) if kind == "regex_all" else not any(matches)
    if kind in {"contains_all", "contains_none"}:
        values = check.get("values")
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise ValueError("Contains checks require non-empty values")
        return all(v in text for v in values) if kind == "contains_all" else not any(v in text for v in values)
    if kind == "exact" and isinstance(check.get("value"), str):
        return text == check["value"]
    raise ValueError("Unsupported validation check kind: " + str(kind))


def find_lesson_text(root, identity):
    if not re.fullmatch(r"lesson-[A-Za-z0-9_-]+", str(identity)):
        raise ValueError("Invalid lesson identity")
    root = Path(root).resolve()
    matches = [path for path in root.rglob(str(identity) + ".lesson.md") if path.is_file() and not path.is_symlink()]
    if len(matches) != 1:
        return None
    path = matches[0]
    if not path.resolve().is_relative_to(root) or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Invalid reading-pack lesson")
    return path.read_text(encoding="utf-8")


def build_prompt(task, project, memory, with_memory):
    targets = [relative_target(value) for value in task["targets"]]
    sections = ["Complete the task using only the supplied inputs. Return each requested file in this exact envelope:\n===FILE: relative/path===\nfile body\n===END===",
                "Requested output files: " + json.dumps(targets), "Task:\n" + task["prompt"]]
    for item in task.get("inputs", []):
        path = resolve(item["path"], project)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Model input exceeds 4 MiB")
        text = path.read_text(encoding="utf-8")
        if "lines" in item:
            span = item["lines"]
            if not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span) or not 1 <= span[0] <= span[1] <= len(text.splitlines()):
                raise ValueError("Declared input line range is invalid")
            text = "\n".join(text.splitlines()[span[0] - 1:span[1]])
        sections.append("Input " + str(item.get("title", Path(item["path"]).name)) + (" lines " + str(item["lines"]) if "lines" in item else "") + ":\n" + text)
    if with_memory:
        for identity in task.get("lessons", []):
            text = find_lesson_text(memory, identity)
            if not text:
                raise ValueError("Selected lesson is absent from the frozen reading pack")
            sections.append("Applicable experience (check its conditions against this task):\n" + text)
    return "\n\n".join(sections)


def extract_files(text):
    result = {}
    for match in re.finditer(r"^===FILE:\s*([^\r\n]+?)\s*===\r?\n(.*?)\r?\n===END===\s*$", text, re.MULTILINE | re.DOTALL):
        name = relative_target(match.group(1))
        if name in result:
            raise ValueError("Duplicate output file envelope")
        result[name] = match.group(2)
    return result


