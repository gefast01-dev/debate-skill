#!/usr/bin/env python3
"""Check or update this debate skill from its public GitHub repository."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY = "gefast01-dev/debate-skill"
BRANCH = "main"
REMOTE_PREFIX = "skills/debate/"
EXCLUDED = {"settings.json"}
ALLOWED_TOP_LEVEL = {"SKILL.md", "agents", "references", "scripts", "assets"}


class UpdateError(Exception):
    pass


def api_json(path):
    request = Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "debate-skill-updater"},
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def remote_bytes(commit, relative):
    request = Request(
        f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{REMOTE_PREFIX}{relative}",
        headers={"User-Agent": "debate-skill-updater"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def allowed_relative(path):
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or path in EXCLUDED:
        return False
    return len(pure.parts) == 1 and pure.name == "SKILL.md" or (
        len(pure.parts) > 1 and pure.parts[0] in ALLOWED_TOP_LEVEL - {"SKILL.md"}
    )


def skill_root(value=None):
    root = Path(value).expanduser() if value else Path(__file__).resolve().parents[1]
    root = root.resolve()
    if not (root / "SKILL.md").is_file():
        raise UpdateError(f"Not a debate skill directory: {root}")
    return root


def remote_manifest():
    commit = api_json(f"/repos/{REPOSITORY}/commits/{BRANCH}").get("sha")
    if not isinstance(commit, str) or len(commit) < 12:
        raise UpdateError("GitHub returned no usable commit SHA.")
    tree = api_json(f"/repos/{REPOSITORY}/git/trees/{commit}?recursive=1").get("tree")
    if not isinstance(tree, list):
        raise UpdateError("GitHub returned no usable repository tree.")
    files = []
    for entry in tree:
        path = entry.get("path") if isinstance(entry, dict) else None
        if entry.get("type") != "blob" or not isinstance(path, str) or not path.startswith(REMOTE_PREFIX):
            continue
        relative = path[len(REMOTE_PREFIX):]
        if allowed_relative(relative):
            files.append(relative)
    if "SKILL.md" not in files:
        raise UpdateError("The remote repository has no skills/debate/SKILL.md.")
    return commit, sorted(files)


def destination(root, relative):
    path = (root / PurePosixPath(relative)).resolve()
    if not path.is_relative_to(root):
        raise UpdateError(f"Refusing path outside skill directory: {relative}")
    return path


def inspect(root):
    commit, files = remote_manifest()
    changes = []
    content = {}
    for relative in files:
        data = remote_bytes(commit, relative)
        content[relative] = data
        local = destination(root, relative)
        old = local.read_bytes() if local.is_file() else None
        if old != data:
            changes.append({
                "path": relative,
                "status": "new" if old is None else "modified",
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    return commit, content, changes


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".debate-update-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(root, apply):
    commit, content, changes = inspect(root)
    result = {
        "ok": True,
        "repository": REPOSITORY,
        "remote_commit": commit,
        "changes": changes,
        "preserved": ["settings.json", "Debates/"],
    }
    if not changes:
        result["action"] = "up_to_date"
        return result
    if not apply:
        result["action"] = "update_available"
        return result
    for change in changes:
        atomic_write(destination(root, change["path"]), content[change["path"]])
    result["action"] = "updated"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check GitHub without changing files (default).")
    mode.add_argument("--apply", action="store_true", help="Check first, then update changed skill files.")
    parser.add_argument("--skill-root", help="Explicit skill folder, only for testing or a nonstandard install.")
    args = parser.parse_args()
    try:
        result = run(skill_root(args.skill_root), apply=args.apply)
    except (HTTPError, URLError, OSError, ValueError, UpdateError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

