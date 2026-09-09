import json
from pathlib import Path
import subprocess
import sys
import unittest
import shutil
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "debate" / "scripts" / "debate_state.py"


class DebateStateTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(__file__).resolve().parents[1] / "work"
        self.work.mkdir(exist_ok=True)
        self.case = self.work / ("debate-test-" + uuid.uuid4().hex)
        self.case.mkdir()
        self.addCleanup(self.cleanup_case)
        self.root = self.case / "registry"
        self.source = self.case / "source.md"
        self.source.write_text('Задача: сохранить "точные слова".\n$(literal) `literal`\nЕще строка.', encoding="utf-8")

    def cleanup_case(self):
        if self.case.resolve().parent != self.work.resolve():
            raise RuntimeError("Refusing cleanup outside test workspace")
        shutil.rmtree(self.case)

    def call(self, *args, scope="session-A", ok=True):
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--root", str(self.root), "--scope", scope, *args],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode == 0, ok, process.stderr + process.stdout)
        self.assertEqual(result["ok"], ok)
        return result

    def create(self, title="База знаний", scope="session-A"):
        return self.call("new", "--title", title, "--source-file", str(self.source), scope=scope)

    def test_empty_list_is_read_only(self):
        result = self.call("list")
        self.assertEqual(result["tasks"], [])
        self.assertFalse(self.root.exists())

    def test_new_preserves_source_and_starts_only_first_file(self):
        task = self.create()
        self.assertEqual(task["phase"], "interview")
        self.assertEqual(task["participant"], "A")
        self.assertEqual(task["exists"], {"task": True, "discussion": False, "result": False})
        contents = Path(task["files"]["task"]).read_text(encoding="utf-8")
        quoted = "\n".join("> " + line for line in self.source.read_text(encoding="utf-8").split("\n"))
        self.assertIn(quoted, contents)

    def test_a_b_a_handoff_remembers_both_bindings(self):
        task = self.create()
        b = self.call("use", "--participant", "B", scope="session-B")
        self.assertEqual(b["id"], task["id"])
        self.assertEqual(b["participant"], "B")
        self.assertEqual(self.call("use")["participant"], "A")
        self.assertEqual(self.call("use", scope="session-B")["participant"], "B")

    def test_new_task_does_not_steal_existing_context_binding(self):
        first = self.create()
        self.create("Другой проект", scope="session-C")
        self.assertEqual(self.call("use")["id"], first["id"])
        before = (self.root / "index.json").read_bytes()
        ambiguous = self.call("use", scope="session-new", ok=False)
        self.assertEqual(ambiguous["code"], "AMBIGUOUS")
        self.assertEqual(len(ambiguous["candidates"]), 2)
        self.assertEqual(before, (self.root / "index.json").read_bytes())
        selected = self.call("use", "--task", "База знаний", "--participant", "B", scope="session-new")
        self.assertEqual(selected["id"], first["id"])
        self.assertEqual(self.call("use", scope="session-new")["id"], first["id"])

    def test_switching_tasks_remembers_participant_per_task(self):
        first = self.create()
        second = self.create("Вторая")
        self.call("use", "--task", first["id"], "--participant", "B")
        self.call("use", "--task", second["id"])
        self.assertEqual(self.call("use")["participant"], "A")
        self.assertEqual(self.call("use", "--task", first["id"])["participant"], "B")

    def test_duplicate_titles_do_not_overwrite_or_silently_select(self):
        first = self.create()
        original = Path(first["files"]["task"]).read_bytes()
        second = self.create()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(original, Path(first["files"]["task"]).read_bytes())
        self.assertEqual(self.call("use", "--task", "База знаний", ok=False)["code"], "AMBIGUOUS")

    def test_status_does_not_rewrite_registry(self):
        self.create()
        before = (self.root / "index.json").read_bytes()
        self.call("status")
        self.assertEqual(before, (self.root / "index.json").read_bytes())

    def test_missing_bound_file_does_not_switch_to_another_task(self):
        first = self.create()
        self.create("Другая", scope="session-C")
        Path(first["files"]["task"]).unlink()
        self.assertEqual(self.call("use", ok=False)["code"], "MISSING_TASK_FILE")

    def test_completed_requires_saved_result_and_remains_addressable(self):
        task = self.create()
        self.assertEqual(self.call("phase", "--task", task["id"], "--value", "completed", ok=False)["code"], "NO_RESULT")
        Path(task["files"]["result"]).write_text("Итог пробной задачи.", encoding="utf-8")
        self.call("phase", "--task", task["id"], "--value", "completed")
        self.assertEqual(self.call("use")["id"], task["id"])
        self.assertEqual(self.call("use", scope="new-context", ok=False)["code"], "NO_ACTIVE_TASK")
        self.assertEqual(self.call("use", "--task", task["id"], scope="new-context")["id"], task["id"])

    def test_corrupt_registry_is_preserved(self):
        self.create()
        index = self.root / "index.json"
        index.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.call("use", ok=False)["code"], "INVALID_JSON")
        self.assertEqual(index.read_text(encoding="utf-8"), "{broken")

    def test_missing_registry_with_task_folders_is_not_reset(self):
        self.create()
        (self.root / "index.json").unlink()
        self.assertEqual(self.call("new", "--title", "Еще задача", "--source-file", str(self.source), ok=False)["code"], "MISSING_INDEX")
        self.assertFalse((self.root / "index.json").exists())

    def test_existing_lock_is_not_removed(self):
        self.create()
        lock = self.root / ".registry.lock"
        lock.write_text("Existing operation", encoding="utf-8")
        self.assertEqual(self.call("use", ok=False)["code"], "BUSY")
        self.assertEqual(lock.read_text(encoding="utf-8"), "Existing operation")

    def test_path_like_registry_task_id_is_rejected(self):
        task = self.create()
        index = self.root / "index.json"
        data = json.loads(index.read_text(encoding="utf-8"))
        record = data["tasks"].pop(task["id"])
        record["id"] = "../elsewhere"
        data["tasks"]["../elsewhere"] = record
        index.write_text(json.dumps(data), encoding="utf-8")
        before = index.read_bytes()
        self.assertEqual(self.call("use", ok=False)["code"], "INVALID_INDEX")
        self.assertEqual(before, index.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
