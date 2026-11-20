"""Read-only validation of the documented MigLoop Memory JSON format.

No imports from a Memory engine or source checkout. This is structural and
provenance validation; it does not reproduce a generator's semantic lint.
"""
import hashlib
import json
from pathlib import Path
import re

VERSION = "studio-migloop-format/1"
SCOPE = "snapshot/card content hashes, lesson structure, exact evidence and dependency bindings"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Memory input must be an ordinary JSON file below 32 MiB")
    return json.loads(path.read_text(encoding="utf-8"))


def revision(value):
    if not re.fullmatch(r"[a-f0-9]{64}", str(value)):
        raise ValueError("Invalid Memory content revision")
    return value


def snapshot(store, selected=None):
    root = Path(store).resolve()
    selected = revision(selected or read_json(root / "HEAD.json")["revision"])
    value = read_json(root / "snapshots" / (selected + ".json"))
    if value.get("schema") != "migloop-memory/1" or value.get("revision") != selected:
        raise ValueError("Unsupported Memory snapshot format or identity")
    if fingerprint({k: v for k, v in value.items() if k != "revision"}) != selected:
        raise ValueError("Memory snapshot content hash mismatch")
    if not isinstance(value.get("lessons"), dict) or not isinstance(value.get("cases"), dict):
        raise ValueError("Memory needs explicit lesson and case registries")
    return value


def card(store, identity, selected):
    if not re.fullmatch(r"case-[a-f0-9]+", str(identity)):
        raise ValueError("Invalid evidence card identity")
    selected = revision(selected)
    value = read_json(Path(store) / "cases" / identity / (selected + ".json"))
    if value.get("schema") != "migloop-case/4":
        raise ValueError("Built-in format adapter supports migloop-case/4; explicitly compact older cards with their engine first")
    if value.get("id") != identity or value.get("revision") != selected:
        raise ValueError("Evidence card identity mismatch")
    if fingerprint({k: v for k, v in value.items() if k not in {"revision", "created_at"}}) != selected:
        raise ValueError("Evidence card content hash mismatch")
    if not isinstance(value.get("summary"), str) or not isinstance(value.get("recommendations"), list) or not value.get("graphs"):
        raise ValueError("Evidence card lacks its authored claims or graphs")
    return value


def claim(value, identity):
    if identity == "diagnosis":
        text, kind = value["summary"], "diagnosis"
    elif re.fullmatch(r"recommendation:[1-9][0-9]*", str(identity)):
        index = int(identity.split(":")[1]) - 1
        if index >= len(value["recommendations"]):
            raise ValueError("Evidence recommendation is absent")
        text, kind = value["recommendations"][index], "recommendation"
    else:
        raise ValueError("Unsupported evidence claim identity")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Evidence claim is empty")
    return {"text": text, "kind": kind, "status": "model_claim"}


def lint(store, state, base):
    hits = []
    def hit(identity, rule, reason): hits.append({"lesson": identity, "rule": rule, "reason": reason})
    changed = {key: value for key, value in state["lessons"].items() if base.get("lessons", {}).get(key) != value}
    for identity, lesson in changed.items():
        if lesson.get("status") != "active":
            continue
        if lesson.get("id") != identity or not re.fullmatch(r"lesson-[A-Za-z0-9_-]+", identity):
            hit(identity, "identity", "Lesson registry key does not match its identity")
        for field in ["title", "when", "description", "why"]:
            if not isinstance(lesson.get(field), str) or not lesson[field].strip(): hit(identity, "structure", "Missing " + field)
        if not isinstance(lesson.get("how"), list) or not lesson["how"] or any(not isinstance(step, str) or not step.strip() for step in lesson["how"]):
            hit(identity, "structure", "Lesson needs explicit non-empty actions")
        if not isinstance(lesson.get("topic"), list) or not 1 <= len(lesson["topic"]) <= 3:
            hit(identity, "structure", "Topic needs one to three navigation segments")
        for dep in lesson.get("requires", []):
            if state["lessons"].get(dep, {}).get("status") != "active": hit(identity, "dependency", "Required lesson is absent or inactive")
        if not lesson.get("evidence"):
            hit(identity, "evidence", "Active lesson needs exact evidence bindings")
        for binding in lesson.get("evidence", []):
            try:
                ref = state["cases"].get(binding["case"], {})
                if ref.get("revision") != binding["revision"] or ref.get("status") != "available":
                    raise ValueError("Bound card revision is no longer available in this snapshot")
                claim(card(store, binding["case"], binding["revision"]), binding["claim"])
            except (ValueError, KeyError, OSError) as error: hit(identity, "evidence", str(error))
    visiting, done = set(), set()
    def visit(identity):
        if identity in visiting: raise ValueError("Lesson dependency cycle")
        if identity in done: return
        visiting.add(identity)
        for dep in state["lessons"].get(identity, {}).get("requires", []): visit(dep)
        visiting.remove(identity); done.add(identity)
    try:
        for identity in state["lessons"]: visit(identity)
    except ValueError as error: hit("*", "dependency", str(error))
    return {"adapter": VERSION, "scope": SCOPE, "summary": {"lessons_with_open_hits": len({h["lesson"] for h in hits}), "hits": len(hits)}, "hits": hits}
