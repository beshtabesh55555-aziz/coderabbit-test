"""
JARVIS Central Core Orchestrator (Monika)
=========================================
Integrates Gemini Function Calling Brain, Live Sensory Perception,
Background Scheduler, Tool Registry, Vision, Voice, System Admin,
Telegram (Telethon & Bot), Antigravity Deep Bridge, Desktop Controller,
and Personal Memory into a unified 100% autonomous executive assistant.

Hardening Features:
- Autonomous Gemini Function Calling engine with live sensory & personal context.
- Complete execution loop with safe tool dispatch and thought-signature resilience.
- Local fast-path commands and graceful failover when Gemini is offline or hits quota.
- Watchdog heartbeat writer (heartbeat.json logged every 5 seconds via atomic write).
- Workstation lock state respect: suppresses motion alerts when user is active/unlocked unless Armed.
- Non-blocking asynchronous alert dispatch.
- Silent on Telegram: does NOT output speech through PC speakers when commanded via Telegram.
"""

import os
import sys
import re
import json
import time
import html
import logging
import threading
import asyncio
import psutil
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple

# Import local modules
import config
from jarvis_system import JarvisSystem
from jarvis_safety import SafetyManager
from jarvis_tracer import JarvisTracer
from jarvis_local_llm import JarvisLocalLLM
from jarvis_nlp import get_ambiguity_engine, get_nlp_preprocessor
from jarvis_dialogue import get_dialogue_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("JARVIS-CORE")


class JarvisCore:
    def __init__(self):
        logger.info("Initializing JARVIS Core System (Monika)...")
        self.system = JarvisSystem()
        self.safety = SafetyManager()
        self.tracer = JarvisTracer()
        self.local_llm = JarvisLocalLLM()
        self.vision = None
        self.voice = None
        self.telegram = None
        self.antigravity = None
        self.desktop = None
        self.sensor = None
        self.memory = None
        self.scheduler = None
        self.tool_registry = None
        self.gemini_model = None
        self.gemini_summary_model = None
        self.nlp_preprocessor = get_nlp_preprocessor()
        self.ambiguity_engine = get_ambiguity_engine()
        self.dialogue_manager = get_dialogue_manager()
        self._clarification_dispatched_via_tg = False
        self.is_running = False
        self._stop_event = threading.Event()
        self.concierge = None
        self._command_lock = threading.RLock()

        # Base path for runtime artifacts
        self.base_dir = getattr(config, "BASE_DIR", Path(__file__).resolve().parent)
        self.heartbeat_file = self.base_dir / "heartbeat.json"

        # Contextual artifact tracking for anaphora resolution ('uni ... jo'nat')
        self.last_screenshot_path: Optional[str] = None
        self.last_file_path: Optional[str] = None
        self.omni_knowledge = None

        # Enforce single instance execution across the OS (Prevents double screenshot / polling races)
        self._acquire_single_instance_lock()

        # Start watchdog heartbeat monitor thread immediately so supervisor sees live state
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="JARVIS-Core-Heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        # Lazy load modules
        self._init_desktop()
        self._init_sensor()
        self._init_memory()
        self._init_vision()
        self._init_voice()
        self._init_antigravity()
        self._init_telegram()
        self._init_scheduler()
        self._init_tools()
        self._init_gemini()
        self._init_omni_knowledge()
        self._init_concierge()

    def _init_concierge(self):
        """Initialize Monika AI Concierge / Auto-Responder (Kotiba Rejimi)."""
        try:
            from jarvis_concierge import JarvisConcierge
            self.concierge = JarvisConcierge.get_instance()
            logger.info("🛡️ Monika AI Concierge (Kotiba Rejimi) engine initialized.")
        except Exception as e:
            logger.warning(f"Concierge engine could not be initialized: {e}")

    def _init_desktop(self):
        try:
            from jarvis_desktop import JarvisDesktop
            self.desktop = JarvisDesktop(failsafe=True)
            logger.info("Desktop Controller (GUI / Mouse / Keyboard) initialized successfully.")
        except Exception as e:
            logger.warning(f"Desktop Controller could not be loaded: {e}")

    def _init_sensor(self):
        try:
            from jarvis_pc_sensor import JarvisPCSensor
            self.sensor = JarvisPCSensor(desktop_controller=getattr(self, "desktop", None))
            logger.info("PC Sensory Engine (Active Window, Volume, Clipboard, Lock Screen) initialized successfully.")
        except Exception as e:
            logger.warning(f"PC Sensory Engine could not be loaded: {e}")

    def _init_memory(self):
        try:
            from jarvis_memory import JarvisMemory
            self.memory = JarvisMemory()
            logger.info("Personal Memory & Knowledge Base initialized successfully.")
        except Exception as e:
            logger.warning(f"Personal Memory could not be loaded: {e}")

    def _init_vision(self):
        try:
            from jarvis_vision import JarvisVision
            self.vision = JarvisVision(
                camera_index=config.CAMERA_INDEX,
                sensitivity=config.MOTION_SENSITIVITY,
                min_area=config.MOTION_MIN_AREA,
                cooldown=config.MOTION_COOLDOWN,
            )
            self.vision.set_motion_callback(self.on_motion_detected)
            logger.info("Vision Engine initialized successfully.")
        except Exception as e:
            logger.warning(f"Vision Engine could not be loaded: {e}")

    def _init_voice(self):
        try:
            from jarvis_voice import JarvisVoice
            self.voice = JarvisVoice(
                language=config.VOICE_LANGUAGE,
                voice_name=config.TTS_VOICE,
            )
            logger.info("Voice Engine initialized successfully.")
        except Exception as e:
            logger.warning(f"Voice Engine could not be loaded: {e}")

    def _init_antigravity(self):
        try:
            from jarvis_antigravity import JarvisAntigravity
            self.antigravity = JarvisAntigravity()
            logger.info("Antigravity Bridge initialized successfully.")
        except Exception as e:
            logger.warning(f"Antigravity Bridge could not be loaded: {e}")

    def _init_telegram(self):
        try:
            from jarvis_telegram import JarvisTelegram
            if config.is_telegram_configured():
                self.telegram = JarvisTelegram(
                    token=config.TELEGRAM_BOT_TOKEN,
                    admin_chat_id=config.TELEGRAM_ADMIN_CHAT_ID,
                    core_ref=self,
                )
                logger.info("Telegram Gateway initialized successfully.")
            else:
                logger.info("Telegram bot not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID in .env")
        except Exception as e:
            logger.warning(f"Telegram Gateway could not be loaded: {e}")

    def _init_scheduler(self):
        """Initialize background reminder and alert scheduler."""
        try:
            from jarvis_scheduler import JarvisScheduler
            self.scheduler = JarvisScheduler(
                storage_file=self.base_dir / "reminders.json",
                check_interval=2.0,
                auto_start=True
            )

            # Register callback to send due reminder alerts to Telegram admin
            def _on_reminder_due(rem: Dict[str, Any]):
                rem_text = rem.get("text", "Eslatma")
                trigger_at = rem.get("trigger_at", time.strftime("%H:%M:%S"))
                alert_msg = (
                    f"⏰ <b>JARVIS ESLATMASI (Monika)</b>\n\n"
                    f"📌 <b>Vazifa:</b> {html.escape(rem_text)}\n"
                    f"⏱ <b>Vaqt:</b> {trigger_at}"
                )
                if self.telegram and hasattr(self.telegram, "send_message"):
                    self.telegram.send_message(alert_msg)
                logger.info(f"Scheduled alert dispatched to Telegram: {rem_text}")

            self.scheduler.register_callback(_on_reminder_due)
            logger.info("Scheduler Engine (Background Reminders & Alerts) initialized successfully.")
        except Exception as e:
            logger.warning(f"Scheduler Engine could not be loaded: {e}")

    def _init_tools(self):
        """Initialize central tool registry for Gemini Function Calling and direct dispatch."""
        try:
            from jarvis_tool_registry import JarvisToolRegistry
            from jarvis_user_telegram import JarvisUserTelegram
            self.tool_registry = JarvisToolRegistry(
                sensor=getattr(self, "sensor", None),
                desktop=getattr(self, "desktop", None),
                system=getattr(self, "system", None),
                safety=getattr(self, "safety", None),
                scheduler=getattr(self, "scheduler", None),
                user_telegram=JarvisUserTelegram,
                memory=getattr(self, "memory", None),
                base_dir=self.base_dir,
            )
            tools_count = len(self.tool_registry.get_gemini_tools())
            logger.info(f"Tool Registry initialized successfully with {tools_count} functions.")
        except Exception as e:
            logger.warning(f"Tool Registry could not be loaded: {e}")

    def _init_gemini(self):
        """Initialize Gemini model with Function Calling tools from JarvisToolRegistry."""
        self.gemini_model = None
        self.gemini_summary_model = None
        self.gemini_candidate_models = getattr(
            config,
            "GEMINI_FALLBACK_MODELS",
            [
                "models/gemini-3.5-flash-lite",
                "models/gemini-3.1-flash-lite-preview",
                "models/gemini-3.1-flash-lite",
                "models/gemini-3-flash-preview",
                "models/gemini-3.5-flash",
                "models/gemini-flash-lite-latest",
            ]
        )
        if config.is_gemini_configured():
            try:
                import google.generativeai as genai
                genai.configure(api_key=config.GEMINI_API_KEY, transport="rest")
                model_name = getattr(config, "GEMINI_MODEL", "models/gemini-3.5-flash-lite")
                tools = self.tool_registry.get_gemini_tools() if self.tool_registry else []
                
                self.gemini_model = genai.GenerativeModel(model_name, tools=tools)
                self.gemini_summary_model = genai.GenerativeModel(model_name)
                logger.info(f"Gemini Brain ({model_name}) initialized with {len(tools)} Function Calling tools.")
            except Exception as e:
                logger.warning(f"Could not initialize Gemini API: {e}")
        else:
            logger.info("Gemini API key not set. Set GEMINI_API_KEY in .env for full multimodal intelligence.")

    def _init_omni_knowledge(self):
        """Initialize Omni-Knowledge Engine (Desktop Apps, Files, Telegram Personal Chats, Saved Messages)."""
        try:
            from jarvis_omni_indexer import get_omni_knowledge
            self.omni_knowledge = get_omni_knowledge()
            # Non-blocking background sync on boot
            self.omni_knowledge.sync_all(force=False, async_bg=True)
            logger.info("Omni-Knowledge Engine (Desktop Apps, Files, Telegram Chats & Saved Messages) initialized successfully.")
        except Exception as e:
            logger.warning(f"Could not initialize Omni-Knowledge Engine: {e}")

    # =========================================================================
    # Single Instance Process Guard & Context Resolution
    # =========================================================================

    def _acquire_single_instance_lock(self):
        """Enforce strict single instance execution using an OS-level file lock without terminating other processes."""
        lock_file_path = self.base_dir / ".jarvis_core.lock"
        pid_file = self.base_dir / ".jarvis_core.pid"
        my_pid = os.getpid()

        import msvcrt
        try:
            self._lock_file = open(lock_file_path, "a+b")
        except (IOError, OSError) as open_err:
            logger.error(f"Failed to open instance lock file '{lock_file_path}': {open_err}")
            raise

        try:
            msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except (IOError, OSError) as e:
            import errno
            if getattr(e, "errno", None) in (errno.EACCES, errno.EDEADLK) or getattr(e, "winerror", None) == 33 or "lock" in str(e).lower():
                logger.warning(
                    f"Another instance of JARVIS Core is already running and holds the OS lock ({e}). "
                    "Exiting gracefully to prevent duplicate instance conflicts."
                )
                try:
                    self._lock_file.close()
                except Exception:
                    pass
                sys.exit(0)
            else:
                logger.error(f"Unexpected OS error during lock acquisition: {e}")
                try:
                    self._lock_file.close()
                except Exception:
                    pass
                raise

        try:
            pid_file.write_text(str(my_pid), encoding="utf-8")
            import atexit
            def _cleanup_lock():
                try:
                    if pid_file.exists():
                        pid_file.unlink(missing_ok=True)
                finally:
                    if hasattr(self, "_lock_file") and self._lock_file:
                        try:
                            msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                        try:
                            self._lock_file.close()
                        except Exception:
                            pass
            atexit.register(_cleanup_lock)
            logger.info(f"Single instance OS lock acquired (PID: {my_pid}).")
        except Exception as e:
            logger.error(f"Failed to write PID lock file: {e}")

    def get_last_active_file(self, user_id: Optional[str] = None) -> Optional[str]:
        """Retrieve the most recently generated or received file (screenshot, wallpaper, media)."""
        if self.last_screenshot_path and os.path.exists(self.last_screenshot_path):
            return self.last_screenshot_path
        if self.last_file_path and os.path.exists(self.last_file_path):
            return self.last_file_path
        if self.dialogue_manager and user_id:
            ctx = self.dialogue_manager.get_context(str(user_id))
            if ctx:
                ctx_file = ctx.get("last_file") or ctx.get("last_screenshot")
                if ctx_file and os.path.exists(ctx_file):
                    return ctx_file
        # Fallback to the latest file in snapshots/
        snap_dir = self.base_dir / "snapshots"
        if snap_dir.exists():
            snaps = sorted(snap_dir.glob("*.png"), key=os.path.getmtime, reverse=True)
            if snaps:
                return str(snaps[0])
        return None

    # =========================================================================
    # Watchdog Heartbeat Writer
    # =========================================================================

    def _heartbeat_loop(self):
        """Continuously write heartbeat.json every 5 seconds using atomic file replace until stopped."""
        logger.info(f"Watchdog heartbeat monitor started ({self.heartbeat_file}).")
        while not self._stop_event.is_set():
            try:
                sec_status = self.system.get_security_status() if self.system else "Unknown"
                sec_mode = self.telegram.get_security_mode() if self.telegram else "UNKNOWN"
                is_armed = self.telegram.is_armed() if self.telegram else False

                heartbeat_data = {
                    "timestamp": time.time(),
                    "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "healthy",
                    "pid": os.getpid(),
                    "is_running": self.is_running,
                    "vision_monitoring": bool(self.vision and self.vision.is_monitoring),
                    "telegram_polling": bool(self.telegram and self.telegram.is_running),
                    "scheduler_running": bool(self.scheduler and getattr(self.scheduler, "_is_running", False)),
                    "tool_registry_ready": bool(self.tool_registry is not None),
                    "workstation_security": sec_status,
                    "security_mode": sec_mode,
                    "is_armed": is_armed,
                    "concierge_enabled": bool(self.concierge and self.concierge.is_away_mode_active()),
                }
                tmp_file = self.heartbeat_file.with_suffix(".tmp")
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(heartbeat_data, f, indent=2)
                tmp_file.replace(self.heartbeat_file)
            except Exception as e:
                logger.error(f"Error updating heartbeat: {e}")
            self._stop_event.wait(5.0)

    def speak(self, text: str):
        """Speak out text via Voice Engine."""
        if self.voice:
            self.voice.speak(text)
        else:
            logger.info(f"JARVIS: {text}")

    def on_motion_detected(self, *args, **kwargs):
        """Security handler for motion detection events."""
        snapshot_path = None
        for a in args:
            if isinstance(a, (str, Path)) and (str(a).endswith((".jpg", ".png", ".jpeg")) or "snapshot" in str(a).lower()):
                snapshot_path = str(a)
                break
        if not snapshot_path and len(args) >= 2 and isinstance(args[1], str):
            snapshot_path = args[1]
        if not snapshot_path:
            snapshot_path = kwargs.get("snapshot_path")
        logger.info(f"[SECURITY EVENT] Motion detected! Snapshot: {snapshot_path}")

        lock_status = self.system.get_lock_status() if self.system else {}
        is_confirmed_locked = bool(lock_status.get("is_locked") is True and not lock_status.get("error"))
        is_armed = self.telegram.is_armed() if self.telegram else False
        sec_status = "LOCKED" if is_confirmed_locked else "UNLOCKED"

        # STRICT SUPPRESSION: Motion alerts must ONLY be sent when lock probe confirms screen is LOCKED and Telegram is armed
        if not (is_confirmed_locked and is_armed):
            logger.info(
                f"[SECURITY INFO] Motion detected but workstation is not confirmed locked (locked={lock_status.get('is_locked')}, error={lock_status.get('error')}, armed={is_armed}). "
                "Foydalanuvchi kompyuter qarshisida yoki holat noaniq. Motion alert to'liq bekor qilindi."
            )
            try:
                if snapshot_path and os.path.exists(snapshot_path):
                    os.remove(snapshot_path)
            except Exception:
                pass
            return

        def _dispatch_alert():
            if self.telegram:
                status_note = " (Tizim Qulflangan)"
                caption = (
                    f"🚨 <b>DIQQAT: Harakat aniqlandi!</b>\n"
                    f"⏰ Vaqt: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"🔒 Holat: {sec_status}{status_note}"
                )
                self.telegram.send_alert_photo(snapshot_path, caption=caption)

        threading.Thread(target=_dispatch_alert, name="JARVIS-AlertDispatch", daemon=True).start()

    @staticmethod
    def _parse_contact_message(query: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract recipient and message from commands like '@Jamsh1d_Sodiqov ga salom deb yoz'."""
        raw_q = (query or "").strip()
        q_norm = raw_q.lower()
        q_norm = q_norm.replace("ó", "o'").replace("o‘", "o'").replace("o’", "o'").replace("o`", "o'")
        q_norm = q_norm.replace("gʻ", "g'").replace("g’", "g'").replace("g‘", "g'").replace("g`", "g'")

        q_clean = re.sub(r'^(?:monika|monica|jarvis|hey\s+monika|salom\s+monika|iltimos|yana\s+bir\s+marta)\b\s*', '', raw_q, flags=re.IGNORECASE)
        deb_pattern = r'["\']?(.+?)["\']?\s+deb\s+(?:yoz|yubor|jo[\'’`]?nat|jonat|ayt|yo\b)'

        # 1. Check if @username exists
        at_match = re.search(r'(@[a-zA-Z0-9_]{3,})', q_clean)
        if at_match:
            target = at_match.group(1)
            after_target = q_clean[at_match.end():]
            after_target = re.sub(r"^\s*[\)\],]?\s*(?:nikiga\s+ega\s+)?(?:tanishim|do[\'’`]?stim|hamkasbim|birodarim|og[\'’`]?aynim)?(?:\s*\b(?:ga|ka|qa)\b)?\s*", "", after_target, flags=re.IGNORECASE)
            
            # Require explicit send instruction: "deb yoz/yubor" OR "xabar yoz/yubor: msg"
            m_msg = re.search(deb_pattern, after_target, re.IGNORECASE)
            if m_msg:
                msg = m_msg.group(1).strip()
            else:
                m_msg2 = re.search(r'(?:(?:xabar\s+)?(?:yoz|yubor|jo[\'’`]?nat|jonat)[\s:]+)(.+)', after_target, re.IGNORECASE)
                if m_msg2:
                    msg = m_msg2.group(1).strip()
                else:
                    return None, None
            
            # Normalize message to verify it's not a leftover verb or artifact
            msg_norm = msg.lower().replace("ó", "o'").replace("o‘", "o'").replace("o’", "o'").replace("o`", "o'").strip()
            if msg_norm in ["yoz", "yubor", "jo'nat", "jonat", "tashla", "deb", ""]:
                return None, None
            return target, msg

        # 2. Check for name before ga/ka/qa + deb yoz
        m_all = re.findall(r"(\b[a-zA-Z0-9_\'’`]+?)(?:ga|ka|qa)\b\s+[\"']?(.+?)[\"']?\s+deb\s+(?:yoz|yubor|jo[\'’`]?nat|jonat|ayt|yo\b)", q_clean, re.IGNORECASE)
        if m_all:
            reserved = {"telegram", "telegramdan", "telegramda", "monika", "monica", "yana", "bot"}
            for t, msg in reversed(m_all):
                if t.lower() not in reserved:
                    msg_norm = msg.lower().replace("ó", "o'").replace("o‘", "o'").replace("o’", "o'").strip()
                    if msg_norm not in ["yoz", "yubor", "jo'nat", "jonat", "tashla", "deb", ""]:
                        return t.strip(), msg.strip()

        # 3. Check for name + xabar yoz: msg (Require explicit 'xabar yoz' or 'yoz:' / 'yubor:')
        m2 = re.search(r"(\b[a-zA-Z0-9_\'’`]+?)(?:ga|ka|qa)?\s+(?:(?:xabar\s+)?\b(?:yoz|yubor|jo[\'’`]?nat|jonat)\b[\s:]+)[\s:]*(.+)", q_clean, re.IGNORECASE)
        if m2:
            target = m2.group(1).strip()
            msg = m2.group(2).strip()
            msg_norm = msg.lower().replace("ó", "o'").replace("o‘", "o'").replace("o’", "o'").strip()
            if target.lower() not in ["telegram", "bot", "monika", "fayl"] and msg_norm not in ["yoz", "yubor", "jo'nat", "jonat", "tashla", "deb", ""]:
                return target, msg

        return None, None

    def _get_current_wallpaper_path(self) -> Optional[str]:
        """Extract and cache the active Windows Desktop Wallpaper image."""
        try:
            appdata = os.environ.get("APPDATA", "")
            tw = Path(appdata) / "Microsoft" / "Windows" / "Themes" / "TranscodedWallpaper"
            if tw.exists() and tw.stat().st_size > 0:
                snap_dir = self.base_dir / "snapshots"
                snap_dir.mkdir(parents=True, exist_ok=True)
                wp_jpg = snap_dir / "current_wallpaper.jpg"
                wp_jpg.write_bytes(tw.read_bytes())
                return str(wp_jpg)
        except Exception as e:
            logger.error(f"Error extracting Windows wallpaper: {e}")
        return None

    def handle_clarification_response(self, user_id: str, choice_or_callback: str) -> str:
        """Handle clarification choice from Telegram button callback or external channel."""
        uid = str(user_id)
        if not hasattr(self, "dialogue_manager") or not self.dialogue_manager:
            return "Kechirasiz, muloqot holati moduli faol emas."

        resolved = self.dialogue_manager.resolve_clarification(uid, choice_or_callback)
        if not resolved:
            return "Kechirasiz, faol aniqlashtirish so'rovi topilmadi yoki muddati o'tgan."
        if resolved.get("status") == "cancelled":
            return "Tushunarli, so'rov bekor qilindi."
        return self._execute_resolved_clarification(resolved, speak_output=False, user_id=uid)

    def _execute_resolved_clarification(self, resolved: Dict[str, Any], speak_output: bool = True, user_id: Optional[str] = None) -> str:
        """Execute the resolved action after user disambiguation."""
        action = resolved.get("action", "")
        intent = resolved.get("intent", "")
        params = resolved.get("params") or resolved.get("kwargs") or {}
        kwargs = params
        metric = params.get("metric") or resolved.get("metric") or ""
        target_uid = user_id or resolved.get("user_id")

        # 1. Storage Metric Resolution (RAM vs Disk vs Both)
        if intent == "INQUIRE_STORAGE" or action == "get_system_status":
            stats = self.system.get_system_stats() if self.system else {}
            total_ram = stats.get("ram_total_gb", 0)
            used_ram = stats.get("ram_used_gb", 0)
            ram_pct = stats.get("ram_percent", 0)
            free_ram = round(max(0.0, total_ram - used_ram), 2)
            free_ram_pct = round(100.0 - ram_pct, 1)

            total_disk = stats.get("disk_total_gb", 0)
            free_disk = stats.get("disk_free_gb", 0)
            disk_pct = stats.get("disk_percent", 0)

            if metric == "ram":
                return f"Hozirda noutbukda {free_ram} GB ({free_ram_pct}%) tezkor xotira (RAM) bo'sh, {used_ram} GB ({ram_pct}%) band."
            elif metric == "disk":
                return f"Hozirda asosiy diskda (C:) {free_disk} GB bo'sh joy mavjud (jami {total_disk} GB dan, {disk_pct}% band)."
            else:  # both
                return (
                    f"Noutbuk xotira holati:\n"
                    f"• ⚡ <b>Tezkor xotira (RAM):</b> {free_ram} GB bo'sh (jami {total_ram} GB dan, {ram_pct}% band)\n"
                    f"• 💾 <b>Asosiy disk (C:):</b> {free_disk} GB bo'sh (jami {total_disk} GB dan, {disk_pct}% band)"
                )

        # 2. Wallpaper Resolution
        elif intent == "GET_WALLPAPER" or action == "send_wallpaper_image":
            recipient = kwargs.get("recipient") or resolved.get("params", {}).get("recipient") or resolved.get("recipient")
            wp_path = self._get_current_wallpaper_path()
            if not wp_path or not os.path.exists(wp_path):
                return "Kechirasiz, tizimning faol fon rasmini (Wallpaper) olishning imkoni bo'lmadi."

            if recipient:
                # Send via user telegram to the contact (e.g. @XuRsHiDuLLoH)
                try:
                    from jarvis_user_telegram import JarvisUserTelegram
                    res = JarvisUserTelegram.send_telegram_file(recipient, wp_path, caption="🖥️ Ish stoli fon rasmi (Desktop Wallpaper)")
                    if res.get("success"):
                        return f"Ish stoli fon rasmi (Wallpaper) muvaffaqiyatli olindi va {recipient} ga Telegram orqali yuborildi."
                    else:
                        return f"Fon rasmi olindi, ammo {recipient} ga yuborishda xatolik: {res.get('error', 'Nomaʼlum')}"
                except Exception as e:
                    return f"Telegram orqali fayl yuborishda xatolik: {e}"
            else:
                # Send directly to the admin chat in bot
                if self.telegram:
                    self.telegram.send_photo(wp_path, caption="🖥️ Ish stoli fon rasmi (Desktop Wallpaper)")
                    self._media_dispatched_via_tg = True
                    return "Ish stoli fon rasmi (Wallpaper) muvaffaqiyatli olindi va Telegramingizga yuborildi."
                return f"Fon rasmi olindi: {wp_path}"

        # 3. Desktop Files Resolution
        elif intent == "GET_DESKTOP_FILE" or action == "list_desktop_files":
            desktop_path = Path.home() / "Desktop"
            files = [f.name for f in desktop_path.glob("*") if f.is_file()][:10]
            if files:
                f_list = "\n".join([f"• <code>{f}</code>" for f in files])
                return f"Ish stoli (Desktop) papkasidagi fayllar:\n{f_list}\n\nQaysi birini jo'nataylik?"
            return "Ish stoli (Desktop) papkasida hozircha fayllar topilmadi."

        # 4. Anaphoric / Last File Dispatch Resolution
        elif intent == "FORWARD_LAST_FILE" or action == "send_last_file":
            recipient = kwargs.get("recipient") or resolved.get("recipient")
            target_file = self.get_last_active_file(target_uid)
            if not target_file or not os.path.exists(target_file):
                return "Kechirasiz, yuborish uchun oldingi fayl yoki skrinshot topilmadi."
            if not recipient:
                return "Qabul qiluvchi shaxs ko'rsatilmadi."
            try:
                from jarvis_user_telegram import JarvisUserTelegram
                caption = f"📄 {Path(target_file).name}"
                res = JarvisUserTelegram.send_telegram_file(recipient, str(target_file), caption=caption)
                if res.get("success"):
                    return f"Oxirgi fayl ({Path(target_file).name}) muvaffaqiyatli olindi va {res.get('target', recipient)} ga yuborildi."
                else:
                    return f"Faylni {recipient} ga yuborishda xatolik: {res.get('error')}"
            except Exception as e:
                return f"Faylni yuborishda xatolik yuz berdi: {e}"

        # 5. Named File Resolution
        elif intent == "SEND_FILE" or action == "send_file":
            recipient = kwargs.get("recipient") or resolved.get("recipient")
            filename = kwargs.get("filename") or kwargs.get("file_path") or resolved.get("filename")
            if not recipient:
                return "Qabul qiluvchi shaxs ko'rsatilmadi."
            if not filename:
                return "Yuboriladigan fayl nomi ko'rsatilmadi."

            target_file = None
            search_dirs = [
                self.base_dir,
                Path.home() / "Documents",
                Path.home() / "Downloads",
                Path.home() / "Desktop",
            ]
            for sdir in search_dirs:
                cand = sdir / filename
                if cand.exists() and cand.is_file():
                    target_file = cand
                    break

            if not target_file:
                return f"❌ <b>{filename}</b> fayli kompyuterda topilmadi."

            try:
                from jarvis_user_telegram import JarvisUserTelegram
                caption = f"📄 {target_file.name}"
                res = JarvisUserTelegram.send_telegram_file(recipient, str(target_file), caption=caption)
                if res.get("success"):
                    return f"✅ <b>{target_file.name}</b> fayli muvaffaqiyatli olindi va {res.get('target', recipient)} ga yuborildi."
                else:
                    return f"❌ Faylni {recipient} ga yuborishda xatolik: {res.get('error')}"
            except Exception as e:
                return f"❌ Faylni yuborishda xatolik yuz berdi: {e}"

        # Default fallback execution
        res_query = resolved.get("resolved_query") or resolved.get("message") or "Buyruq qabul qilindi"
        return res_query

    def _handle_anaphora_forwarding(self, query: str, user_id: Optional[str] = None) -> Optional[str]:
        """Detect commands forwarding the previously generated/captured item (e.g. 'Endi uni xurshidga jónat')."""
        raw_q = (query or "").strip()
        q = raw_q.lower()
        q = q.replace("ó", "o'").replace("o‘", "o'").replace("o’", "o'").replace("o`", "o'")
        q = q.replace("gʻ", "g'").replace("g’", "g'").replace("g‘", "g'").replace("g`", "g'")

        is_forward = (
            any(w in q for w in ["uni", "buni", "o'shani", "oshani", "oxirgisini", "oxirgi skrinshotni", "oxirgi rasmni"])
            and any(w in q for w in ["jo'nat", "jonat", "yubor", "tashla", "yetkaz", "send", "uzat"])
        )
        if not is_forward:
            return None

        # Extract recipient
        target = None
        at_m = re.search(r'(@[a-zA-Z0-9_]{3,})', raw_q)
        if at_m:
            target = at_m.group(1).strip()
        else:
            m_target = re.search(r'(?:telegramda\s+)?([a-zA-Z0-9_\'`]+?)(?:ga|ka|qa)\s*(?:nikiga\s+ega\s+)?(?:tanishim|do[\'’`]?stim|hamkasbim)?', q, re.IGNORECASE)
            if m_target:
                cand = m_target.group(1).strip()
                if cand.lower() not in ["telegram", "bot", "monika", "fayl", "menga", "bizga", "unga", "bunga"]:
                    target = cand

        if not target:
            return None

        known_aliases = {
            "xurshid": "@XuRsHiDuLLoH",
            "xurshidulloh": "@XuRsHiDuLLoH",
            "jamshid": "@Jamsh1d_Sodiqov"
        }
        target_resolved = known_aliases.get(target.lower(), target)

        target_file = self.get_last_active_file(user_id)
        if not target_file or not os.path.exists(target_file):
            return "Kechirasiz, yuborish uchun oldingi skrinshot yoki fayl topilmadi. Avval yangi skrinshot oling."

        try:
            from jarvis_user_telegram import JarvisUserTelegram
            file_p = Path(target_file)
            caption = f"🖥️ {file_p.name} (Surat / Skrinshot)"
            res = JarvisUserTelegram.send_telegram_file(target_resolved, str(file_p), caption=caption)
            if res.get("success"):
                return f"🖥️ Oxirgi olingan ekran surati ({file_p.name}) Telegram orqali {res.get('target', target_resolved)} ga muvaffaqiyatli yuborildi."
            else:
                return f"Faylni yuborishda xatolik yuz berdi: {res.get('error')}"
        except Exception as e:
            logger.error(f"Error in _handle_anaphora_forwarding: {e}")
            return f"Telegram orqali yuborishda xatolik: {e}"

    def _handle_telegram_file_send(self, query: str) -> Optional[str]:
        """Detect and execute sending local or chat media (music, photo, video, docs) via Telegram."""
        raw_q = (query or "").strip()
        q = raw_q.lower()
        q = q.replace("ó", "o'").replace("o‘", "o'").replace("o’", "o'").replace("o`", "o'")
        q = q.replace("gʻ", "g'").replace("g’", "g'").replace("g‘", "g'").replace("g`", "g'")

        media_keywords = [
            "fayl", "faylini", "faylni", "hujjat", "kitob", "doc", "docx", "pdf", "zip", "rar",
            "rasm", "surat", "foto", "musiqa", "qo'shiq", "audio", "mp3", "video", "klip", "media"
        ]
        has_media_kw = any(w in q for w in media_keywords)
        has_send_verb = any(w in q for w in ["jo'nat", "jonat", "yubor", "tashla", "yetkaz", "send", "uzat"])
        has_tg_target = any(w in q for w in ["telegram", "telegramda", "telegramdan", "@"]) or bool(re.search(r'@[a-zA-Z0-9_]{3,}', raw_q))

        if not (has_media_kw and has_send_verb and has_tg_target):
            return None

        # 1. Check if this is a Chat-to-Chat Media Transfer (parse source chat first)
        is_chat_transfer = any(w in q for w in ["botdagi", "botdan", "kanaldagi", "kanaldan", "chatdagi", "chatdan", "vkm", "vkmusic", "saved messages", "saqlangan"])
        source_chat = None
        src_span = None
        if is_chat_transfer:
            m_src = re.search(r'(@?[a-zA-Z0-9_]+?)\s*(?:botdagi|botdan|kanaldagi|kanaldan|chatdagi|chatdan)', raw_q, re.IGNORECASE)
            if m_src:
                src_span = m_src.span(1)
                src_tok = m_src.group(1).strip()
                if src_tok.lower() in ["vkm", "vkmusic", "@vkm", "@vkmusic"]:
                    source_chat = "@vkmusic_bot"
                elif src_tok.lower() in ["me", "saved"]:
                    source_chat = "me"
                else:
                    source_chat = src_tok
            elif "saved messages" in q or "saqlangan" in q:
                source_chat = "me"
            elif any(re.search(r'\b' + pat + r'\b', q) for pat in ["vkm", "vkmusic"]):
                source_chat = "@vkmusic_bot"
            if not source_chat:
                source_chat = "@vkmusic_bot" if ("qo'shiq" in q or "musiqa" in q) else "me"

        # 2. Extract destination recipient (distinct from source_chat and outside source token position)
        target = None
        for m_h in re.finditer(r'(@[a-zA-Z0-9_]{3,})(?:(?:\s+)?(?:ga|ka|qa)\b)?', raw_q, re.IGNORECASE):
            if src_span and (m_h.start() < src_span[1] and m_h.end() > src_span[0]):
                continue
            h_cand = m_h.group(1).strip()
            if not source_chat or h_cand.lower() != source_chat.lower():
                target = h_cand
                break

        if not target:
            for m_d in re.finditer(r'\b([a-zA-Z0-9_\'’`]+?)(?:ga|ka|qa)\b', raw_q, re.IGNORECASE):
                if src_span and (m_d.start() < src_span[1] and m_d.end() > src_span[0]):
                    continue
                cand = m_d.group(1).strip()
                if cand.lower() not in ["telegram", "bot", "monika", "fayl"] and (not source_chat or cand.lower() != source_chat.lower()):
                    target = cand
                    break

        if not target:
            all_handles = re.findall(r'(@[a-zA-Z0-9_]{3,})', raw_q)
            for h in all_handles:
                if not source_chat or h.lower() != source_chat.lower():
                    target = h
                    break

        if not target:
            m_target = re.search(r'(?:telegramda\s+)?([a-zA-Z0-9_\'’`]+?)(?:ga|ka|qa)\s+(?:fayl\w*|musiqa\w*|qo[\'’`]?shiq\w*|video\w*|rasm\w*)?\s*(?:jo[\'’`]?nat|jonat|yubor|tashla)', q, re.IGNORECASE)
            if m_target:
                cand = m_target.group(1).strip()
                if cand.lower() not in ["telegram", "bot", "monika", "fayl"] and (not source_chat or cand.lower() != source_chat.lower()):
                    target = cand

        if not target:
            return None

        known_aliases = {
            "xurshid": "@XuRsHiDuLLoH",
            "xurshidulloh": "@XuRsHiDuLLoH",
            "jamshid": "@Jamsh1d_Sodiqov"
        }
        if target.lower() in known_aliases:
            target = known_aliases[target.lower()]

        if is_chat_transfer:
            if any(w in q for w in ["qo'shiq", "musiqa", "audio", "mp3"]):
                m_type = "audio"
                m_label = "🎵 Qo'shiq (Musiqa)"
            elif any(w in q for w in ["video", "klip", "mp4"]):
                m_type = "video"
                m_label = "🎬 Video lavha"
            elif any(w in q for w in ["rasm", "surat", "foto"]):
                m_type = "photo"
                m_label = "🖼️ Rasm"
            else:
                m_type = "document"
                m_label = "📄 Hujjat / Fayl"

            try:
                from jarvis_user_telegram import JarvisUserTelegram
                res = JarvisUserTelegram.transfer_chat_media(source_chat, target, media_type=m_type)
                if res.get("success"):
                    return f"{m_label} '{res.get('source', source_chat)}' dan muvaffaqiyatli topildi va Telegram orqali {res.get('target', target)} ga yuborildi."
                else:
                    return f"Medianing o'tkazilishida xatolik: {res.get('error')}"
            except Exception as e:
                logger.error(f"Error transferring chat media: {e}")
                return f"Medianing yuborilishida xatolik: {e}"

        # 3. Local File System Search & Dispatch
        clean_q = re.sub(r'@[a-zA-Z0-9_]{3,}', '', q)
        clean_q = re.sub(r"\b(?:telegram|telegramda|telegramdan|nikiga\s+ega|tanishimga|do['’`]?stimga|hamkasbimga|va\s+uni|uni|menga|top|va)\b", " ", clean_q, flags=re.IGNORECASE)

        m_file = re.search(r'(?:rabochiy\s+stoldan|desktopdan|ish\s+stolidan|downloads(?:dan)?|yuklamalardan)?\s*(.+?)\s+(?:faylini|faylni|hujjatini|hujjat|kitobini|kitob|musiqasini|musiqa|qo\'shig\'ini|qo\'shiq|videoni|video|rasmini|rasm)', clean_q, re.IGNORECASE)
        file_query_str = m_file.group(1).strip() if m_file else ""
        if not file_query_str:
            m_alt = re.search(r"([a-zA-Z0-9_\'`\s\-]+?)\s+(?:fayl|faylini|faylni|hujjat|musiqa|qo\'shiq|video|rasm)", clean_q, re.IGNORECASE)
            if m_alt:
                file_query_str = m_alt.group(1).strip()

        user_home = Path(os.environ.get('USERPROFILE', os.path.expanduser('~')))
        downloads_base = self.base_dir / "downloads"

        search_dirs = [
            user_home / 'OneDrive' / 'Desktop',
            user_home / 'Desktop',
            downloads_base / 'music',
            downloads_base / 'photos',
            downloads_base / 'videos',
            downloads_base / 'documents',
            user_home / 'Downloads',
            user_home / 'Music',
            user_home / 'Videos',
            user_home / 'Pictures',
            user_home / 'Documents',
            self.base_dir
        ]

        raw_kws = file_query_str.split()
        filter_words = {'fayl', 'faylini', 'faylni', 'hujjat', 'top', 'rabochiy', 'stoldan', 'stol', 'desktop', 'ish', 'musiqa', 'qo\'shiq', 'video', 'rasm'}
        clean_kws = [k.lower() for k in raw_kws if len(k) > 1 and k.lower() not in filter_words]

        matched_file = None
        for s_dir in search_dirs:
            if not s_dir.exists():
                continue
            for root, dirs, files in os.walk(s_dir):
                depth = len(Path(root).relative_to(s_dir).parts)
                if depth > 1:
                    continue
                for f in files:
                    if f.startswith('~$'):
                        continue
                    f_lower = f.lower()
                    if clean_kws and all(k in f_lower for k in clean_kws):
                        matched_file = Path(root) / f
                        break
                if matched_file:
                    break
            if matched_file:
                break

        if not matched_file:
            return f"Kechirasiz, '{file_query_str}' bo'yicha ko'rsatilgan manzildan fayl topilmadi."

        try:
            from jarvis_user_telegram import JarvisUserTelegram
            caption = f"📄 {matched_file.name}"
            res = JarvisUserTelegram.send_telegram_file(target, str(matched_file), caption=caption)
            if res.get("success"):
                return f"📄 '{matched_file.name}' fayli muvaffaqiyatli topildi va Telegram orqali {res.get('target', target)} ga yuborildi."
            else:
                return f"Faylni Telegram orqali yuborishda xatolik yuz berdi: {res.get('error')}"
        except Exception as e:
            logger.error(f"Error sending file via telegram: {e}")
            return f"Fayl yuborishda xatolik yuz berdi: {e}"


    # =========================================================================
    # Central Command Processor (Gemini Function Calling + Fast-Path)
    # =========================================================================

    def process_command(self, query: str, speak_output: bool = True, user_id: Optional[str] = None) -> str:
        """Thread-safe command dispatcher protecting shared state across Telegram, Voice, and WebApp."""
        q = (query or "").lower().strip()
        # Pre-lock check: Immediate emergency silence / stop (non-blocking interruption)
        is_silence_cmd = (
            not any(w in q for w in ["dastur", "jarayon", "process", "fayl", "papka"])
            and (
                any(w in q for w in ["jim", "bas", "bo'ldi", "boldi", "gapirma", "ovozni o'chir"])
                or bool(re.search(r'\bto[\'\'\`\’]?xta\b', q))
                or q in ["stop", "to'xta", "toxta"]
            )
        )
        if is_silence_cmd:
            if getattr(self, "voice", None):
                try:
                    self.voice.stop_speaking()
                except Exception:
                    pass

            # If user explicitly said 'stop'/'to'xta' and a confirmation is pending, cancel the pending action
            safety_mgr = getattr(self, "safety", None) or getattr(self, "safety_manager", None)
            if q in ["stop", "to'xta", "toxta", "bekor", "cancel"] and safety_mgr:
                cancel_res = safety_mgr.cancel_pending()
                if cancel_res:
                    return cancel_res

            msg = "Xo'p bo'ladi, jim bo'ldim."
            if speak_output:
                self.speak(msg)
            return msg

        lock = getattr(self, "_command_lock", None)
        if lock:
            with lock:
                return self._process_command_impl(query, speak_output=speak_output, user_id=user_id)
        return self._process_command_impl(query, speak_output=speak_output, user_id=user_id)

    def _process_command_impl(self, query: str, speak_output: bool = True, user_id: Optional[str] = None) -> str:
        """Process a text or voice command from user with autonomous Gemini Function Calling.
        
        Args:
            query: The user input text.
            speak_output: If True, reads out the reply via PC speakers.
                          If False (e.g. from Telegram), stays completely silent on PC.
            user_id: Chat ID or identifier of user (for stateful dialogue tracking).
        """
        q = (query or "").lower().strip()
        raw_query = (query or "").strip()
        uid = str(user_id or getattr(config, "TELEGRAM_ADMIN_CHAT_ID", None) or "7710339509")
        self._clarification_dispatched_via_tg = False
        self._media_dispatched_via_tg = False

        def _speak_if_allowed(text_to_speak: str):
            if hasattr(self, "dialogue_manager") and self.dialogue_manager and text_to_speak:
                try:
                    self.dialogue_manager.append_chat_message(uid, "assistant", text_to_speak)
                except Exception as e:
                    logger.warning(f"Could not persist assistant chat message: {e}")
            if speak_output:
                self.speak(text_to_speak)

        # Sensitive Data Exposure Gate (CWE-532 / CWE-200):
        # Route actual credential submissions through dedicated local handler before logging, recording, or tracing
        pin_val = self._extract_credential(raw_query)

        if pin_val:
            logger.info(f"Processing command: '[REDACTED CREDENTIAL SUBMISSION]' (speak_output={speak_output}, user_id={uid})")
            if hasattr(self, "dialogue_manager") and self.dialogue_manager:
                self.dialogue_manager.append_chat_message(uid, "user", "[REDACTED CREDENTIAL SUBMISSION]")
            trace_ctx = self.tracer.start_trace("[REDACTED CREDENTIAL SUBMISSION]", source="voice" if speak_output else "telegram")

            # Verify workstation is currently locked before sending keystrokes
            is_currently_locked = True
            if hasattr(self, "sensor") and self.sensor:
                is_currently_locked = self.sensor.is_workstation_locked()
            elif hasattr(self, "system") and self.system:
                st = self.system.get_lock_status()
                is_currently_locked = st.get("is_locked", True)

            if is_currently_locked is False:
                reply = "Kompyuter allaqachon qulfdan chiqarilgan va faol."
                self.tracer.complete_trace(trace_ctx, reply, status="success")
            elif not (hasattr(self, "sensor") and self.sensor) and not (hasattr(self, "desktop") and self.desktop):
                reply = "Kechirasiz, klaviatura yoki sensor vositasi mavjud emas, PIN kiritib bo'lmadi."
                self.tracer.complete_trace(trace_ctx, reply, status="error")
            else:
                if hasattr(self, "sensor") and self.sensor:
                    self.sensor.unlock_with_pin(pin_val)
                elif hasattr(self, "desktop") and self.desktop:
                    self.desktop.keyboard_type(pin_val, press_enter=True)

                time.sleep(1.0)
                # Verify actual physical unlock state
                final_locked = True
                if hasattr(self, "sensor") and self.sensor:
                    final_locked = self.sensor.is_workstation_locked()
                elif hasattr(self, "system") and self.system:
                    st = self.system.get_lock_status()
                    final_locked = st.get("is_locked", True)

                if final_locked is False:
                    reply = "PIN muvaffaqiyatli kiritildi va kompyuter qulfdan chiqarildi."
                    self.tracer.complete_trace(trace_ctx, reply, status="success")
                else:
                    reply = "PIN kiritildi, ammo kompyuter qulfdan chiqmadi. Parol noto'g'ri bo'lishi mumkin."
                    self.tracer.complete_trace(trace_ctx, reply, status="verification_failed")

            _speak_if_allowed(reply)
            return reply

        logger.info(f"Processing command: '{q}' (speak_output={speak_output}, user_id={uid})")

        # Record user query in persistent dialogue history for two-way sync with Mini App
        if hasattr(self, "dialogue_manager") and self.dialogue_manager and query:
            try:
                self.dialogue_manager.append_chat_message(uid, "user", query)
            except Exception as e:
                logger.warning(f"Could not persist user chat message: {e}")

        # OpenJarvis-style Execution Trace context
        trace_ctx = self.tracer.start_trace(query, source="voice" if speak_output else "telegram")

        # ---------------------------------------------------------------------
        # Gate 0: Active Safety Confirmation Gate Check
        # ---------------------------------------------------------------------
        if hasattr(self, "safety") and self.safety and self.safety.has_pending():
            logger.info(f"Active safety confirmation pending. Evaluating user response: '{query}'")
            reply = self.safety.handle_confirmation_reply(query)
            if reply is not None:
                _speak_if_allowed(reply)
                return reply

        # ---------------------------------------------------------------------
        # Gate 0.1: Active Dialogue Clarification Check (User answers clarification)
        # ---------------------------------------------------------------------
        if hasattr(self, "dialogue_manager") and self.dialogue_manager and self.dialogue_manager.is_pending(uid):
            logger.info(f"User {uid} has pending clarification. Resolving with reply: '{query}'")
            resolved = self.dialogue_manager.resolve_clarification(uid, query)
            if resolved:
                if resolved.get("status") == "resolved":
                    final_reply = self._execute_resolved_clarification(resolved, speak_output=speak_output)
                    self.tracer.complete_trace(trace_ctx, final_reply, status="clarification_resolved")
                    _speak_if_allowed(final_reply)
                    return final_reply
                elif resolved.get("status") == "cancelled":
                    cancel_reply = "Xo'p bo'ladi, so'rov bekor qilindi."
                    self.tracer.complete_trace(trace_ctx, cancel_reply, status="clarification_cancelled")
                    _speak_if_allowed(cancel_reply)
                    return cancel_reply
                elif resolved.get("status") == "unresolved":
                    unres_reply = resolved.get("message") or "Iltimos, taklif qilingan variantlardan birini tanlang."
                    self.tracer.complete_trace(trace_ctx, unres_reply, status="clarification_unresolved")
                    _speak_if_allowed(unres_reply)
                    return unres_reply
                elif resolved.get("status") == "already_handled":
                    handled_reply = resolved.get("message") or "Ushbu so'rov allaqachon bajarilgan."
                    self.tracer.complete_trace(trace_ctx, handled_reply, status="clarification_handled")
                    _speak_if_allowed(handled_reply)
                    return handled_reply

        # ---------------------------------------------------------------------
        # Gate 0.2: Cognitive Ambiguity Check (Detects missing slots or 2-sided ambiguities)
        # ---------------------------------------------------------------------
        if hasattr(self, "ambiguity_engine") and self.ambiguity_engine:
            ambiguity_res = self.ambiguity_engine.analyze(query)
            if ambiguity_res.clarification_needed:
                logger.info(f"Ambiguity detected ({ambiguity_res.ambiguity_type}). Triggering hybrid clarification loop...")
                if hasattr(self, "dialogue_manager") and self.dialogue_manager:
                    self.dialogue_manager.set_pending_clarification(
                        user_id=uid,
                        original_query=query,
                        intent=ambiguity_res.intent,
                        ambiguity_type=ambiguity_res.ambiguity_type,
                        question=ambiguity_res.clarification_question,
                        options=ambiguity_res.clarification_options,
                        context_data={"raw_query": query, "entities": ambiguity_res.entities}
                    )
                q_text = ambiguity_res.clarification_question
                if self.telegram and not speak_output:
                    self.telegram.send_clarification_question(
                        chat_id=uid,
                        question=q_text,
                        options=ambiguity_res.clarification_options
                    )
                    self._clarification_dispatched_via_tg = True
                _speak_if_allowed(q_text)
                self.tracer.complete_trace(trace_ctx, q_text, status="clarification_requested")
                return q_text

        # ---------------------------------------------------------------------
        # 0. Immediate Silence / Stop Command (Voice / TTS interruption)
        # ---------------------------------------------------------------------
        is_silence_cmd = (
            not any(w in q for w in ["dastur", "jarayon", "process", "fayl", "papka"])
            and (
                any(w in q for w in ["jim", "bas", "bo'ldi", "boldi", "gapirma", "ovozni o'chir"])
                or bool(re.search(r'\bto[\'\'\`\’]?xta\b', q))
                or q in ["stop", "to'xta", "toxta"]
            )
        )
        if is_silence_cmd:
            if self.voice:
                self.voice.stop_speaking()
            msg = "Xo'p bo'ladi, jim bo'ldim."
            _speak_if_allowed(msg)
            return msg

        # ---------------------------------------------------------------------
        # 0.5 Safety Gate: Destructive Actions Interceptor
        # ---------------------------------------------------------------------
        # 0.5.1 PC Power: Shutdown & Restart
        is_pc_shutdown = (
            any(w in q for w in [
                "kompyuterni o'chir", "kompyuterni ochir", "tizimni o'chir",
                "tizimni ochir", "kompyuterni o'chirish", "kompyuterni ochirish",
                "kompyuterni o'chirib qo'y", "pcni o'chir", "pcni ochir"
            ])
            or q in ["shutdown", "kompyuterni o'chir", "kompyuterni ochir"]
        )
        is_pc_restart = any(w in q for w in [
            "qayta ishga tushir", "qayta ishga tushirish", "kompyuterni qayta ishga tushir",
            "qayta yoq", "restart", "reboot"
        ])

        if is_pc_shutdown:
            desc = "kompyuterni o'chirish (Shutdown)"
            prompt = self.safety.request_confirmation(
                "shutdown",
                desc,
                self.system.shutdown_pc,
                delay_seconds=10,
                restart=False,
            )
            _speak_if_allowed(prompt)
            return prompt

        if is_pc_restart:
            desc = "kompyuterni qayta ishga tushirish (Restart)"
            prompt = self.safety.request_confirmation(
                "restart",
                desc,
                self.system.shutdown_pc,
                delay_seconds=10,
                restart=True,
            )
            _speak_if_allowed(prompt)
            return prompt

        # 0.5.1 Process Termination Safety Check (e.g. 'notepad dasturini to'xtat', 'chrome dasturini yop')
        is_kill_proc = (
            any(w in q for w in ["dasturini to'xtat", "dasturini yop", "dasturni to'xtat", "dasturni yop", "processni to'xtat", "jarayonni to'xtat", "kill "])
            or (any(w in q for w in ["dastur", "jarayon", "process"]) and any(w in q for w in ["to'xtat", "toxtat", "yop", "o'ldir", "kill"]))
        )
        if is_kill_proc and not is_silence_cmd:
            m_app = re.search(r'([a-zA-Z0-9_\-]+)\s+(?:nomli\s+)?(?:dasturini|dasturni|jarayonini|jarayonni|processini|processni)\s+(?:to\'xtat|toxtat|yop|o\'ldir)', q)
            app_target = m_app.group(1).strip() if m_app else "dastur"
            desc = f"'{app_target}' dasturini yopish (majburiy to'xtatish)"
            prompt = self.safety.request_confirmation(
                "close_application",
                desc,
                self.system.close_application,
                name=app_target,
            )
            _speak_if_allowed(prompt)
            return prompt

        # 0.5.2 File & Directory Deletion
        is_delete_cmd = (
            (("o'chir" in q or "ochir" in q or "delete" in q or "remove" in q) and any(w in q for w in ["fayl", "papka", "folder", "katalog", "direktoriya", "file"]))
            or any(w in q for w in [
                "faylni o'chir", "faylni ochir", "fayl o'chir", "fayl ochir",
                "faylini o'chir", "faylini ochir", "fayllarni o'chir",
                "papkani o'chir", "papkani ochir", "papkasini o'chir", "papkasini ochir",
                "papka o'chir", "papka ochir",
                "katalogni o'chir", "direktoriyani o'chir", "format qilish", "diskni tozalash"
            ])
            or q.startswith("delete ")
            or q.startswith("remove ")
            or ("o'chir" in q and any(ext in q for ext in [".txt", ".log", ".json", ".py", ".tmp", ".bak", ".doc", ".pdf", ".mp3", ".png", ".jpg", ".csv"]))
            or ("ochir" in q and any(ext in q for ext in [".txt", ".log", ".json", ".py", ".tmp", ".bak", ".doc", ".pdf", ".mp3", ".png", ".jpg", ".csv"]))
        )

        if is_delete_cmd:
            is_folder = any(w in q for w in ["papka", "papkani", "papkasini", "folder", "katalog", "direktoriya"])
            target_raw = None
            m_colon = re.search(r'(?:o\'chir|ochir|delete|remove|format)[\s:]+([^\s]+)$', query, re.IGNORECASE)
            m_prefix = re.search(r'([^\s]+)\s+(?:nomli\s+)?(?:faylini|faylni|fayl|papkasini|papkani|papka)\s+(?:o\'chir|ochir)', query, re.IGNORECASE)
            m_del = re.search(r'(?:delete|remove)\s+(?:file\s+|folder\s+)?([^\s]+)', query, re.IGNORECASE)
            m_ni = re.search(r'([^\s]+)\s*(?:ni|ni\s+ham)\s+(?:o\'chir|ochir)', query, re.IGNORECASE)

            if m_colon:
                target_raw = m_colon.group(1).strip("'\"` ")
            elif m_prefix:
                target_raw = m_prefix.group(1).strip("'\"` ")
            elif m_del:
                target_raw = m_del.group(1).strip("'\"` ")
            elif m_ni:
                target_raw = m_ni.group(1).strip("'\"` ")

            if target_raw and target_raw.lower() not in ["faylni", "papkani", "papkasini", "ovozni", "kompyuterni", "dasturni"]:
                p = Path(target_raw)
                if p.is_absolute():
                    resolved = p
                else:
                    # Resolve parent directory to eliminate traversal ('..', '.'), while preserving the final component for symlink/junction checks
                    target_path = self.base_dir / p
                    resolved = target_path.parent.resolve() / target_path.name

                if is_folder:
                    desc = f"'{target_raw}' papkasini o'chirish"
                    callback_fn = self.system.delete_folder
                    kwargs = {"folderpath": str(resolved)}
                else:
                    desc = f"'{target_raw}' faylini o'chirish"
                    callback_fn = self.system.delete_file
                    kwargs = {"filepath": str(resolved)}

                prompt = self.safety.request_confirmation(
                    action_type="delete_folder" if is_folder else "delete_file",
                    description=desc,
                    callback_fn=callback_fn,
                    **kwargs,
                )
                _speak_if_allowed(prompt)
                return prompt
            else:
                msg = "Qaysi faylni yoki papkani o'chirmoqchisiz? Iltimos, nomini ko'rsating (masalan: 'test.txt faylini o'chir')."
                _speak_if_allowed(msg)
                return msg

        # ---------------------------------------------------------------------
        # 0.6 Vision Multimodal Inquiry Fast-Path
        # ---------------------------------------------------------------------
        if any(w in q for w in ["ko'ryapsan", "koryapsan", "qaragin", "nima bor", "kim bor"]) and "dastur" not in q:
            if self.vision:
                snap_path = self.vision.capture_snapshot()
                if self.gemini_model and snap_path:
                    _speak_if_allowed("Kamerani tahlil qilyapman, bir daqiqa...")
                    try:
                        import PIL.Image
                        img = PIL.Image.open(snap_path)
                        response = self.gemini_model.generate_content([
                            "Tasvirda nimalar ko'rinmoqda? Qisqa, aniq va do'stona o'zbek tilida 1-2 gapda bayon qil:",
                            img
                        ])
                        reply = response.text.strip()
                        _speak_if_allowed(reply)
                        return reply
                    except Exception as e:
                        logger.error(f"Gemini vision error: {e}")
                elif snap_path:
                    msg = f"Surat saqlandi: {snap_path}"
                    _speak_if_allowed(msg)
                    return msg

        # ---------------------------------------------------------------------
        # 1. Gemini Function Calling Autonomous Engine
        # ---------------------------------------------------------------------
        if config.is_gemini_configured() and self.tool_registry:
            user_context = self.memory.get_user_summary() if self.memory else "Foydalanuvchi: Azizbek Maripov"
            sensory_context = self.sensor.get_sensory_context() if hasattr(self, "sensor") and self.sensor else ""

            system_instruction = (
                "Siz Azizbek Maripovning zamonaviy, chaqqon, zukko va hazilkash shaxsiy yordamchisi Monikasiz (Jarvis darajasida).\n\n"
                f"FOYDALANUVCHI HAQIDA MA'LUMOT:\n{user_context}\n\n"
                f"JONLI SENSORIK KONTEKST:\n{sensory_context}\n\n"
                "QAT'IY ANTI-GALUTSINATSIYA VA SHAXSIYAT QOIDALARI:\n"
                "1. Hech qachon bajarilmagan ishni bajarildi deb da'vo qilmang. O'zingizdan mavjud bo'lmagan fayllar, faktlar yoki xabarlarni aslo to'qimang.\n"
                "2. Agar biror vosita (tool) xato qaytarsa, aynan nima xato bo'lganini rost va samimiy ayting.\n"
                "3. Javoblar faqat va faqat vositalar qaytargan real fizik faktlarga tayanishi shart.\n"
                "4. Til: Zamonaviy, tabiiy, lo'nda o'zbek tili (1-2 gap, ko'pi bilan 25 ta so'z). Ortiqcha so'z cho'zmang."
            )

            candidate_models = self.gemini_candidate_models
            tools = self.tool_registry.get_gemini_tools()

            for m_name in candidate_models:
                executed_tools = []
                try:
                    import google.generativeai as genai
                    m = genai.GenerativeModel(m_name, tools=tools)
                    chat = m.start_chat(enable_automatic_function_calling=False)
                    full_prompt = f"{system_instruction}\n\nFoydalanuvchi: {query}"

                    response = chat.send_message(full_prompt, request_options={"timeout": 30})

                    max_fc_turns = 3
                    fc_turn = 0

                    while fc_turn < max_fc_turns:
                        fc_turn += 1
                        function_calls = []
                        if response.candidates and response.candidates[0].content:
                            for part in response.candidates[0].content.parts:
                                if hasattr(part, "function_call") and part.function_call:
                                    function_calls.append(part.function_call)

                        if not function_calls:
                            break

                        turn_responses = []
                        for fc in function_calls:
                            fn_name = fc.name
                            fn_args = dict(fc.args) if fc.args else {}
                            logger.info(f"Executing Tool Call: {fn_name}({fn_args})")

                            tool_result = self.tool_registry.dispatch(fn_name, **fn_args)
                            # Record dispatch immediately before verification to prevent duplicate side effects on error
                            tool_idx = len(executed_tools)
                            executed_tools.append((fn_name, fn_args, tool_result, None))

                            v_record = None
                            try:
                                v_record = self.tracer.verify_tool_execution(fn_name, fn_args, tool_result, sensor=getattr(self, "sensor", None))
                                executed_tools[tool_idx] = (fn_name, fn_args, tool_result, v_record)
                            except Exception as v_err:
                                logger.warning(f"Verification raised error for {fn_name}: {v_err}")
                                v_record = {"verified": False, "verified_success": False, "reason": f"Verification error: {v_err}"}
                                executed_tools[tool_idx] = (fn_name, fn_args, tool_result, v_record)

                            trace_ctx["tools_called"].append({"tool": fn_name, "args": fn_args, "result": tool_result, "verification": v_record})

                            # Anti-Hallucination Safety Gate: If tool requires human confirmation, return prompt immediately
                            if isinstance(tool_result, dict) and tool_result.get("requires_confirmation"):
                                p_msg = tool_result.get("prompt") or tool_result.get("message") or "Ushbu amal xavfsizlik tasdig'ini talab qiladi."
                                self.tracer.complete_trace(trace_ctx, p_msg, status="safety_confirmation_required")
                                _speak_if_allowed(p_msg)
                                return p_msg

                            # Anti-Hallucination Verification Gate: If physical verification explicitly failed, block success reply!
                            if v_record and v_record.get("verified_success") is False:
                                fail_reason = v_record.get("reason", "Amal tizimda fizik tasdiqlanmadi")
                                fail_msg = f"Kechirasiz, {fail_reason}."
                                self.tracer.complete_trace(trace_ctx, fail_msg, status="verification_failed")
                                _speak_if_allowed(fail_msg)
                                return fail_msg

                            turn_responses.append(
                                genai.protos.Part(
                                    function_response=genai.protos.FunctionResponse(
                                        name=fn_name,
                                        response={"result": tool_result}
                                    )
                                )
                            )

                        try:
                            # Send all function responses in batch for the current turn
                            response = chat.send_message(
                                turn_responses,
                                request_options={"timeout": 30}
                            )
                        except Exception as fc_send_err:
                            logger.warning(
                                f"Chat function_response batch send returned exception ({fc_send_err}), "
                                "synthesizing natural response across all executed tools..."
                            )
                            summary_m = genai.GenerativeModel(m_name)
                            tools_summary_lines = []
                            for fn, args, res, v in executed_tools:
                                v_note = f" (Fizik tekshiruv: {v['reason']})" if v else ""
                                tools_summary_lines.append(f"- {fn}({args}) -> {res}{v_note}")
                            tools_summary_str = "\n".join(tools_summary_lines)

                            prompt_synth = (
                                f"{system_instruction}\n\n"
                                f"Foydalanuvchi so'rovi: '{query}'\n"
                                f"Amalga oshirilgan tizim vositalari:\n{tools_summary_str}\n\n"
                                f"Foydalanuvchiga nima aniq ishlar bajarilganini bildirib, 1-2 gapda samimiy, zamonaviy va lo'nda o'zbek tilida javob bering. Yolg'on gapirmang."
                            )
                            res_synth = summary_m.generate_content(prompt_synth, request_options={"timeout": 30})
                            final_reply = res_synth.text.strip()
                            self.tracer.complete_trace(trace_ctx, final_reply, status="success")
                            _speak_if_allowed(final_reply)
                            return final_reply

                    # Check if Gemini exited loop with unhandled pending tool calls at the limit
                    pending_fcs = []
                    if response and response.candidates and response.candidates[0].content:
                        for part in response.candidates[0].content.parts:
                            if hasattr(part, "function_call") and part.function_call:
                                pending_fcs.append(part.function_call.name)

                    if pending_fcs:
                        logger.warning(f"Tool round limit reached with pending tool calls: {pending_fcs}")
                        successful_names = []
                        failed_names = []
                        unverified_names = []
                        for fn, _, res, v in executed_tools:
                            if v and v.get("verified_success") is False:
                                failed_names.append(fn)
                            elif isinstance(res, dict) and res.get("success") is False:
                                failed_names.append(fn)
                            elif v and v.get("verified_success") is True:
                                successful_names.append(fn)
                            elif isinstance(res, dict) and res.get("success") is True:
                                unverified_names.append(fn)
                            else:
                                failed_names.append(fn)

                        completed_parts = []
                        if successful_names:
                            completed_parts.append(f"Muvaffaqiyatli: {', '.join(successful_names)}")
                        if unverified_names:
                            completed_parts.append(f"Bajarildi (tekshiruvsiz): {', '.join(unverified_names)}")
                        if failed_names:
                            completed_parts.append(f"Xatolik: {', '.join(failed_names)}")
                        status_details = "; ".join(completed_parts) if completed_parts else "amallar yakunlanmadi"
                        pending_str = ", ".join(pending_fcs)
                        incomplete_reply = f"Amallar qisman bajarildi ({status_details}). Biroq limit sababli quyidagilar bajarilmadi: {pending_str}."
                        self.tracer.complete_trace(trace_ctx, incomplete_reply, status="partially_completed")
                        _speak_if_allowed(incomplete_reply)
                        return incomplete_reply

                    if response and hasattr(response, "text") and response.text:
                        final_reply = response.text.strip()
                        self.tracer.complete_trace(trace_ctx, final_reply, status="success")
                        _speak_if_allowed(final_reply)
                        return final_reply
                    elif executed_tools:
                        tools_summary_lines = []
                        for fn, args, res, v in executed_tools:
                            v_note = f" (Fizik tekshiruv: {v['reason']})" if v else ""
                            tools_summary_lines.append(f"- {fn}({args}) -> {res}{v_note}")
                        tools_summary_str = "\n".join(tools_summary_lines)
                        summary_m = genai.GenerativeModel(m_name)
                        prompt_synth = (
                            f"{system_instruction}\n\n"
                            f"Foydalanuvchi so'rovi: '{query}'\n"
                            f"Amalga oshirilgan vositalar:\n{tools_summary_str}\n\n"
                            f"Foydalanuvchiga nima qilinganini 1-2 gapda samimiy va lo'nda bildiring. Yolg'on da'vo qilmang."
                        )
                        res_synth = summary_m.generate_content(prompt_synth, request_options={"timeout": 30})
                        final_reply = res_synth.text.strip()
                        self.tracer.complete_trace(trace_ctx, final_reply, status="success")
                        _speak_if_allowed(final_reply)
                        return final_reply

                except Exception as model_err:
                    err_str = str(model_err)
                    logger.warning(f"Gemini execution error with {m_name}: {model_err}")
                    # If tools were already executed, DO NOT replay command or fall through to other handlers!
                    if executed_tools:
                        logger.info("Tools already executed prior to exception; recovering reply without replaying command.")
                        summary_lines = []
                        for fn, args, res, v in executed_tools:
                            v_str = f" ({v['reason']})" if v else ""
                            summary_lines.append(f"{fn}: {res}{v_str}")
                        fallback_reply = "Quyidagi amallar bajarildi:\n" + "\n".join(summary_lines)
                        self.tracer.complete_trace(trace_ctx, fallback_reply, status="success")
                        _speak_if_allowed(fallback_reply)
                        return fallback_reply

                    if "429" in err_str or "quota" in err_str.lower() or "resourceexhausted" in err_str.lower():
                        logger.warning(f"Quota exhausted on {m_name}. Switching to offline/local fallback immediately.")
                        break
                    continue

        # ---------------------------------------------------------------------
        # 1.5 Local Fast-Path & Deterministic Command Engine (Runs BEFORE Local LLM)
        # Prevents Ollama hallucinating actions ("ovozni sozladim", "ekranni qulfladim") without executing them
        # ---------------------------------------------------------------------
        logger.info(f"Gemini unavailable or bypassed. Running local fast-path for: '{query}'")
        fast_path_reply = self._execute_local_fast_path(query, speak_output=speak_output, user_id=user_id)
        if fast_path_reply is not None:
            self.tracer.complete_trace(trace_ctx, fast_path_reply, status="fast_path")
            return fast_path_reply

        # ---------------------------------------------------------------------
        # 2. Local On-Device LLM Failover (OpenJarvis style conversational fallback)
        # ---------------------------------------------------------------------
        if hasattr(self, "local_llm") and self.local_llm and self.local_llm.is_available():
            logger.info("Attempting local offline LLM generation for general conversation...")
            sensory_context = self.sensor.get_sensory_context() if hasattr(self, "sensor") and self.sensor else ""
            local_sys = f"Siz Monika (Jarvis)siz. O'zbek tilida 1-2 gapda aniq va rost javob bering. Holat: {sensory_context}"
            local_reply = self.local_llm.generate(query, system=local_sys)
            if local_reply:
                self.tracer.complete_trace(trace_ctx, local_reply, status="local_llm")
                _speak_if_allowed(local_reply)
                return local_reply

        # Fallback safe response
        fallback_msg = f"Xo'p bo'ladi, buyruq qabul qilindi: '{query}'."
        self.tracer.complete_trace(trace_ctx, fallback_msg, status="fallback")
        _speak_if_allowed(fallback_msg)
        return fallback_msg

    # =========================================================================
    # Local Fast-Path Dispatcher
    # =========================================================================

    def _execute_local_fast_path(self, query: str, speak_output: bool = True, user_id: Optional[str] = None) -> Optional[str]:
        """Execute built-in deterministic offline commands."""
        raw_q = (query or "").strip()
        q = raw_q.lower()

        def _speak_if_allowed(text_to_speak: str):
            if hasattr(self, "dialogue_manager") and self.dialogue_manager and text_to_speak:
                self.dialogue_manager.append_chat_message(user_id or "default", "assistant", text_to_speak)
            if speak_output:
                self.speak(text_to_speak)

        # 0. Friendly Greetings & Health Check Fast-Path
        if any(w in q for w in ["salom", "assalomu alaykum", "qalaysan", "qalesan", "ahvoling", "ahvol qanday", "xolat qanday", "holat qanday"]):
            stats = self.system.get_system_stats() if self.system else {}
            cpu = stats.get("cpu_percent", 0)
            ram = stats.get("ram_percent", 0)
            msg = f"Assalomu alaykum, Azizbek! Hammasi a'lo darajada va nazorat ostida. Tizim holati barqaror (CPU: {cpu}%, RAM: {ram}%). Sizga qanday yordam bera olaman?"
            _speak_if_allowed(msg)
            return msg

        # 1. Unlock & Wake Lock Screen Commands
        is_unlock_or_wake = (
            any(w in q for w in [
                "ekranni uyg'ot", "ekranni och", "kompyuterni och", "qulfni och",
                "kod kiritish joyini och", "kod kiritish", "parol kiritish",
                "qulfdan chiqar", "qulfdan och", "unlock"
            ])
            or ("ochish kerak" in q and any(w in q for w in ["kompyuter", "ekran", "qulf", "lock", "kod"]))
        )
        if is_unlock_or_wake:
            if hasattr(self, "sensor") and self.sensor:
                self.sensor.wake_lock_screen()
            elif hasattr(self, "desktop") and self.desktop:
                self.desktop.key_press("space")
            msg = "Ekranni uyg'otdim va PIN kiritish joyini ochdim. Kodingizni aytsangiz, darhol terib beraman."
            _speak_if_allowed(msg)
            return msg

        # 2. PIN / Password Submission to Unlock Workstation
        is_pin_submission = (
            any(q.startswith(w) for w in ["parol ", "kod ", "pin ", "parol:", "kod:", "pin:"])
            or ("parol" in q and any(ch.isdigit() for ch in q))
            or ("kod" in q and any(ch.isdigit() for ch in q))
        )
        if is_pin_submission:
            m_pin = re.search(r'(?:parol|kod|pin|parolim|kodim)?\s*[:=]?\s*([a-zA-Z0-9!@#\$%\^&*_\-]+)', q)
            if m_pin:
                pin_val = m_pin.group(1).strip()
                if hasattr(self, "sensor") and self.sensor:
                    self.sensor.unlock_with_pin(pin_val)
                elif hasattr(self, "desktop") and self.desktop:
                    self.desktop.keyboard_type(pin_val, press_enter=True)
                msg = "PIN terildi va Enter bosildi. Kompyuter qulfdan chiqarildi, marhamat!"
                _speak_if_allowed(msg)
                return msg

        # 3. Lock Command (Strictly exclude unlock/open queries)
        is_lock_cmd = (
            (any(w in q for w in ["qulfla", "blokla"]) or (bool(re.search(r'\block\b', q)) and "unlock" not in q))
            and not any(w in q for w in ["och", "chiqar", "uyg'ot", "kerak", "qanday"])
        )
        if is_lock_cmd:
            res_lock = self.system.lock_workstation() if self.system else {"success": False}
            if not res_lock.get("success"):
                msg = "Kompyuter ekranini qulflab bo'lmadi."
                _speak_if_allowed(msg)
                return msg
            msg = "Kompyuter ekrani muvaffaqiyatli qulflandi."
            _speak_if_allowed(msg)
            return msg

        # 4. Active Window / What is on screen
        if any(w in q for w in ["qaysi dasturdaman", "ekranda nima ochiq", "qaysi oynadaman", "faol dastur", "qayerda turibman kompyuterda"]):
            if hasattr(self, "sensor") and self.sensor:
                win = self.sensor.get_active_window()
                msg = f"Hozir sizda {win['process']} dasturi faol. Oyna sarlavhasi: \"{win['title']}\"."
            else:
                msg = "Dasturlarni aniqlash senzori faol emas."
            _speak_if_allowed(msg)
            return msg

        # 5. Clipboard / Bufer
        if any(w in q for w in ["buferda nima bor", "clipboardda nima bor", "nusxalangan narsa nima", "buferni o'qi"]):
            if hasattr(self, "sensor") and self.sensor:
                clip = self.sensor.get_clipboard_text()
                if clip:
                    msg = f"Buferdagi matn: \"{clip[:120]}\""
                else:
                    msg = "Hozircha bufer bo'sh, hech narsa nusxalanmagan."
            else:
                msg = "Bufer senzori faol emas."
            _speak_if_allowed(msg)
            return msg

        # 6. Volume Control
        if any(w in q for w in ["ovozni balandlat", "ovozni oshir", "ovozni ko'tar", "ovozni balandroq"]):
            if hasattr(self, "sensor") and self.sensor:
                self.sensor.volume_up(5)
            elif self.tool_registry:
                self.tool_registry.set_volume(80)
            msg = "Ovoz balandroq qilindi."
            _speak_if_allowed(msg)
            return msg

        if any(w in q for w in ["ovozni pasaytir", "ovozni kamaytir", "ovozni sekinroq"]):
            if hasattr(self, "sensor") and self.sensor:
                self.sensor.volume_down(5)
            elif self.tool_registry:
                self.tool_registry.set_volume(30)
            msg = "Ovoz pasaytirildi."
            _speak_if_allowed(msg)
            return msg

        if any(w in q for w in ["ovozni o'chir", "tovushni o'chir", "ovozni yop"]):
            if hasattr(self, "sensor") and self.sensor:
                self.sensor.volume_mute()
            elif self.tool_registry:
                self.tool_registry.mute_volume()
            msg = "Kompyuter ovozi o'chirildi (mute)."
            _speak_if_allowed(msg)
            return msg

        # Specific volume level (e.g., "ovozni 50 ga qo'y", "ovozni 60% qil")
        m_vol = re.search(r'ovoz(?:ni)?\s*(\d+)(?:\s*ga|\s*foiz|\s*%)?\s*(?:qilib\s*qo\'y|qo\'y|qoy|sozla|qil)', q)
        if m_vol:
            vol_val = max(0, min(100, int(m_vol.group(1))))
            if self.tool_registry:
                self.tool_registry.set_volume(vol_val)
            elif self.system:
                self.system.set_volume(vol_val)
            msg = f"Ovoz balandligi {vol_val}% ga sozlandi."
            _speak_if_allowed(msg)
            return msg

        # 7. Heavy Processes / Qotish tahlili
        if any(w in q for w in ["nima qotyapti", "kompyuter sekinlashdi", "ko'p ram", "ko'p xotira", "qaysi dastur og'ir"]):
            if hasattr(self, "sensor") and self.sensor:
                top_p = self.sensor.get_top_processes(3)
                lines = ["⚡ <b>Eng ko'p xotira (RAM) yeyayotgan dasturlar:</b>"]
                for idx, p in enumerate(top_p, 1):
                    lines.append(f"{idx}. <b>{p['name']}</b> — {p['ram_mb']} MB (PID: {p['pid']})")
                msg = "\n".join(lines)
            else:
                msg = "Jarayonlar senzori faol emas."
            _speak_if_allowed(msg)
            return msg

        # 8. Application & Website Launching
        if q.startswith("och ") or q.endswith("ni och") or q.endswith("ni qo'y"):
            target = re.sub(r'^(?:monika|iltimos|tezda)?\s*(?:och\s+)?', '', q).strip()
            target = re.sub(r'(?:ni\s+och|ni\s+qo\'y|ni\s+qoy|dasturini\s+och)$', '', target).strip()
            if target and target not in ["kod", "parol", "qulf", "ekran"]:
                if hasattr(self, "sensor") and self.sensor:
                    res = self.sensor.open_target(target)
                    if res.get("status") == "success":
                        msg = f"{target.capitalize()} ochildi!"
                        _speak_if_allowed(msg)
                        return msg
                elif self.tool_registry:
                    res = self.tool_registry.launch_application(target)
                    if res.get("success"):
                        msg = f"{target.capitalize()} ochildi!"
                        _speak_if_allowed(msg)
                        return msg

        # 9. System Stats
        if any(w in q for w in ["statistika", "status", "tizim holati", "holat", "xolat", "ahvol"]):
            stats = self.system.get_system_stats()
            msg = f"Tizim holati joyida: CPU {stats['cpu_percent']}%, RAM {stats['ram_percent']}%. Hammasi nazorat ostida."
            _speak_if_allowed(msg)
            return msg

        # 9.1 Web Search Fast-Path (Strictly exclude local files, telegram, and saved notes search)
        is_local_search = any(w in q for w in ["fayl", "faylini", "hujjat", "papka", "kompyuter", "telegram", "saved", "saqlangan", "eslatma", "qayd", "kontakt", "lichka"])
        if not is_local_search and any(w in q for w in ["google qidir", "qidiruv", "qidir"]) and not any(w in q for w in ["qayerda", "chiqar", "qulfdan"]):
            m_search = re.search(r'(?:google\s+dan\s+qidir|google\s+qidir|internetdan\s+qidir|qidiruv|qidir)[\s:]+(.+)$', query, re.IGNORECASE)
            search_query = m_search.group(1).strip() if m_search else re.sub(r'^(?:monika|iltimos|tezda)?\s*(?:google(?:\s+dan)?\s+)?qidir(?:uv)?[\s:]*', '', q).strip()
            if search_query:
                if self.tool_registry:
                    res = self.tool_registry.search_google(search_query, num_results=3)
                    if res.get("success"):
                        reply = f"🔍 '{search_query}' bo'yicha Google qidiruvi bajarildi."
                    else:
                        reply = f"Qidiruvda xatolik: {res.get('error', 'Noma\'lum xatolik')}"
                else:
                    reply = f"🔍 '{search_query}' bo'yicha qidiruv so'rovi qabul qilindi."
                _speak_if_allowed(reply)
                return reply

        # 9.2 Reminder Addition Fast-Path
        if any(w in q for w in ["eslatma qo'sh", "eslatma qosh", "eslatma o'rnat", "eslatma ornat", "eslatib qo'y", "eslatib qoy"]):
            m_rem = re.search(r'(?:eslatma\s+qo\'sh|eslatma\s+qosh|eslatma\s+o\'rnat|eslatma\s+ornat|eslatib\s+qo\'y|eslatib\s+qoy)[\s:]+(.+)$', query, re.IGNORECASE)
            raw_rem = m_rem.group(1).strip() if m_rem else q
            m_delay = re.search(r'(\d+)\s*(?:daqiqa|minut)', raw_rem, re.IGNORECASE)
            delay_mins = int(m_delay.group(1)) if m_delay else 0
            rem_text = re.sub(r'\s*\d+\s*(?:daqiqa|minut)(?:dan\s+keyin)?', '', raw_rem, flags=re.IGNORECASE).strip()
            if not rem_text:
                rem_text = "Eslatma"
            saved = False
            error_msg = None
            if self.tool_registry:
                res = self.tool_registry.add_reminder(rem_text, delay_minutes=delay_mins)
                if res.get("success"):
                    saved = True
                else:
                    error_msg = res.get("error")
            elif hasattr(self, "scheduler") and self.scheduler:
                try:
                    self.scheduler.add_reminder(rem_text, delay_minutes=delay_mins)
                    saved = True
                except Exception as s_err:
                    error_msg = str(s_err)

            if saved:
                reply = f"Eslatma muvaffaqiyatli saqlandi: \"{rem_text}\"."
            else:
                err_detail = f": {error_msg}" if error_msg else ""
                reply = f"Kechirasiz, eslatmani saqlash imkoni bo'lmadi{err_detail}."
            _speak_if_allowed(reply)
            return reply

        # 10. Personal Memory & Context Engine
        if any(w in q for w in ["men qayerdaman", "joylashuvim", "qayerdaman", "qayerdaligim", "qayerda turibman"]):
            loc = self.memory.get_location() if self.memory else "Toshkent, O'zbekiston"
            reply = f"📍 Sizning oxirgi qayd etilgan joylashuvingiz: {loc}."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["rejalarim", "maqsadlarim", "rejam nima", "qanday rejalarim bor", "rejalar"]):
            if self.memory:
                plans = self.memory.get_plans()
                if not plans:
                    reply = "Hozirda ro'yxatda faol rejalar mavjud emas."
                else:
                    lines = [f"🎯 <b>Sizning faol rejalaringiz va maqsadlaringiz ({len(plans)} ta):</b>"]
                    for idx, p in enumerate(plans, 1):
                        lines.append(f"{idx}. [{p.get('status', 'ACTIVE')}] <b>{p.get('title')}</b> ({p.get('category', 'Umumiy')})")
                    reply = "\n".join(lines)
            else:
                reply = "Xotira moduli faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["meni taniysanmi", "men haqimda", "men kimman", "profilim", "ma'lumotlarim"]):
            if self.memory:
                reply = self.memory.get_user_summary()
            else:
                reply = "Foydalanuvchi ma'lumotlari mavjud emas."
            _speak_if_allowed(reply)
            return reply

        # 10.1 Telegram Saved Messages & Notes Fast-Path
        # Let explicit media transfers (e.g. "saved messages dan @user ga video yubor") reach _handle_telegram_file_send
        has_transfer_intent = any(w in q for w in ["jo'nat", "jonat", "yubor", "tashla", "yetkaz", "send", "uzat"]) and ("@" in q or bool(re.search(r'\b(?:ga|ka|qa)\b', q)))
        if not has_transfer_intent and any(w in q for w in ["saqlangan xabarlar", "saved messages", "eslatmalarim", "qaydlarim", "telegramdagi qayd", "saqlangan xabar"]):
            from jarvis_omni_indexer import TelegramOmniIndexer
            kw_match = re.search(r'(?:saqlangan\s+xabarlar(?:dan|imdan)?|saved\s+messages(?:dan)?)\s+(?:haqida\s+)?(.+)', q)
            search_term = ""
            if kw_match:
                cand_kw = kw_match.group(1).strip()
                cand_kw = re.sub(r'\b(?:ni|qidir|top|ko\'r|kor|ayt|ber)\b', '', cand_kw).strip()
                if cand_kw:
                    search_term = cand_kw

            if search_term:
                matches = TelegramOmniIndexer.search_saved_messages(search_term, limit=5)
                if matches:
                    lines = [f"📌 <b>Telegram 'Saved Messages' dan topilgan qaydlar ('{search_term}'):</b>"]
                    for idx, m in enumerate(matches, 1):
                        lines.append(f"{idx}. {m.get('snippet', '')}")
                    reply = "\n\n".join(lines)
                else:
                    reply = f"Telegram 'Saved Messages' da '{search_term}' bo'yicha hech qanday qayd topilmadi."
            else:
                data = TelegramOmniIndexer.load_cache()
                saved = data.get("saved_messages", [])[:5]
                if saved:
                    lines = [f"📌 <b>Telegram 'Saved Messages' (oxirgi {len(saved)} ta qayd):</b>"]
                    for idx, s in enumerate(saved, 1):
                        lines.append(f"{idx}. {s.get('snippet', '')}")
                    reply = "\n\n".join(lines)
                else:
                    reply = "Telegram 'Saved Messages' qaydlari topilmadi."
            _speak_if_allowed(reply)
            return reply

        # 10.2 Telegram Contact & Personal Chat Lookup Fast-Path
        is_contact_inquiry = (
            any(w in q for w in ["kontakt", "kontaktlar", "shaxsiy chatlar", "chatlar ro'yxati"])
            or bool(re.search(r'([a-zA-Z0-9_\'`]+)\s+(?:kim|haqida|bilan\s+yozishmalar|ning\s+raqami|ning\s+telefoni|chatini\s+ko\'rsat)', q))
        )
        if is_contact_inquiry and not any(w in q for w in ["jo'nat", "yubor", "tashla", "och", "skrinshot", "yoz"]):
            from jarvis_omni_indexer import TelegramOmniIndexer
            m_target = re.search(r'([a-zA-Z0-9_\'`]+)\s+(?:kim|haqida|bilan\s+yozishmalar|ning\s+raqami|ning\s+telefoni)', q)
            target_name = m_target.group(1).strip() if m_target else ""
            if target_name.lower() in ["men", "sen", "monika", "jarvis", "dastur", "bu"]:
                target_name = ""

            if target_name:
                contacts = TelegramOmniIndexer.search_contacts_and_chats(target_name)
                if contacts:
                    c = contacts[0]
                    uname = c.get("username") or "Mavjud emas"
                    phone = c.get("phone") or "Mavjud emas"
                    name = c.get("name") or target_name
                    lines = [
                        f"👤 <b>Telegram Kontaktdosh:</b> {name}",
                        f"📱 <b>Username:</b> {uname}",
                        f"📞 <b>Telefon:</b> {phone}",
                    ]
                    if c.get("recent_history"):
                        lines.append("\n💬 <b>Oxirgi yozishmalar:</b>")
                        for h in c["recent_history"]:
                            lines.append(f"• {h}")
                    reply = "\n".join(lines)
                else:
                    reply = f"Telegram kontaktlari orasida '{target_name}' topilmadi."
                _speak_if_allowed(reply)
                return reply
            elif any(w in q for w in ["shaxsiy chatlar", "chatlar ro'yxati"]):
                data = TelegramOmniIndexer.load_cache()
                chats = data.get("direct_chats", [])[:6]
                if chats:
                    lines = [f"💬 <b>Sizning Telegram shaxsiy chatlaringiz ({len(chats)} ta):</b>"]
                    for idx, c in enumerate(chats, 1):
                        lines.append(f"{idx}. <b>{c.get('name')}</b> ({c.get('username') or 'No username'}) — <i>\"{c.get('last_message', '')[:40]}\"</i>")
                    reply = "\n".join(lines)
                else:
                    reply = "Shaxsiy chatlar ma'lumoti topilmadi."
                _speak_if_allowed(reply)
                return reply

        # 10.3 PC Installed Applications Inquiry Fast-Path
        if any(w in q for w in ["qanday dasturlar bor", "qanday ilovalar bor", "o'rnatilgan dasturlar", "qanday programmalar bor", "dasturlar ro'yxati"]):
            from jarvis_omni_indexer import PCAppsIndexer
            apps = PCAppsIndexer.load_cache()
            if apps:
                categories = {}
                for a in apps:
                    cat = a.get("category", "Boshqa")
                    categories.setdefault(cat, []).append(a.get("name"))

                lines = [f"💻 <b>Kompyuterda o'rnatilgan dasturlar (jami {len(apps)} ta):</b>"]
                for cat, app_list in list(categories.items())[:5]:
                    lines.append(f"• <b>{cat}:</b> {', '.join(app_list[:4])}{'...' if len(app_list) > 4 else ''}")
                lines.append("\n<i>Biror dasturni ochish uchun '... ni och' deb buyruq bering.</i>")
                reply = "\n".join(lines)
            else:
                reply = "O'rnatilgan dasturlar indeksi hozircha bo'sh."
            _speak_if_allowed(reply)
            return reply

        # 10.4 PC File Search Fast-Path
        if any(w in q for w in ["fayl qidir", "faylini qidir", "kompyuterdan qidir"]) and not any(w in q for w in ["google", "internet"]):
            m_fsearch = re.search(r'(?:fayl\s+qidir|faylini\s+qidir|kompyuterdan\s+qidir)[\s:]+(.+)$', query, re.IGNORECASE)
            f_query = m_fsearch.group(1).strip() if m_fsearch else ""
            if f_query:
                from jarvis_omni_indexer import PCFilesIndexer
                matches = PCFilesIndexer.search_files(f_query, limit=5)
                if matches:
                    lines = [f"📁 <b>Kompyuterdan topilgan fayllar ('{f_query}'):</b>"]
                    for idx, f in enumerate(matches, 1):
                        lines.append(f"{idx}. <b>{f.get('name')}</b> ({f.get('size_mb', 0)} MB)\n   <code>{f.get('path')}</code>")
                    reply = "\n\n".join(lines)
                else:
                    reply = f"Kompyuteringizda '{f_query}' nomli fayl topilmadi."
                _speak_if_allowed(reply)
                return reply

        # 11. Screenshots & Telegram Image Dispatch
        is_screen_req = (
            any(w in q for w in ["skrinshot", "screenshot"]) or
            ("ekran" in q and any(w in q for w in ["rasm", "surat", "ol", "jo'nat", "jonat", "yubor", "tashla", "ko'r", "kor"]))
        )
        if is_screen_req:
            _speak_if_allowed("Kompyuter ekrani surati olinmoqda...")
            sc_path = None
            if self.desktop:
                try:
                    sc = self.desktop.take_screenshot()
                    if sc and sc.get("status") == "success":
                        sc_path = sc.get("path")
                except Exception as sc_err:
                    logger.warning(f"Desktop screenshot failed: {sc_err}")

            if not sc_path or not os.path.exists(sc_path):
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
                    if hdesk:
                        user32.SetThreadDesktop(hdesk)
                    from PIL import ImageGrab
                    img = ImageGrab.grab()
                    snap_dir = self.base_dir / "snapshots"
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    fallback_p = snap_dir / f"desktop_{int(time.time()*1000)}.png"
                    img.save(str(fallback_p))
                    sc_path = str(fallback_p)
                except Exception as fb_err:
                    logger.error(f"Screenshot fallback error: {fb_err}")

            if sc_path and os.path.exists(sc_path):
                self.last_screenshot_path = sc_path
                self.last_file_path = sc_path
                u_id = user_id
                if u_id and self.dialogue_manager:
                    self.dialogue_manager.update_context(str(u_id), last_file=sc_path, last_screenshot=sc_path)

                target_recipient = None
                at_m = re.search(r'(@[a-zA-Z0-9_]{3,})', raw_q)
                if at_m:
                    target_recipient = at_m.group(1).strip()
                else:
                    m_target = re.search(
                        r'(?:telegramda\s+)?([a-zA-Z0-9_\'`]+?)(?:ga|ka|qa)\s*(?:nikiga\s+ega\s+)?(?:tanishim|do[\'’`]?stim|hamkasbim)?',
                        q,
                        re.IGNORECASE
                    )
                    if m_target:
                        cand = m_target.group(1).strip()
                        if cand.lower() not in ["telegram", "bot", "monika", "fayl", "menga", "bizga", "o'zim", "ozim", "ekran", "rasm", "skrinshot"]:
                            target_recipient = cand

                known_aliases = {
                    "xurshid": "@XuRsHiDuLLoH",
                    "xurshidulloh": "@XuRsHiDuLLoH",
                    "jamshid": "@Jamsh1d_Sodiqov"
                }
                if target_recipient and target_recipient.lower() in known_aliases:
                    target_recipient = known_aliases[target_recipient.lower()]

                if target_recipient:
                    try:
                        from jarvis_user_telegram import JarvisUserTelegram
                        caption = f"🖥️ Kompyuter Ekrani Surati (Jonli Skrinshot)\n⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        res = JarvisUserTelegram.send_telegram_file(target_recipient, sc_path, caption=caption)
                        if res.get("success"):
                            reply = f"🖥️ Kompyuter ekrani muvaffaqiyatli suratga olindi va Telegram orqali {res.get('target', target_recipient)} ga yuborildi."
                        else:
                            reply = f"📸 Ekran surati olindi, ammo {target_recipient} ga yuborishda xatolik yuz berdi: {res.get('error')}"
                    except Exception as tg_err:
                        logger.error(f"Error sending screenshot to recipient {target_recipient}: {tg_err}")
                        reply = f"📸 Ekran surati olindi, ammo yuborishda xatolik: {tg_err}"
                else:
                    send_to_tg = (not speak_output) or any(w in q for w in ["jo'nat", "jonat", "yubor", "tashla", "telegram", "menga"])
                    if send_to_tg and self.telegram:
                        caption = f"🖥️ <b>Kompyuter Ekrani Surati (Jonli Skrinshot):</b>\n⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        sent = self.telegram.send_photo(sc_path, caption=caption)
                        if sent:
                            self._media_dispatched_via_tg = True
                            reply = "🖥️ Kompyuter ekrani muvaffaqiyatli suratga olindi va Telegramingizga rasm shaklida yuborildi."
                        else:
                            reply = f"📸 Ekran surati olindi: <code>{sc_path}</code>, ammo Telegramga yuklashda xatolik yuz berdi."
                    else:
                        reply = f"📸 Kompyuter ekrani surati muvaffaqiyatli olindi: <code>{sc_path}</code>."
            else:
                reply = "⚠️ Ekranni suratga olib bo'lmadi. Tizim video/ekran buferiga ulana olmadi."

            _speak_if_allowed(reply)
            return reply

        # 12. Mouse Cursor Movement & Clicks
        if any(w in q for w in ["sichqonchani siljit", "kursorni siljit"]):
            if not self.desktop:
                reply = "Desktop moduli faol emas."
            else:
                m_coords = re.search(r'(\d+)\s*(?:ga|x|va|,)\s*(\d+)', q)
                if m_coords:
                    mx, my = int(m_coords.group(1)), int(m_coords.group(2))
                    try:
                        res = self.desktop.mouse_move(mx, my, smooth=True)
                    except Exception as desk_err:
                        res = {"success": False, "error": str(desk_err)}

                    actual_x = res.get("x") if isinstance(res, dict) else None
                    actual_y = res.get("y") if isinstance(res, dict) else None
                    if res.get("success") and actual_x is not None and actual_y is not None:
                        reply = f"Sichqoncha kursorini ({actual_x}, {actual_y}) koordinatasiga siljitdim."
                    elif actual_x is not None and actual_y is not None:
                        reply = f"Kursor ({actual_x}, {actual_y}) koordinatasiga yetdi (mo'ljal: {mx}, {my})."
                    elif res.get("moved_only"):
                        reply = f"Kursor ({mx}, {my}) tomon siljitildi, biroq yakuniy koordinatani o'qib bo'lmadi."
                    elif res.get("error"):
                        reply = f"Kursor holatini aniqlashda xatolik yuz berdi: {res.get('error')}."
                    else:
                        reply = "Kursor siljitildi, biroq yakuniy pozitsiyani tasdiqlab bo'lmadi."
                else:
                    reply = "Iltimos, kursor koordinatasini ko'rsating (masalan: 'kursorni siljit 500x300')."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["ikki marta bos", "double click"]):
            if self.desktop:
                self.desktop.mouse_double_click()
                reply = "Sichqonchaning chap tugmasi bilan ikki marta bosildi."
            else:
                reply = "Desktop moduli faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["o'ng tugmani bos", "ong tugmani bos", "right click"]):
            if self.desktop:
                self.desktop.mouse_right_click()
                reply = "Sichqonchaning o'ng tugmasi bosildi."
            else:
                reply = "Desktop moduli faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["sichqonchani bos", "bitta bos", "tugmani bos", "kursorni bos", "click qil"]):
            if self.desktop:
                self.desktop.mouse_click()
                reply = "Sichqoncha kursor turgan joyda bosildi."
            else:
                reply = "Desktop moduli faol emas."
            _speak_if_allowed(reply)
            return reply

        # 13. Keyboard Typing
        if any(w in q for w in ["matn yoz", "klaviaturada yoz", "matn kirit"]):
            m_txt = re.search(r'(?:matn\s+yoz|klaviaturada\s+yoz|matn\s+kirit)[\s:]+["\']?(.+?)["\']?$', query, re.IGNORECASE)
            if m_txt and self.desktop:
                to_type = m_txt.group(1).strip()
                self.desktop.keyboard_type(to_type, press_enter=False)
                reply = f"Klaviaturada matn terildi: '{to_type}'."
            else:
                reply = "Kiritilishi kerak bo'lgan matn topilmadi. Masalan: 'matn yoz: Salom dunyo'."
            _speak_if_allowed(reply)
            return reply

        # 14. Window Management
        if any(w in q for w in ["ochiq oynalar", "dasturlar ro'yxati", "qaysi oynalar ochiq"]):
            if self.desktop:
                wins = self.desktop.list_windows()
                lines = [f"💻 <b>Ochiq dasturlar va oynalar ({len(wins)} ta):</b>"]
                for idx, w_item in enumerate(wins[:8], 1):
                    lines.append(f"{idx}. {w_item['title'][:60]}")
                reply = "\n".join(lines)
            else:
                reply = "Desktop moduli faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["oynani och", "oynaga o't", "dasturga o't"]):
            m_win = re.search(r'(?:oynani\s+och|oynaga\s+o\'t|dasturga\s+o\'t)[\s:]+([a-zA-Z0-9_\'\`\s]+)', q)
            if m_win and self.desktop:
                target_title = m_win.group(1).strip()
                res = self.desktop.focus_window(target_title)
                if res.get("status") == "success":
                    reply = f"'{res.get('title')}' oynasi oldi planga chiqarildi."
                else:
                    reply = f"'{target_title}' nomli faol oyna topilmadi."
            else:
                reply = "Oyna nomi aniqlanmadi."
            _speak_if_allowed(reply)
            return reply

        # 15. Antigravity Deep Integration
        if any(w in q for w in ["loyihalar ro'yxati", "loyihalarim", "qanday loyihalar bor", "loyihalar nima", "proyektlar"]):
            if self.antigravity:
                projs = self.antigravity.list_projects()
                lines = [f"📂 <b>Antigravity loyihalari ({len(projs)} ta):</b>"]
                for idx, pr in enumerate(projs, 1):
                    lines.append(f"{idx}. <b>{pr['name']}</b> — {pr['description']}")
                reply = "\n".join(lines)
            else:
                reply = "Antigravity ko'prigi faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["yangi loyiha och", "yangi proyekt och", "yangi loyiha yarat", "proyekt yarat"]):
            m_pr = re.search(r'(?:yangi\s+loyiha\s+(?:och|yarat)|yangi\s+proyekt\s+(?:och|yarat))[\s:]+([a-zA-Z0-9_\-]+)', q)
            p_name = m_pr.group(1).strip() if m_pr else f"project_{int(time.time())}"
            if self.antigravity:
                res = self.antigravity.create_project(p_name, description="Monika orqali yaratilgan yangi loyiha.")
                reply = f"✅ {res.get('message')}\n📁 Manzil: <code>{res.get('path')}</code>"
            else:
                reply = "Antigravity ko'prigi faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["oxirgi muloqot", "antigravitydagi chat", "chatda nima bo'ldi", "chat tarixi"]):
            if self.antigravity:
                summary = self.antigravity.read_latest_chat_summary(max_lines=6)
                reply = f"📝 <b>Antigravity oxirgi muloqot jurnali:</b>\n{summary}"
            else:
                reply = "Antigravity ko'prigi faol emas."
            _speak_if_allowed(reply)
            return reply

        if any(w in q for w in ["qanday vazifalar", "vazifalar nima", "vazifalar ro'yxati", "qaysi vazifalar", "topshiriqlar ro'yxati"]) or (
            "antigravity" in q and any(w in q for w in ["qanday", "nima", "qaysi", "holat", "status", "bajarilyapti", "bajarilmoqda", "ro'yxat", "qilingan"])
        ):
            if self.antigravity:
                history = self.antigravity.get_delegation_history()
                valid_tasks = [t for t in history if "qanday vazifalar" not in t.get("prompt", "").lower()]
                if not valid_tasks:
                    msg = "Hozirda Antigravity tizimida hech qanday faol vazifa mavjud emas."
                else:
                    lines = ["📋 <b>Antigravitydagi vazifalar ro'yxati va holati:</b>"]
                    for idx, t in enumerate(valid_tasks[-5:], 1):
                        status_emoji = "✅" if t.get("status") == "COMPLETED" else "⏳"
                        res_note = f"\n   • <b>Natija:</b> {t.get('result')}" if t.get("result") else ""
                        lines.append(f"{idx}. {status_emoji} <b>ID:</b> <code>{t.get('task_id')}</code>\n   • <b>Vazifa:</b> {t.get('prompt')}\n   • <b>Holat:</b> {t.get('status', 'QUEUED')} ({t.get('created_at', '')}){res_note}")
                    msg = "\n".join(lines)
                _speak_if_allowed(msg)
                return msg
            else:
                msg = "Antigravity ko'prigi faol emas."
                _speak_if_allowed(msg)
                return msg

        if any(w in q for w in ["antigravity", "dastur yoz", "kodni tekshir", "vazifa", "topshiriq"]):
            _speak_if_allowed("Vazifa Antigravity muhandislik agentiga topshirilmoqda...")
            if self.antigravity:
                task_res = self.antigravity.delegate_task(query)
                if isinstance(task_res, dict):
                    msg = task_res.get("speech_summary") or f"Vazifa Antigravity tizimiga yuborildi (ID: {task_res.get('task_id')})."
                else:
                    msg = str(task_res)
                _speak_if_allowed("Antigravity vazifani qabul qildi.")
                return msg
            else:
                msg = "Antigravity ko'prigi faol emas."
                _speak_if_allowed(msg)
                return msg

        # 16. User Telegram Contact Messaging & Unread Triage
        if any(w in q for w in [
            "o'qilmagan xabar", "oqilmagan xabar", "xabarlarni tekshir",
            "chatlar sarhisobi", "telegramdagi xabar", "unread", "digest",
            "yangi xabarlar", "xabarlarni sarhisob", "chatlarni ko'r",
            "telegramda nima gap", "telegram yangiliklar"
        ]):
            try:
                _speak_if_allowed("Telegram xabarlaringizni tahlil qilyapman, bir daqiqa...")
                from jarvis_user_telegram import JarvisUserTelegram
                summary_res = JarvisUserTelegram.generate_unread_summary(limit_dialogs=25)
                voice_text = summary_res.get_voice()
                _speak_if_allowed(voice_text)
                return summary_res.get_markdown()
            except Exception as e:
                logger.error(f"Error generating telegram unread summary: {e}")
                err_msg = f"Telegram xabarlarini tahlil qilishda xatolik: {e}"
                _speak_if_allowed(err_msg)
                return err_msg

        # 16.0 Anaphoric / Context File Forwarding (e.g. 'Endi uni xurshidga jónat')
        anaphora_res = self._handle_anaphora_forwarding(query, user_id=user_id)
        if anaphora_res:
            _speak_if_allowed(anaphora_res)
            return anaphora_res

        # 16.1 Telegram File / Document Dispatch
        file_send_res = self._handle_telegram_file_send(query)
        if file_send_res:
            _speak_if_allowed(file_send_res)
            return file_send_res

        target, msg_to_send = self._parse_contact_message(query)
        if target and msg_to_send:
            try:
                from jarvis_user_telegram import JarvisUserTelegram
                res = JarvisUserTelegram.send_telegram_message(target, msg_to_send)
                if res.get("success"):
                    reply = f"Telegram orqali {res.get('target', target)}ga xabar muvaffaqiyatli yuborildi: \"{msg_to_send}\""
                else:
                    reply = f"Telegram orqali xabar yuborishda xatolik: {res.get('error')}"
                _speak_if_allowed(reply)
                return reply
            except Exception as e:
                logger.error(f"Error handling telegram contact message: {e}")
                err_msg = f"Telegram xabarini yuborishda xatolik: {e}"
                _speak_if_allowed(err_msg)
                return err_msg

        if any(w in q for w in ["qo'ng'iroq", "qongiroq", "telefon qil", "sim qoq", "ring me", "call me"]):
            reply = (
                "Kechirasiz, Telegram avtomatlashtirilgan xizmati orqali to'g'ridan-to'g'ri audio qo'ng'iroq "
                "qilish texnik jihatdan cheklangan. Biroq men sizga har qanday eslatma yoki xabarni darhol Telegramga yetkazaman!"
            )
            _speak_if_allowed(reply)
            return reply

        return None

    # Backward compatibility alias
    process_query = process_command

    def start(self):
        """Start the entire JARVIS background system."""
        self.is_running = True
        logger.info("=== MONIKA (JARVIS) IS NOW ONLINE ===")
        self.speak("Assalomu alaykum! Men Monikaman. Sizga qanday yordam bera olaman?")

        # Start Scheduler loop if not already running
        if self.scheduler and not getattr(self.scheduler, "_is_running", False):
            self.scheduler.start()
            logger.info("Scheduler background loop verified active.")

        # Start Vision monitoring thread
        if self.vision:
            vision_thread = threading.Thread(target=self.vision.start_monitoring, daemon=True)
            vision_thread.start()
            logger.info("Vision monitoring loop started in background.")

        # Start Telegram bot thread
        if self.telegram:
            telegram_thread = threading.Thread(target=self.telegram.start_polling, daemon=True)
            telegram_thread.start()
            logger.info("Telegram polling started in background.")

        # Start Concierge Telethon listener thread
        self._start_concierge_listener()

        # Start Voice listening thread if enabled
        if self.voice:
            voice_thread = threading.Thread(target=self._voice_loop, daemon=True)
            voice_thread.start()
            logger.info("Voice listener loop started in background.")

            # Start Push-to-Talk (Ctrl+Space) global hotkey listener (Mark-LV style)
            if hasattr(self.voice, "start_push_to_talk_listener"):
                def _on_ptt():
                    logger.info("⚡ [PTT HOTKEY DETECTED]: Listening directly without wake word...")
                    heard = self.voice.listen(timeout=6, phrase_time_limit=10)
                    if heard and heard.strip():
                        self.process_command(heard)
                self.voice.start_push_to_talk_listener(_on_ptt)
                logger.info("⚡ Push-to-Talk (Ctrl+Space) global hotkey listener started.")

        try:
            while self.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Stopping JARVIS...")
            self.stop()

    @classmethod
    def _extract_credential(cls, text: str) -> Optional[str]:
        """Extract credential/PIN from user text if present, supporting := or space separator."""
        if not text:
            return None
        # Pattern 1: Explicit separator (:= or =)
        m = re.search(r'\b(?:parol|kod|pin|parolim|kodim)\b\s*[:=]\s*([a-zA-Z0-9!@#\$%\^&*_\-]{3,})', text, re.IGNORECASE)
        # Pattern 2: Space separator (including alphanumeric passwords/PINs)
        if not m:
            m = re.search(r'\b(?:parol|kod|pin|parolim|kodim)\b\s+([a-zA-Z0-9!@#\$%\^&*_\-]{3,32})\b', text, re.IGNORECASE)
        if not m:
            return None
        val = m.group(1).strip()
        non_creds = {"kirit", "kiritish", "sorash", "so'rash", "ochish", "bilan", "nima", "qanday", "berish", "yozish", "ornatish", "o'rnatish"}
        if val.lower() in non_creds:
            return None
        return val

    @classmethod
    def _redact_voice_transcript(cls, text: str) -> str:
        """Replace sensitive credential transcripts with fixed redaction message."""
        if not text:
            return ""
        if cls._extract_credential(text):
            return "[REDACTED CREDENTIAL SUBMISSION]"
        return text

    def _voice_loop(self):
        """Continuous background listening loop with strict wake-word gating and active window."""
        logger.info(f"🎙️ Voice listener loop active. Strict Wake Words: {config.WAKE_WORDS}")
        active_window_until = 0.0

        while self.is_running:
            try:
                if self.voice and hasattr(self.voice, "listen"):
                    heard_text = self.voice.listen()
                    if not heard_text or not heard_text.strip():
                        continue

                    lower_text = heard_text.lower().strip()

                    # 1. STOP command check at any time
                    if any(w in lower_text for w in ["to'xta", "toxta", "stop", "jim", "bas", "bo'ldi", "boldi", "gapirma"]):
                        logger.info("🛑 STOP voice command detected.")
                        if self.voice:
                            self.voice.stop_speaking()
                        active_window_until = 0.0
                        continue

                    now = time.time()
                    # 2. Strict Wake Word check
                    is_wake = any(w in lower_text for w in config.WAKE_WORDS)

                    if is_wake:
                        redacted = self._redact_voice_transcript(heard_text)
                        logger.info(f"✨ Wake word recognized: '{redacted}'")
                        cmd_text = heard_text
                        for w in config.WAKE_WORDS:
                            if lower_text.startswith(w):
                                cmd_text = heard_text[len(w):].strip()
                                break
                            idx = lower_text.find(w)
                            if idx >= 0:
                                cmd_text = (heard_text[:idx] + heard_text[idx+len(w):]).strip()
                                break

                        if not cmd_text or len(cmd_text) < 3:
                            self.speak("Labbay, eshitaman?")
                            active_window_until = now + 9.0
                        else:
                            active_window_until = now + 7.0
                            self.process_command(cmd_text)
                    elif now < active_window_until:
                        redacted = self._redact_voice_transcript(heard_text)
                        logger.info(f"💬 Active window speech: '{redacted}'")
                        active_window_until = now + 7.0
                        self.process_command(heard_text)
                    else:
                        redacted = self._redact_voice_transcript(heard_text)
                        logger.debug(f"[VOICE IGNORED - NO WAKE WORD]: '{redacted}'")
            except Exception as e:
                logger.error(f"Voice loop error: {e}")
                time.sleep(0.5)

    def _start_concierge_listener(self):
        """Start the background Telethon listener for Monika AI Concierge (Kotiba Rejimi)."""
        self._concierge_thread = threading.Thread(
            target=self._concierge_telethon_worker,
            name="JARVIS-ConciergeListener",
            daemon=True
        )
        self._concierge_thread.start()
        logger.info("🛡️ Monika AI Concierge Telethon background listener thread spawned.")

    def _concierge_telethon_worker(self):
        """Worker thread executing Telethon event loop for incoming 1-on-1 private messages."""
        logger.info("🛡️ Initializing Telethon Concierge event listener...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._concierge_loop = loop

        async def _runner():
            try:
                from telegram_client import get_client
                from telethon import events
                from jarvis_concierge import JarvisConcierge
                from jarvis_user_telegram import JarvisUserTelegram
            except Exception as e:
                logger.warning(f"Concierge Telethon prerequisites unavailable: {e}")
                return

            concierge = JarvisConcierge.get_instance()
            max_reconnect_attempts = 10
            attempt = 0

            while self.is_running and attempt < max_reconnect_attempts:
                client = None
                try:
                    attempt += 1
                    client = get_client()
                    await client.connect()
                    if not self.is_running:
                        await client.disconnect()
                        break

                    if not await client.is_user_authorized():
                        logger.warning("Telethon client not authorized. Concierge listener cannot start.")
                        await client.disconnect()
                        break

                    if not self.is_running:
                        await client.disconnect()
                        break

                    # Expose shared client to JarvisUserTelegram
                    if hasattr(JarvisUserTelegram, "set_shared_client"):
                        JarvisUserTelegram.set_shared_client(client, loop)

                    self._concierge_client = client
                    # Reset reconnect counter on successful connect
                    attempt = 0

                    # Listen to all private messages (both incoming from contacts AND outgoing from Azizbek)
                    @client.on(events.NewMessage(func=lambda e: e.is_private))
                    async def _on_private_message(event):
                        try:
                            # Forward event to Concierge state machine
                            await concierge.handle_telethon_event(
                                client=client,
                                event=event,
                                bot_instance=self.telegram
                            )
                        except Exception as e:
                            logger.error(f"Error handling concierge telethon message: {e}", exc_info=True)

                    # Also listen to ANY outgoing message sent by Azizbek across Telegram to track active presence
                    @client.on(events.NewMessage(outgoing=True))
                    async def _on_user_outgoing(event):
                        try:
                            cid = getattr(event, "chat_id", None) or getattr(event, "to_id", None)
                            if cid:
                                concierge.record_user_outgoing(cid)
                        except Exception as e:
                            logger.debug(f"Error recording user outgoing message: {e}")

                    logger.info("🛡️ [KOTIBA REJIMI] Telethon live listener ONLINE and watching private chats.")
                    await client.run_until_disconnected()

                except asyncio.CancelledError:
                    break
                except Exception as ex:
                    logger.warning(f"Concierge Telethon listener loop exception (attempt {attempt}): {ex}")
                    await asyncio.sleep(5.0)
                finally:
                    self._concierge_client = None
                    if hasattr(JarvisUserTelegram, "clear_shared_client"):
                        JarvisUserTelegram.clear_shared_client()
                    if client and client.is_connected():
                        try:
                            await client.disconnect()
                        except Exception:
                            pass

        try:
            loop.run_until_complete(_runner())
        except Exception as e:
            logger.error(f"Concierge Telethon event loop terminated: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def stop(self):
        self.is_running = False
        if hasattr(self, "_stop_event") and self._stop_event:
            self._stop_event.set()

        # Disconnect concierge listener and client cleanly
        if hasattr(self, "_concierge_loop") and self._concierge_loop and self._concierge_loop.is_running():
            try:
                if hasattr(self, "_concierge_client") and self._concierge_client:
                    asyncio.run_coroutine_threadsafe(self._concierge_client.disconnect(), self._concierge_loop)
                for task in asyncio.all_tasks(self._concierge_loop):
                    self._concierge_loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass

        if hasattr(self, "_concierge_thread") and self._concierge_thread:
            try:
                self._concierge_thread.join(timeout=3.0)
            except Exception:
                pass

        if hasattr(self, "_heartbeat_thread") and self._heartbeat_thread:
            try:
                self._heartbeat_thread.join(timeout=2.0)
            except Exception:
                pass

        if self.voice and hasattr(self.voice, "stop"):
            self.voice.stop()
        if self.vision:
            self.vision.stop()
        if self.telegram:
            self.telegram.stop()
        if self.scheduler:
            self.scheduler.stop()
        logger.info("JARVIS shut down safely.")


if __name__ == "__main__":
    jarvis = JarvisCore()
    jarvis.start()
