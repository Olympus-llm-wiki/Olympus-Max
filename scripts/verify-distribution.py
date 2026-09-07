#!/usr/bin/env python3
"""Verify packaged files and the complete skill inventory, without network or Git."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def safe_path(root, relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or not p.parts or any(x in {"..", "."} for x in p.parts):
        raise ValueError("invalid_manifest_path")
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink_in_packaged_file: " + relative)
    return current


def skill_files(root):
    found = set()
    directory = root / ".agents/skills"
    if directory.is_symlink():
        raise ValueError("skill_root_is_symlink")
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            p = Path(parent) / name
            if p.is_symlink():
                raise ValueError("symlink_in_skills: " + str(p.relative_to(root)))
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".pyc"):
                found.add((Path(parent) / name).relative_to(root).as_posix())
    return found


def verify(root):
    manifest = json.loads((root / "DISTRIBUTION.json").read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise ValueError("invalid_distribution_manifest")
    issues = []
    for relative, expected in sorted(manifest["files"].items()):
        path = safe_path(root, relative)
        if not path.is_file():
            issues.append({"path": relative, "code": "missing"})
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            issues.append({"path": relative, "code": "changed"})
    expected_skills = {p for p in manifest["files"] if p.startswith(".agents/skills/")}
    for path in sorted(skill_files(root) - expected_skills):
        issues.append({"path": path, "code": "unexpected_skill_file"})
    # A relative link keeps both agents on the same reviewed source files.
    link = root / ".claude/skills"
    if not link.is_symlink() or os.readlink(link) != "../.agents/skills":
        issues.append({"path": ".claude/skills", "code": "claude_skill_link_changed"})
    return {"state": "verified" if not issues else "failed", "files_checked": len(manifest["files"]),
            "skills": len(list((root / ".agents/skills").glob("*/SKILL.md"))), "issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = verify(args.root.resolve())
    except (OSError, ValueError, TypeError) as error:
        result = {"state": "failed", "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["state"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
