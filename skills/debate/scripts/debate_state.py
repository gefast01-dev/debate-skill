#!/usr/bin/env python3
"""Local task registry for the debate skill; no network or model calls."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


PHASES = (
    "interview", "awaiting_confirmation", "discussion", "awaiting_user",
    "ready_to_finalize", "completed",
)
ID_PATTERN = re.compile(r"\d{8}-[0-9a-f]{8}\Z")
FILE_NAMES = {
    "task": "01_task.md", "discussion": "02_discussion.md", "result": "03_result.md",
}


class DebateError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.payload = {"ok": False, "code": code, "message": message, **details}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise DebateError("INVALID_JSON", f"Cannot read {path}: {exc}") from exc


def storage_root(override=None):
    configured = override or os.environ.get("DEBATE_HOME")
    if not configured:
        settings = read_json(Path(__file__).resolve().parents[1] / "settings.json")
        configured = settings.get("data_root") if isinstance(settings, dict) else None
    if not isinstance(configured, str) or not configured.strip():
        raise DebateError("NO_ROOT", "A data_root is required in settings.json.")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise DebateError("RELATIVE_ROOT", "The storage root must be absolute.")
    return root.resolve()


def scope_id(override=None):
    if override:
        return override
    thread_id = os.environ.get("CODEX_THREAD_ID")
    return f"thread:{thread_id}" if thread_id else f"cwd:{os.path.normcase(str(Path.cwd().resolve()))}"


def load_index(root):
    path = root / "index.json"
    if not path.exists():
        if root.exists() and any(p.is_dir() and ID_PATTERN.fullmatch(p.name) for p in root.iterdir()):
            raise DebateError("MISSING_INDEX", "Task folders exist but index.json is missing; do not reset the registry.")
        return {"schema_version": 1, "tasks": {}, "bindings": {}}
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise DebateError("INVALID_INDEX", "Unsupported registry schema.")
    if not isinstance(data.get("tasks"), dict) or not isinstance(data.get("bindings"), dict):
        raise DebateError("INVALID_INDEX", "Invalid tasks or bindings in registry.")
    for task_id, task in data["tasks"].items():
        if (not ID_PATTERN.fullmatch(task_id) or not isinstance(task, dict)
                or task.get("id") != task_id or not isinstance(task.get("title"), str)
                or task.get("phase") not in PHASES):
            raise DebateError("INVALID_INDEX", f"Invalid task record: {task_id}")
    for binding in data["bindings"].values():
        if (not isinstance(binding, dict) or not isinstance(binding.get("participants", {}), dict)
                or not isinstance(binding.get("task_id"), str)):
            raise DebateError("INVALID_INDEX", "Invalid context binding.")
    return data


def save_index(root, data):
    fd, tmp_name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=root)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, root / "index.json")
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def registry_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".registry.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DebateError("BUSY", "Registry is locked by another operation. Retry after it finishes; do not remove a live lock.") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\ncreated={now()}\n")
        yield
    finally:
        lock.unlink()


def paths_for(root, task_id):
    if not ID_PATTERN.fullmatch(task_id):
        raise DebateError("INVALID_ID", "Invalid task ID.")
    directory = (root / task_id).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise DebateError("OUTSIDE_ROOT", "Task folder resolves outside the storage root.")
    paths = {key: (directory / name).resolve() for key, name in FILE_NAMES.items()}
    if any(not p.is_relative_to(directory) for p in paths.values()):
        raise DebateError("OUTSIDE_TASK", "A task file resolves outside its task folder.")
    return directory, paths


def choices(tasks):
    return [{key: task[key] for key in ("id", "title", "phase")} for task in tasks]


def resolve_task(data, scope, selector=None):
    tasks = data["tasks"]
    if selector:
        query = selector.strip().casefold()
        if selector in tasks:
            return tasks[selector]
        matches = [t for t in tasks.values() if t["title"].casefold() == query]
        if not matches:
            matches = [t for t in tasks.values() if t["id"].startswith(query) or query in t["title"].casefold()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise DebateError("AMBIGUOUS", "Choose a task by title or ID.", candidates=choices(matches))
        raise DebateError("NOT_FOUND", "No matching task. Use list to inspect available tasks.")
    bound = data["bindings"].get(scope, {}).get("task_id")
    if bound:
        if bound not in tasks:
            raise DebateError("MISSING_BOUND_TASK", "The bound task is missing; explicitly select another task.")
        return tasks[bound]
    active = [t for t in tasks.values() if t["phase"] != "completed"]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise DebateError("AMBIGUOUS", "This context has no task binding. Choose a task.", candidates=choices(active))
    raise DebateError("NO_ACTIVE_TASK", "No unfinished task is selected. Create a new task or select an existing one.", candidates=choices(tasks.values()))


def bind(data, scope, task_id, participant=None):
    binding = data["bindings"].setdefault(scope, {"task_id": task_id, "participants": {}})
    binding["task_id"] = task_id
    if participant is not None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", participant):
            raise DebateError("INVALID_PARTICIPANT", "Use a stable label such as A or B.")
        binding.setdefault("participants", {})[task_id] = participant


def present(root, data, scope, task):
    directory, paths = paths_for(root, task["id"])
    if not paths["task"].is_file():
        raise DebateError("MISSING_TASK_FILE", f"The task file is missing: {paths['task']}")
    participant = data["bindings"].get(scope, {}).get("participants", {}).get(task["id"])
    return {
        "ok": True, **task, "root": str(root), "directory": str(directory),
        "scope": scope, "participant": participant,
        "files": {key: str(path) for key, path in paths.items()},
        "exists": {key: path.exists() for key, path in paths.items()},
    }


def initial_document(source):
    # Prefix each source line as a quotation without interpreting it as shell code.
    quoted = "\n".join("> " + line for line in source.split("\n"))
    return (
        "# Постановка задачи\n\nСтатус: Интервью\n\n"
        "## Исходное сообщение пользователя\n\n" + quoted + "\n\n"
        "## Уточненная постановка\n\nФормируется во время интервью.\n\n"
        "## История интервью\n\n"
        "## Неразрешенные вопросы\n\nПредстоит выявить при интервью.\n\n"
        "## Подтверждение постановки пользователем\n\nПока не получено.\n"
    )


def run(args):
    root = storage_root(args.root)
    scope = scope_id(args.scope)
    if args.command in ("list", "status"):
        data = load_index(root)
        if args.command == "list":
            return {"ok": True, "root": str(root), "tasks": choices(data["tasks"].values())}
        return present(root, data, scope, resolve_task(data, scope, args.task))

    with registry_lock(root):
        data = load_index(root)
        if args.command == "new":
            source = Path(args.source_file).read_text(encoding="utf-8-sig")
            if not source.strip() or not args.title.strip():
                raise DebateError("EMPTY_TASK", "Both a title and a source task are required.")
            # Validate the label before creating any task files.
            if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", args.participant):
                raise DebateError("INVALID_PARTICIPANT", "Use a stable label such as A or B.")
            task_id = datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]
            directory, paths = paths_for(root, task_id)
            directory.mkdir()
            with paths["task"].open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(initial_document(source))
            task = {"id": task_id, "title": args.title.strip(), "phase": "interview", "created_at": now(), "updated_at": now()}
            data["tasks"][task_id] = task
            bind(data, scope, task_id, args.participant)
        else:
            task = resolve_task(data, scope, args.task)
            present(root, data, scope, task)  # Validate the selected files before writing registry state.
            if args.command == "use":
                bind(data, scope, task["id"], args.participant)
            else:
                if args.value == "completed":
                    _, paths = paths_for(root, task["id"])
                    if not paths["result"].is_file() or not paths["result"].read_text(encoding="utf-8-sig").strip():
                        raise DebateError("NO_RESULT", "Save a nonempty 03_result.md before marking the task completed.")
                task["phase"] = args.value
                task["updated_at"] = now()
        save_index(root, data)
        return present(root, data, scope, task)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Explicit absolute storage root, e.g. an isolated test directory")
    parser.add_argument("--scope", help="Explicit context binding; normally CODEX_THREAD_ID is used")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("new")
    create.add_argument("--title", required=True)
    create.add_argument("--source-file", required=True)
    create.add_argument("--participant", default="A")
    sub.add_parser("list")
    status = sub.add_parser("status")
    status.add_argument("--task")
    use = sub.add_parser("use")
    use.add_argument("--task")
    use.add_argument("--participant")
    phase = sub.add_parser("phase")
    phase.add_argument("--task", required=True)
    phase.add_argument("--value", required=True, choices=PHASES)
    args = parser.parse_args()
    try:
        result = run(args)
    except DebateError as exc:
        result = exc.payload
    except OSError as exc:
        result = {"ok": False, "code": "FILESYSTEM_ERROR", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
