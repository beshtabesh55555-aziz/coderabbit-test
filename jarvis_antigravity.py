"""
JARVIS Antigravity Deep Bridge Module
====================================
Provides full bidirectional integration with Google Antigravity:
- Working with documents (read, create, edit, summarize)
- Reading Antigravity conversations & transcripts
- Spawning / initializing new project workspaces
- Submitting tasks to Antigravity and tracking execution
"""

import os
import sys
import json
import time
import shutil
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("JarvisAntigravity")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-AGY] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class JarvisAntigravity:
    """Enterprise-grade bridge between Monika and Google Antigravity."""

    def __init__(self, tasks_file: Optional[Path] = None):
        self.base_dir = Path(__file__).resolve().parent
        self.antigravity_root = Path(os.environ.get("ANTIGRAVITY_ROOT", Path.home() / ".gemini" / "antigravity"))
        self.brain_dir = self.antigravity_root / "brain"
        self.scratch_dir = self.antigravity_root / "scratch"
        self.tasks_file = tasks_file or (self.base_dir / "antigravity_tasks.json")
        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
        if not conv_id and self.brain_dir.exists():
            try:
                dirs = [d for d in self.brain_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
                if dirs:
                    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    conv_id = dirs[0].name
            except Exception:
                conv_id = ""
        self.current_conv_id = conv_id
        self._history: List[Dict[str, Any]] = self._load_persisted_tasks()

    def _load_persisted_tasks(self) -> List[Dict[str, Any]]:
        """Load saved tasks from disk."""
        if self.tasks_file.exists():
            try:
                with open(self.tasks_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_persisted_tasks(self):
        """Save tasks list atomically to disk."""
        try:
            tmp = self.tasks_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._history, f, indent=2, ensure_ascii=False)
            tmp.replace(self.tasks_file)
        except Exception as e:
            logger.error(f"Failed to persist antigravity tasks: {e}")

    def list_projects(self) -> List[Dict[str, Any]]:
        """List all project workspaces in Antigravity scratch directory."""
        projects = []
        if self.scratch_dir.exists():
            for p in self.scratch_dir.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    readme = p / "README.md"
                    has_readme = readme.exists()
                    desc = ""
                    if has_readme:
                        try:
                            with open(readme, "r", encoding="utf-8") as f:
                                desc = f.readline().replace("#", "").strip()
                        except Exception:
                            desc = ""
                    projects.append({
                        "name": p.name,
                        "path": str(p),
                        "has_readme": has_readme,
                        "description": desc or "Antigravity Project Workspace"
                    })
        return projects

    def create_project(self, project_name: str, description: str = "") -> Dict[str, Any]:
        """Create a new project workspace under Antigravity scratch directory."""
        clean_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_name.strip())
        target_dir = self.scratch_dir / clean_name
        target_dir.mkdir(parents=True, exist_ok=True)

        readme_file = target_dir / "README.md"
        if not readme_file.exists():
            with open(readme_file, "w", encoding="utf-8") as f:
                f.write(f"# {clean_name}\n\n{description or 'Loyihaning qisqacha tavsifi.'}\n\n"
                        f"*Yaratilgan sana:* {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"*Muallif:* Monika AI / JARVIS System\n")

        gitignore = target_dir / ".gitignore"
        if not gitignore.exists():
            with open(gitignore, "w", encoding="utf-8") as f:
                f.write("__pycache__/\n*.pyc\n.env\n*.tmp\n")

        logger.info(f"New Antigravity project workspace created: {target_dir}")
        return {
            "project_name": clean_name,
            "path": str(target_dir),
            "status": "CREATED",
            "message": f"'{clean_name}' nomli yangi Antigravity loyiha maydoni muvaffaqiyatli yaratildi."
        }

    def get_recent_conversations(self, limit: int = 5) -> List[Dict[str, Any]]:
        """List active/recent Antigravity conversations and their artifacts."""
        convs = []
        if self.brain_dir.exists():
            dirs = sorted(self.brain_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
            for d in dirs:
                if d.is_dir() and not d.name.startswith("."):
                    artifacts = [a.name for a in d.glob("*.md")]
                    convs.append({
                        "conv_id": d.name,
                        "is_active": d.name == self.current_conv_id,
                        "artifacts_count": len(artifacts),
                        "artifacts": artifacts[:3]
                    })
                    if len(convs) >= limit:
                        break
        return convs

    def read_latest_chat_summary(self, max_lines: int = 10) -> str:
        """Read recent exchanges from current conversation transcript."""
        transcript_file = self.brain_dir / self.current_conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_file.exists():
            return "Antigravity muloqot jurnali topilmadi."

        entries = []
        try:
            with open(transcript_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            obj = json.loads(line)
                            t = obj.get("type", "")
                            if t in ("USER_INPUT", "PLANNER_RESPONSE"):
                                content = obj.get("content", "")
                                if content and isinstance(content, str):
                                    role = "Foydalanuvchi" if t == "USER_INPUT" else "Antigravity"
                                    clean_txt = content.strip().replace("\n", " ")[:100]
                                    entries.append(f"{role}: {clean_txt}")
                        except Exception:
                            continue
        except Exception as e:
            logger.error(f"Error reading transcript: {e}")

        recent = entries[-max_lines:] if entries else ["Muloqot yozuvlari hali mavjud emas."]
        return "\n".join(recent)

    def work_with_document(self, doc_path: str, action: str = "read", content: Optional[str] = None) -> Dict[str, Any]:
        """
        Handle documents (read, create, or update).
        Supported formats: .md, .txt, .json, .py, etc.
        """
        target = Path(doc_path)
        if not target.is_absolute():
            target = self.base_dir / doc_path

        if action == "read":
            if not target.exists():
                return {"status": "error", "message": f"Hujjat topilmadi: {target}"}
            try:
                with open(target, "r", encoding="utf-8") as f:
                    data = f.read()
                return {"status": "success", "content": data, "size": len(data), "path": str(target)}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif action in ("create", "write"):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content or "")
                return {"status": "success", "message": f"Hujjat saqlandi: {target}", "path": str(target)}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        return {"status": "error", "message": f"Noma'lum amal: {action}"}

    def delegate_task(self, task_prompt: str, workspace_path: Optional[str] = None) -> Dict[str, Any]:
        """Delegate programming, engineering, or research task to Antigravity."""
        self._history = self._load_persisted_tasks()
        task_id = f"AGY-{int(time.time())}-{len(self._history) + 1}"
        speech_summary = f"Vazifa Antigravity muhandislik tizimi navbatiga muvaffaqiyatli qabul qilindi (Topshiriq ID: {task_id})."

        task_record = {
            "task_id": task_id,
            "status": "QUEUED",
            "prompt": task_prompt,
            "workspace": workspace_path or str(self.base_dir),
            "speech_summary": speech_summary,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time()
        }
        self._history.append(task_record)
        self._save_persisted_tasks()
        logger.info(f"Antigravity task queued: {task_id}")
        return task_record

    def get_delegation_history(self) -> List[Dict[str, Any]]:
        self._history = self._load_persisted_tasks()
        return list(self._history)


if __name__ == "__main__":
    agy = JarvisAntigravity()
    print("=== ANTIGRAVITY PROJECTS ===")
    for p in agy.list_projects():
        print(f" - {p['name']}: {p['path']}")
    print("\n=== RECENT CHAT SUMMARY ===")
    print(agy.read_latest_chat_summary(max_lines=3))
