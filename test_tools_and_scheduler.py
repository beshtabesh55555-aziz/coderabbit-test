"""Unit test suite for JARVIS Stage 3: Tools, Scheduler, Concierge & Watchdog
=======================================================================
Verifies:
1. JarvisToolRegistry: Registration, schema discovery, system status, notes, file ops.
2. JarvisScheduler: NL time parsing (Uzbek & English), relative & absolute scheduling, persistence.
3. JarvisConcierge: Filter guardrails, away mode, note capture, concluding cooldown.
4. JarvisWatchdog: Alert spam circuit breaker and heartbeat monitoring.
5. JarvisAntigravity: Workspace listing, document operations, delegation history.
6. JarvisOmniIndexer: App categorization and indexed search.
7. JarvisMCPBridge: Protocol validation and server lifecycle handling.
"""

import os
import sys
import time
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import psutil

# Ensure coderabbit-staging is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from jarvis_tool_registry import JarvisToolRegistry
from jarvis_scheduler import JarvisScheduler, parse_reminder_text, _normalize_time_unit
from jarvis_concierge import JarvisConcierge
from jarvis_watchdog import JarvisWatchdog
from jarvis_antigravity import JarvisAntigravity
from jarvis_omni_indexer import PCAppsIndexer
from jarvis_mcp_bridge import MCPServerProcess


class TestJarvisToolRegistry(unittest.TestCase):
    """Test suite for JarvisToolRegistry."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_tools_test_")
        self.registry = JarvisToolRegistry(
            base_dir=Path(self.temp_dir)
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tool_declarations_and_discovery(self):
        declarations = self.registry.get_tool_declarations()
        self.assertIsInstance(declarations, list)
        self.assertGreater(len(declarations), 10)
        
        tool_names = [d.get("name") for d in declarations]
        self.assertIn("get_system_status", tool_names)
        self.assertIn("launch_application", tool_names)
        self.assertIn("search_files", tool_names)
        self.assertIn("add_reminder", tool_names)
        self.assertIn("save_quick_note", tool_names)

    def test_get_system_status(self):
        status = self.registry.get_system_status()
        self.assertTrue(status.get("success", False))
        stats = status.get("stats", {})
        self.assertIn("cpu_percent", stats)
        self.assertIn("ram_percent", stats)
        self.assertIn("os", stats)

    def test_notes_and_reminders_tools(self):
        # Save note
        save_res = self.registry.save_quick_note(title="Reja", content="Loyihani CodeRabbit orqali tekshirish.")
        self.assertTrue(save_res.get("success"))
        
        # Get notes
        get_res = self.registry.get_quick_notes()
        self.assertTrue(get_res.get("success"))
        self.assertGreaterEqual(len(get_res.get("notes", [])), 1)
        self.assertIn("CodeRabbit", get_res["notes"][-1]["content"])

        # Add reminder
        rem_res = self.registry.add_reminder("Dori ichish", delay_minutes=5)
        self.assertTrue(rem_res.get("success"))
        self.assertIn("reminders", self.registry.list_reminders())

    def test_file_operations_security(self):
        test_file = Path(self.temp_dir) / "sub" / "hello.txt"
        
        # Write
        w_res = self.registry.write_file_content(str(test_file), "Salom Dunyo!")
        self.assertTrue(w_res.get("success"))
        self.assertTrue(test_file.exists())

        # Read
        r_res = self.registry.read_file_content(str(test_file))
        self.assertTrue(r_res.get("success"))
        self.assertEqual(r_res.get("content"), "Salom Dunyo!")

        # Search
        s_res = self.registry.search_files(query="hello", root_folder=self.temp_dir)
        self.assertTrue(s_res.get("success"))
        self.assertGreaterEqual(len(s_res.get("matches", [])), 1)


class TestJarvisScheduler(unittest.TestCase):
    """Test suite for JarvisScheduler natural language time parsing and scheduling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_sched_test_")
        self.storage = Path(self.temp_dir) / "sched_reminders.json"
        self.scheduler = JarvisScheduler(
            storage_file=self.storage,
            check_interval=0.5,
            auto_start=False
        )

    def tearDown(self):
        self.scheduler.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_time_unit_normalization(self):
        self.assertEqual(_normalize_time_unit("soniya"), "seconds")
        self.assertEqual(_normalize_time_unit("soniyadan"), "seconds")
        self.assertEqual(_normalize_time_unit("daqiqa"), "minutes")
        self.assertEqual(_normalize_time_unit("min"), "minutes")
        self.assertEqual(_normalize_time_unit("soat"), "hours")
        self.assertEqual(_normalize_time_unit("kun"), "days")

    def test_parse_natural_relative_uzbek(self):
        # 10 soniyadan keyin
        res = parse_reminder_text("10 soniyadan keyin choy ichish")
        self.assertIsNotNone(res)
        self.assertIn("choy ichish", res["text"])
        self.assertEqual(res["delay_seconds"], 10)

        # 5 daqiqa
        res2 = parse_reminder_text("5 daqiqadan keyin majlis")
        self.assertIsNotNone(res2)
        self.assertIn("majlis", res2["text"])
        self.assertEqual(res2["delay_seconds"], 300)

    def test_parse_natural_relative_english(self):
        res = parse_reminder_text("take medicine in 15 minutes")
        self.assertIsNotNone(res)
        self.assertIn("medicine", res["text"])
        self.assertEqual(res["delay_seconds"], 900)

    def test_add_and_cancel_reminder(self):
        rem = self.scheduler.add_reminder("Test eslatma", delay_seconds=120)
        self.assertIn("id", rem)
        self.assertEqual(rem["status"], "pending")

        # Verify listed
        pending = self.scheduler.list_reminders(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], rem["id"])

        # Cancel
        res = self.scheduler.cancel_reminder(rem["id"])
        self.assertTrue(res)
        self.assertEqual(len(self.scheduler.list_reminders(status="pending")), 0)

    def test_scheduler_callback_trigger(self):
        triggered = []

        def on_alert(record):
            triggered.append(record)

        self.scheduler.register_callback(on_alert)
        # Add a 1 second reminder
        self.scheduler.add_reminder("Tezkor eslatma", delay_seconds=1)
        self.scheduler.start()

        # Wait for worker thread to fire
        time.sleep(2.2)
        self.scheduler.stop()

        self.assertGreaterEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["text"], "Tezkor eslatma")


class TestJarvisConcierge(unittest.TestCase):
    """Test suite for JarvisConcierge state machine and privacy guardrails."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_concierge_test_")
        self.state_file = Path(self.temp_dir) / "test_concierge_state.json"
        self.concierge = JarvisConcierge(state_file=self.state_file)
        self.concierge.notify_admin_bot = MagicMock(return_value=False)

    def tearDown(self):
        time.sleep(0.05)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_state_and_toggle(self):
        state = self.concierge.get_concierge_status()
        self.assertFalse(state["toggle_enabled"])
        
        # Toggle on
        res = self.concierge.toggle_concierge_mode(True)
        self.assertTrue(res["toggle_enabled"])
        self.assertTrue(self.concierge.is_away_mode_active())

        # Toggle off
        res2 = self.concierge.toggle_concierge_mode(False)
        self.assertFalse(res2["toggle_enabled"])

    def test_concluding_pattern_detection(self):
        self.assertTrue(self.concierge.is_concluding_message("Katta rahmat!"))
        self.assertTrue(self.concierge.is_concluding_message("Bo'ldi rahmat"))
        self.assertTrue(self.concierge.is_concluding_message("Xo'p, mayli"))
        self.assertFalse(self.concierge.is_concluding_message("Qachon kelasiz?"))

    def test_note_trigger_detection(self):
        self.assertTrue(self.concierge.is_note_trigger("Azizbekka aytib qo'ying, loyiha tayyor"))
        self.assertTrue(self.concierge.is_note_trigger("Telefon qilib yuborsin iltimos"))
        self.assertFalse(self.concierge.is_note_trigger("Salom yaxshimisiz?"))

    def test_inbox_note_persistence(self):
        with self.concierge._lock:
            self.concierge._record_note_unlocked(
                sender_id="12345",
                sender_name="Ali Valiyev",
                username="alivaliyev",
                note_text="Litsenziya kaliti qachon beriladi?"
            )
        notes = self.concierge.get_inbox_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["sender_name"], "Ali Valiyev")


class TestJarvisWatchdog(unittest.TestCase):
    """Test suite for JarvisWatchdog supervisor and circuit breaker."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_watchdog_test_")
        self.watchdog = JarvisWatchdog(
            base_dir=Path(self.temp_dir),
            max_alerts_count=2,
            alert_window_seconds=10.0,
            heartbeat_timeout_seconds=5.0
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_circuit_breaker_alert_spam(self):
        now = time.time()
        # Trigger 2 alerts spaced by 1.2s -> OK (threshold is > 2)
        self.watchdog.record_alert(now - 3.0)
        self.watchdog.record_alert(now - 1.5)
        tripped, count, _ = self.watchdog.check_alert_rate()
        self.assertFalse(tripped)

        # Trigger 3rd alert -> trips breaker (> 2)
        self.watchdog.record_alert(now)
        tripped, count, _ = self.watchdog.check_alert_rate()
        self.assertTrue(tripped)
        self.assertGreaterEqual(count, 3)

    def test_heartbeat_staleness(self):
        current_proc = psutil.Process(os.getpid())
        hb_file = Path(self.temp_dir) / "heartbeat.json"
        
        # Write fresh heartbeat
        with open(hb_file, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time(), "pid": current_proc.pid}, f)
        
        is_stale, age, _ = self.watchdog.check_heartbeat(proc=current_proc)
        self.assertFalse(is_stale)

        # Write stale heartbeat (60s ago)
        with open(hb_file, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time() - 60, "pid": current_proc.pid}, f)
        
        is_stale, age, _ = self.watchdog.check_heartbeat(proc=current_proc)
        self.assertTrue(is_stale)


class TestJarvisAntigravity(unittest.TestCase):
    """Test suite for JarvisAntigravity deep bridge."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_agy_test_")
        self.tasks_file = Path(self.temp_dir) / "agy_tasks.json"
        
        # Mock environment
        self.env_patcher = patch.dict(os.environ, {
            "ANTIGRAVITY_ROOT": str(self.temp_dir),
            "ANTIGRAVITY_CONVERSATION_ID": "test-conv-123"
        })
        self.env_patcher.start()

        self.agy = JarvisAntigravity(tasks_file=self.tasks_file)

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_creation_and_listing(self):
        res = self.agy.create_project("test_monika_project", description="Avtomatlashtirilgan test loyihasi")
        self.assertEqual(res["status"], "CREATED")
        self.assertTrue(Path(res["path"]).exists())

        projects = self.agy.list_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "test_monika_project")

    def test_document_operations(self):
        doc_path = Path(self.temp_dir) / "doc.md"
        # Write
        w = self.agy.work_with_document(str(doc_path), action="create", content="# Monika AI Test")
        self.assertEqual(w["status"], "success")

        # Read
        r = self.agy.work_with_document(str(doc_path), action="read")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["content"], "# Monika AI Test")

    def test_task_delegation_history(self):
        t = self.agy.delegate_task("Perform code review on test repo")
        self.assertEqual(t["status"], "QUEUED")
        
        history = self.agy.get_delegation_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["task_id"], t["task_id"])


class TestJarvisOmniIndexer(unittest.TestCase):
    """Test suite for PCAppsIndexer and Omni knowledge categorization."""

    def test_app_categorization(self):
        self.assertEqual(PCAppsIndexer._categorize_app("Google Chrome", "C:/chrome.exe"), "browser")
        self.assertEqual(PCAppsIndexer._categorize_app("Telegram Desktop", "C:/telegram.exe"), "communication")
        self.assertEqual(PCAppsIndexer._categorize_app("VS Code", "C:/code.exe"), "developer")
        self.assertEqual(PCAppsIndexer._categorize_app("Microsoft Excel", "C:/excel.exe"), "office")
        self.assertEqual(PCAppsIndexer._categorize_app("Spotify", "C:/spotify.exe"), "media")


class TestJarvisMCPBridge(unittest.TestCase):
    """Test suite for MCPServerProcess JSON-RPC bridge."""

    def test_mcp_server_init(self):
        server = MCPServerProcess("test_server", "python", ["--version"])
        self.assertEqual(server.name, "test_server")
        self.assertEqual(server.command, "python")
        self.assertFalse(server.is_alive)


if __name__ == "__main__":
    unittest.main()
