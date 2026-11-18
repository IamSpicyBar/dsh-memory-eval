#!/usr/bin/env python3
"""Read a local a2h-export/4 without interpreting or rewriting its transcripts.

Only the verified manifest is handed to the experiment host; large raw files stay
on disk. No production credentials or database access are needed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile


def relative(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or "\\" in name or "\0" in name or any(x in ("", ".", "..") for x in name.split("/")):
        raise ValueError("unsafe export path")
    return name


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inside(root, name):
    path = root / relative(name)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("missing or linked export file: " + name)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("export path escapes root")
    return path


def inspect(root):
    root = Path(root).resolve()
    path = inside(root, "manifest.json")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("export manifest exceeds 32 MiB")
    m = json.loads(path.read_text(encoding="utf-8"))
    if m.get("format") != "a2h-export/4":
        raise ValueError("only a2h-export/4 is supported; do not guess transcript format")
    files, gaps, identities = [], [], set()
    runs = m.get("runs", [])
    if not isinstance(runs, list) or not runs:
        raise ValueError("export has no runs")
    for run in runs:
        if not run.get("run_id") or not run.get("sessions"):
            raise ValueError("run identity or raw sessions missing")
        if not any(s.get("kind") == "main" for s in run["sessions"]):
            gaps.append({"run": run["run_id"], "kind": "missing-main-session"})
        for session in run["sessions"]:
            if not session.get("session_id") or session.get("kind") not in ("main", "subagent") or not session.get("epochs"):
                raise ValueError("invalid session identity or epochs")
            for epoch in session["epochs"]:
                name = relative(epoch["file"])
                identity = (run["run_id"], session["session_id"], epoch["epoch"])
                if identity in identities or any(f["file"] == name for f in files):
                    raise ValueError("duplicate run/session/epoch or file")
                identities.add(identity)
                raw = inside(root, name)
                if raw.stat().st_size != epoch.get("bytes") or sha(raw) != epoch.get("sha256"):
                    raise ValueError("raw transcript checksum mismatch: " + name)
                if epoch.get("chain_gaps"):
                    gaps.append({"run": run["run_id"], "session": session["session_id"], "epoch": epoch["epoch"], "chain_gaps": epoch["chain_gaps"]})
                files.append({"file": name, "bytes": epoch["bytes"], "sha256": epoch["sha256"], "runId": run["run_id"], "sessionId": session["session_id"], "kind": session["kind"], "epoch": epoch["epoch"]})
    for section in ("artifacts", "summary_fallback_artifacts"):
        for item in m.get(section, []):
            if item.get("missing") or not item.get("file"):
                gaps.append({"kind": "missing-artifact", "section": section, "uploadId": item.get("upload_id")})
                continue
            raw = inside(root, item["file"])
            if raw.stat().st_size != item.get("bytes"):
                raise ValueError("artifact size mismatch")
            actual = sha(raw)
            if item.get("sha256") and actual != item["sha256"]:
                raise ValueError("artifact checksum mismatch")
            files.append({"file": item["file"], "bytes": raw.stat().st_size, "sha256": actual, "kind": section})
    return {"schemaVersion": "hmbot.rsi.flywheel-source/1", "format": m["format"],
            "root": str(root), "manifestSha256": sha(path), "project": m.get("project"),
            "migbotSessionId": m.get("migbot_session_id"), "files": files, "gaps": gaps,
            "eligible": not gaps, "binding": "source-identity-only; asset version requires separate evidence",
            "summary": {"runs": len(runs), "sessions": len({(f.get("runId"), f.get("sessionId")) for f in files if f.get("sessionId")}),
                        "epochs": len(identities), "bytes": sum(f["bytes"] for f in files)}}


def freeze(source, output, expected_sha=None, max_bytes=32 * 1024**3):
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists():
        raise ValueError("output must be a new directory")
    if output.is_relative_to(source):
        raise ValueError("output cannot be inside source")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".flywheel-", dir=output.parent))
    try:
        root = temporary / "raw"
        root.mkdir()
        if source.is_dir():
            report = inspect(source)
            names = {"manifest.json", *(f["file"] for f in report["files"])}
            if sum(inside(source, n).stat().st_size for n in names) > max_bytes:
                raise ValueError("export exceeds byte limit")
            for name in names:
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(inside(source, name), dest)
            if inspect(source) != report:
                raise ValueError("source changed while freezing")
        else:
            archive_sha = sha(source)
            if expected_sha and archive_sha != expected_sha:
                raise ValueError("archive checksum mismatch")
            total, seen = 0, set()
            with tarfile.open(source, "r|*") as archive:
                for member in archive:
                    name = relative(member.name.rstrip("/") if member.isdir() else member.name)
                    if name in seen or len(seen) >= 200000:
                        raise ValueError("duplicate or too many archive entries")
                    seen.add(name)
                    if member.isdir():
                        (root / name).mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise ValueError("export cannot contain links or special files")
                    total += member.size
                    if total > max_bytes:
                        raise ValueError("export exceeds byte limit")
                    dest = root / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, dest.open("xb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
            if sha(source) != archive_sha:
                raise ValueError("archive changed while freezing")
        report = inspect(root)
        report["root"] = str(output / "raw")
        report["source"] = str(source)
        if source.is_file():
            report["archiveSha256"] = archive_sha
        (temporary / "source.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(temporary, output)
        return report
    except BaseException:
        shutil.rmtree(temporary)
        raise


def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["inspect", "freeze"])
    ap.add_argument("--source", required=True)
    ap.add_argument("--out")
    ap.add_argument("--sha256")
    ap.add_argument("--summary", action="store_true", help="bounded host response; full freeze manifest remains in source.json")
    args = ap.parse_args()
    if args.action == "freeze" and not args.out:
        ap.error("freeze requires --out")
    result = inspect(args.source) if args.action == "inspect" else freeze(args.source, args.out, args.sha256)
    if args.summary:
        result = {**{k: v for k, v in result.items() if k not in ("files", "gaps")}, "gapCount": len(result["gaps"]), "gaps": result["gaps"][:20]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
