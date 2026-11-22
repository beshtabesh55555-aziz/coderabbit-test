"""
Unit & Integration Tests for JARVIS Voice, Vision, Telegram & Orchestrator Core (Stage 4)
========================================================================================
Verifies:
1. JarvisVoice: initialization, clean_tts_text, audio playback interface, proper shutdown.
2. JarvisVision: force_simulation safety guardrail, simulation frame generation, snapshot pruning.
3. JarvisTelegram: admin authentication fail-closed, security state machine, alert worker queueing.
4. JarvisUserTelegram: dialog triage classification, unread summary markdown and voice formatting.
5. JarvisCore / WebApp Chat Dispatch: hardware telemetry, screenshot markdown, and screen lock integration.
"""

from datetime import datetime, timezone
import os
import sys
import time
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config
from jarvis_voice import JarvisVoice
from jarvis_vision import JarvisVision
from jarvis_telegram import JarvisTelegram
from jarvis_user_telegram import JarvisUserTelegram, UnreadSummaryResult, User, Channel, Chat
from jarvis_webapp import _process_chat_message


class TestJarvisVoice(unittest.TestCase):
    """Test suite for Jarvis Voice & Audio Module."""

    def setUp(self):
        self.voice = JarvisVoice()

    def tearDown(self):
        self.voice.stop()

    def test_01_voice_initialization(self):
        """Verify voice module initialises with expected default settings."""
        self.assertIsNotNone(self.voice)
        self.assertTrue(hasattr(self.voice, "language"))
        self.assertTrue(hasattr(self.voice, "voice_name"))
        self.assertIn("uz", self.voice.language.lower())

    def test_02_clean_tts_text(self):
        """Verify text cleaning removes markdown and emojis before speech."""
        raw_text = "**Salom!** Tizim: 100% tayyor. 🚀 [Havola](https://example.com) `test_code`"
        clean = self.voice._clean_tts_text(raw_text)
        self.assertNotIn("**", clean)
        self.assertNotIn("`", clean)
        self.assertIn("Salom", clean)

    @patch("subprocess.Popen")
    def test_03_play_audio_file(self, mock_popen):
        """Verify audio playback invokes the powershell player script safely."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(b"fake audio data")
            tf_path = tf.name

        try:
            self.voice._play_audio_file(tf_path)
            self.assertTrue(mock_popen.called)
        finally:
            if os.path.exists(tf_path):
                os.unlink(tf_path)


class TestJarvisVision(unittest.TestCase):
    """Test suite for Jarvis Vision & Surveillance Module."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_vision_test_")
        self.vision = JarvisVision(
            force_simulation=True,
            snapshots_dir=Path(self.temp_dir)
        )

    def tearDown(self):
        self.vision.release()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_04_vision_simulation_mode(self):
        """Verify simulation mode produces synthetic frames without hardware camera access."""
        self.assertTrue(self.vision.is_simulation_mode)
        success, frame = self.vision.read_frame()
        self.assertTrue(success)
        self.assertIsNotNone(frame)
        self.assertEqual(len(frame.shape), 3)

    def test_05_snapshot_pruning(self):
        """Verify automatic retention pruning limits snapshot accumulation."""
        snapshots_path = Path(self.temp_dir)
        for i in range(160):
            p = snapshots_path / f"snap_{i:03d}.jpg"
            p.write_bytes(b"fake image data")

        pruned_count = self.vision._prune_snapshots(max_retained=150)
        remaining = list(snapshots_path.glob("*.jpg"))
        self.assertLessEqual(len(remaining), 150)
        self.assertGreaterEqual(pruned_count, 10)


class TestJarvisTelegram(unittest.TestCase):
    """Test suite for Jarvis Telegram Remote Gateway."""

    def setUp(self):
        self.bot = JarvisTelegram(
            token="dummy_token_123456789",
            admin_chat_id="999999"
        )

    def tearDown(self):
        self.bot.stop()

    def test_06_fail_closed_authentication(self):
        """Verify unauthorized users are rejected immediately (fail-closed)."""
        self.assertTrue(self.bot.is_admin("999999"))
        self.assertTrue(self.bot.is_admin(999999))
        self.assertFalse(self.bot.is_admin("111111"))
        self.assertFalse(self.bot.is_admin(None))
        self.assertFalse(self.bot.is_admin(""))

    def test_07_security_state_machine(self):
        """Verify security state arming and disarming transitions."""
        self.assertTrue(self.bot.is_armed())
        self.assertEqual(self.bot.get_security_mode(), "ARMED")

        # Disarm
        self.bot.security_state = "DISARMED"
        self.assertFalse(self.bot.is_armed())
        self.assertEqual(self.bot.get_security_mode(), "DISARMED")

        # Re-arm
        self.bot.security_state = "ARMED"
        self.assertTrue(self.bot.is_armed())


class TestJarvisUserTelegram(unittest.TestCase):
    """Test suite for Personal Telegram (Telethon) Bridge."""

    def test_08_dialog_triage_classification(self):
        """Verify dialog categorization into personal, group, and channel tiers."""
        mock_entity = MagicMock(spec=User)
        mock_entity.first_name = "Jamshid"
        mock_entity.last_name = "Aliyev"
        mock_entity.username = "jamshid_dev"
        mock_entity.phone = "+998901234567"
        mock_entity.bot = False

        now_utc = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
        mock_dialog = MagicMock()
        mock_dialog.name = "Jamshid Aliyev"
        mock_dialog.entity = mock_entity
        mock_dialog.unread_count = 2
        mock_dialog.date = now_utc
        mock_msg = MagicMock(text="Salom Azizbek!")
        mock_msg.date = now_utc
        mock_dialog.message = mock_msg

        key_contacts = [{"name": "Jamshid", "username": "jamshid_dev"}]
        cat, info = JarvisUserTelegram._classify_dialog(mock_dialog, key_contacts)
        self.assertEqual(cat, "personal")
        self.assertEqual(info["name"], "Jamshid Aliyev")

    def test_09_unread_summary_formatting(self):
        """Verify UnreadSummaryResult generates clean voice and markdown texts."""
        summary = UnreadSummaryResult(
            markdown="**🔴 Shaxsiy:** Jamshid (2 ta xabar)",
            voice_summary="Jamshiddan 2 ta yangi xabar bor.",
            total_unread=2
        )
        md = summary.get_markdown()
        voice = summary.get_voice()
        self.assertIn("Jamshid", md)
        self.assertIn("Jamshid", voice)


class TestJarvisCoreOrchestration(unittest.TestCase):
    """Test suite for Central Core Orchestration & WebApp Chat Dispatch."""

    def test_10_chat_dispatch_telemetry(self):
        """Verify 'tizim holati' yields live hardware telemetry."""
        reply = _process_chat_message("tizim holati", user_id="123456789")
        self.assertIn("CPU", reply)
        self.assertIn("RAM", reply)
        self.assertNotIn("Buyruq qabul qilindi", reply)

    def test_11_chat_dispatch_screenshot_markdown(self):
        """Verify screenshot command outputs valid inline image markdown."""
        with patch("jarvis_desktop.JarvisDesktop.take_screenshot") as mock_sc:
            mock_sc.return_value = {
                "status": "success",
                "path": str(BASE_DIR / "snapshots" / "mock.png"),
                "width": 1920,
                "height": 1080
            }
            reply = _process_chat_message("ekranni rasmga ol", user_id="123456789")
            self.assertIn("![", reply)
            self.assertIn("/api/screenshot", reply)

    def test_12_chat_dispatch_screen_lock(self):
        """Verify screen lock command dispatches lock workstation safely."""
        with patch("jarvis_system.JarvisSystem.lock_workstation") as mock_lock:
            mock_lock.return_value = True
            reply = _process_chat_message("ekranni qulfla", user_id="123456789")
            self.assertTrue(mock_lock.called)
            self.assertIn("qulf", reply.lower())


if __name__ == "__main__":
    unittest.main()
