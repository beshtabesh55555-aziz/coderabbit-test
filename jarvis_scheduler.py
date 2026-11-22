"""
JARVIS Background Scheduler Module
==================================
Manages time-based reminders and scheduled background alerts for JARVIS.

Features:
- Relative time reminders (e.g., in 30 seconds, 5 minutes, 2 hours).
- Absolute time reminders (e.g., at 14:30, 2026-11-16 09:00:00).
- Natural text parsing for Uzbek, English, and standard time formats.
- Persistent JSON storage in reminders.json (survives restarts).
- Thread-safe daemon background worker (checks every 2-5s).
- Dispatches alerts to registered callbacks and Telegram bot (if configured).
- Clean API: add_reminder, list_reminders, cancel_reminder, start, stop.
"""

import os
import sys
import time
import json
import uuid
import re
import html
import logging
import threading
import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Union

# Base configuration paths
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STORAGE_FILE = BASE_DIR / "reminders.json"

# Logging setup
logger = logging.getLogger("JarvisScheduler")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-SCHEDULER] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Regex patterns for natural language time parsing
# Relative units: seconds, minutes, hours, days (Uzbek and English)
_RELATIVE_REGEX = re.compile(
    r'(?i)\b(?P<val>\d+(?:\.\d+)?)\s*'
    r'(?P<unit>soniya(?:dan\s+keyin|dan|keyin)?|sekund(?:dan\s+keyin|dan|keyin)?|sek|second(?:s)?|sec(?:s)?|s|'
    r'daqiqa(?:dan\s+keyin|dan|keyin)?|daq|minut(?:dan\s+keyin|dan|keyin)?|min(?:s)?|minute(?:s)?|m|'
    r'soat(?:dan\s+keyin|dan|keyin)?|hour(?:s)?|hr(?:s)?|h|'
    r'kun(?:dan\s+keyin|dan|keyin)?|day(?:s)?|d)\b'
)

# Absolute time pattern: [ertaga|tomorrow] [soat|at] HH:MM[:SS] [da|ga]
_ABSOLUTE_TIME_REGEX = re.compile(
    r'(?i)\b(?:(?P<tomorrow>ertaga|tomorrow)\s+)?'
    r'(?:soat\s+|at\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?'
    r'(?:\s*(?P<suffix>da|ga|da\s+bo\'lib|da\s+bo\'ladigan))?\b'
)

# ISO / Full datetime regex: YYYY-MM-DD HH:MM[:SS] or DD.MM.YYYY HH:MM[:SS]
_FULL_DATETIME_REGEX = re.compile(
    r'\b(?P<dt>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?|\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?)\b'
)


def _normalize_time_unit(unit: str) -> str:
    """Classify and normalize time unit token to a standard unit category."""
    unit = unit.lower().strip()
    if unit in ("s", "sec", "secs", "second", "seconds", "sek", "sekund", "soniya") or unit.startswith(("soniya", "sekund")):
        return "seconds"
    if unit in ("m", "min", "mins", "minut", "minute", "minutes", "daq", "daqiqa") or unit.startswith(("daqiqa", "minut")):
        return "minutes"
    if unit in ("h", "hr", "hrs", "hour", "hours", "soat") or unit.startswith("soat"):
        return "hours"
    if unit in ("d", "day", "days", "kun") or unit.startswith("kun"):
        return "days"
    return "unknown"


def _clean_reminder_message(text: str) -> str:
    """Helper to clean common reminder trigger boilerplate words from the text."""
    # Remove leading trigger prefixes
    text = re.sub(
        r'(?i)^(eslatma[:\s]*|eslat[:\s]*|eslatib\s+qo\'y[:\s]*|remind\s+me\s+to[:\s]*|remind\s+me[:\s]*|reminder[:\s]*)',
        '',
        text.strip()
    )
    # Remove conjunctions and prepositions
    text = re.sub(r'(?i)\b(keyin|in|dan|ichida|so\'ng|at|da|ga)\b', '', text)
    # Remove trailing reminder request verbs
    text = re.sub(
        r'(?i)\s*(ni\s+eslat|eslatib\s+qo\'y|eslatgin|eslat|qilishni\s+eslat|haqida\s+eslat|eslatishingizni\s+so\'rayman)\s*$',
        '',
        text.strip()
    )
    # Clean whitespace and punctuation
    text = re.sub(r'\s+', ' ', text).strip(" ,.:;-!?'\"")
    return text


def parse_reminder_text(raw_text: str, now: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Parse a natural language text for reminder intentions and time specifications.
    
    Supports:
    - Uzbek: "5 daqiqadan keyin dori ichishni eslat", "10 minut", "1 soatdan keyin yig'ilish",
             "soat 14:30 da do'konga borish", "ertaga soat 10:00 da hisobot"
    - English: "in 5 minutes call John", "30 seconds test", "tomorrow at 15:00 meeting"
    - Standard datetime formats: "2026-11-16 14:30:00"
    
    Returns:
        dict with keys:
            'text': cleaned reminder text
            'delay_seconds': total seconds from now (int)
            'target_time': formatted string 'YYYY-MM-DD HH:MM:SS'
            'is_relative': bool
        or None if no time specification was recognized.
    """
    if not raw_text or not raw_text.strip():
        return None
        
    now = now or datetime.datetime.now()
    text = raw_text.strip()
    
    # 1. Full datetime check (e.g. 2026-11-16 14:30:00 or 16.11.2026 14:30)
    dt_match = _FULL_DATETIME_REGEX.search(text)
    if dt_match:
        dt_str = dt_match.group("dt").replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                target_dt = datetime.datetime.strptime(dt_str, fmt)
                delay_sec = max(1, int((target_dt - now).total_seconds()))
                clean_text = text[:dt_match.start()] + text[dt_match.end():]
                clean_text = _clean_reminder_message(clean_text)
                return {
                    "text": clean_text or "Eslatma",
                    "delay_seconds": delay_sec,
                    "target_time": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "is_relative": False
                }
            except ValueError:
                pass

    # 2. Absolute time of day check (e.g. "ertaga 14:30 da", "soat 18:00 da", "at 09:15")
    abs_match = _ABSOLUTE_TIME_REGEX.search(text)
    if abs_match:
        is_tomorrow = bool(abs_match.group("tomorrow"))
        hr = int(abs_match.group("hour"))
        mn = int(abs_match.group("minute"))
        sc = int(abs_match.group("second") or 0)
        
        if 0 <= hr <= 23 and 0 <= mn <= 59 and 0 <= sc <= 59:
            target_dt = now.replace(hour=hr, minute=mn, second=sc, microsecond=0)
            if is_tomorrow or target_dt <= now:
                target_dt += datetime.timedelta(days=1)
                
            delay_sec = max(1, int((target_dt - now).total_seconds()))
            clean_text = text[:abs_match.start()] + text[abs_match.end():]
            clean_text = _clean_reminder_message(clean_text)
            return {
                "text": clean_text or "Eslatma",
                "delay_seconds": delay_sec,
                "target_time": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "is_relative": False
            }

    # 3. Relative time check (e.g. "5 daqiqa", "10 minut", "1 soat 30 daqiqa", "in 30 seconds")
    rel_matches = list(_RELATIVE_REGEX.finditer(text))
    if rel_matches:
        total_seconds = 0.0
        spans = []
        for m in rel_matches:
            val = float(m.group("val"))
            unit = m.group("unit").lower()
            
            cat = _normalize_time_unit(unit)
            if cat == "seconds":
                total_seconds += val
            elif cat == "minutes":
                total_seconds += val * 60
            elif cat == "hours":
                total_seconds += val * 3600
            elif cat == "days":
                total_seconds += val * 86400
            spans.append(m.span())
            
        clean_text = text
        for start, end in reversed(spans):
            clean_text = clean_text[:start] + " " + clean_text[end:]
            
        clean_text = _clean_reminder_message(clean_text)
        delay_sec = max(1, int(round(total_seconds)))
        target_dt = now + datetime.timedelta(seconds=delay_sec)
        return {
            "text": clean_text or "Eslatma",
            "delay_seconds": delay_sec,
            "target_time": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "is_relative": True
        }

    return None


def parse_target_time(target_time_str: str, now: Optional[datetime.datetime] = None) -> datetime.datetime:
    """
    Parse an absolute or relative target_time string into a future datetime.datetime.
    Raises ValueError if parsing fails.
    """
    now = now or datetime.datetime.now()
    s = str(target_time_str).strip()
    
    # 1. Full datetime parsing
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M"
    ):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass

    # 2. Time-only parsing (HH:MM or HH:MM:SS)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.datetime.strptime(s, fmt).time()
            dt = datetime.datetime.combine(now.date(), t)
            if dt <= now:
                dt += datetime.timedelta(days=1)
            return dt
        except ValueError:
            pass

    # 3. Natural language check for relative phrases (e.g. "5m", "10 daqiqa", "2 hours")
    natural_res = parse_reminder_text(s, now=now)
    if natural_res and natural_res.get("target_time"):
        return datetime.datetime.strptime(natural_res["target_time"], "%Y-%m-%d %H:%M:%S")

    raise ValueError(f"Could not parse target_time string: '{target_time_str}'")


class JarvisScheduler:
    """
    Thread-safe Background Scheduler for JARVIS.
    Stores reminders persistently in JSON, runs a daemon check loop,
    and dispatches callbacks and alerts when due.
    """

    def __init__(
        self,
        storage_file: Optional[Union[str, Path]] = None,
        check_interval: float = 2.0,
        auto_start: bool = True
    ):
        self.storage_file = Path(storage_file or DEFAULT_STORAGE_FILE)
        self.check_interval = max(0.5, float(check_interval))
        self._lock = threading.RLock()
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._reminders: List[Dict[str, Any]] = []
        
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False

        # Load existing reminders from storage
        self._load_reminders()

        if auto_start:
            self.start()

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_reminders(self):
        """Load reminders from JSON storage file safely."""
        with self._lock:
            if not self.storage_file.exists():
                self._reminders = []
                return

            try:
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                if isinstance(data, list):
                    self._reminders = data
                elif isinstance(data, dict):
                    self._reminders = data.get("reminders", [])
                else:
                    self._reminders = []
                logger.info(f"Loaded {len(self._reminders)} reminders from {self.storage_file.name}")
            except Exception as e:
                logger.error(f"Failed to read reminders file ({e}). Creating backup.")
                try:
                    bak_file = self.storage_file.with_suffix(".json.bak")
                    if self.storage_file.exists():
                        self.storage_file.rename(bak_file)
                except Exception:
                    pass
                self._reminders = []

    def _save_reminders_locked(self):
        """Save reminders to JSON storage file atomically. Assumes self._lock is acquired."""
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Prune completed or cancelled reminders if there are too many (keep last 50 completed/cancelled)
            pending = [r for r in self._reminders if r.get("status") == "pending"]
            completed = [r for r in self._reminders if r.get("status") != "pending"]
            if len(completed) > 50:
                completed = completed[-50:]
            self._reminders = pending + completed

            payload = {
                "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_pending": len(pending),
                "reminders": self._reminders
            }

            temp_file = self.storage_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            
            # Atomic replace
            os.replace(temp_file, self.storage_file)
        except Exception as e:
            logger.error(f"Error persisting reminders to {self.storage_file}: {e}")

    # =========================================================================
    # Public Reminder Management API
    # =========================================================================

    def add_reminder(
        self,
        text: str,
        delay_seconds: Optional[Union[int, float]] = None,
        target_time: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Add a new scheduled reminder.
        
        Args:
            text: Reminder description or natural language query (e.g. "5 daqiqadan keyin dori ichish")
            delay_seconds: Optional delay in seconds relative to current time
            target_time: Optional target time string (e.g. "14:30", "2026-11-16 09:00:00")
            
        Returns:
            Dict representing the created reminder record.
        """
        now = datetime.datetime.now()
        clean_text = text.strip() if text else "Eslatma"
        delay_sec = 0

        # Scenario 1: explicit delay_seconds provided
        if delay_seconds is not None:
            delay_sec = max(0, int(round(float(delay_seconds))))
            target_dt = now + datetime.timedelta(seconds=delay_sec)
        
        # Scenario 2: explicit target_time provided
        elif target_time is not None:
            target_dt = parse_target_time(target_time, now=now)
            delay_sec = max(0, int((target_dt - now).total_seconds()))

        # Scenario 3: Neither provided -> parse natural language string in `text`
        else:
            parsed = parse_reminder_text(text, now=now)
            if not parsed:
                raise ValueError(
                    f"No time specification found in text '{text}'. "
                    "Please specify delay_seconds or target_time."
                )
            target_dt = datetime.datetime.strptime(parsed["target_time"], "%Y-%m-%d %H:%M:%S")
            delay_sec = parsed["delay_seconds"]
            clean_text = parsed["text"]

        reminder_id = f"rem_{int(now.timestamp())}_{uuid.uuid4().hex[:6]}"
        reminder_record = {
            "id": reminder_id,
            "text": clean_text or "Eslatma",
            "raw_input": text,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger_at": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger_timestamp": target_dt.timestamp(),
            "delay_seconds": delay_sec,
            "status": "pending"
        }

        with self._lock:
            self._reminders.append(reminder_record)
            self._save_reminders_locked()

        logger.info(
            f"Scheduled reminder [{reminder_id}]: '{reminder_record['text']}' "
            f"for {reminder_record['trigger_at']} (in {delay_sec}s)"
        )
        return dict(reminder_record)

    def schedule_reminder(
        self,
        reminder_data: Union[Dict[str, Any], str],
        delay_seconds: Optional[Union[int, float]] = None
    ) -> Dict[str, Any]:
        """Convenience method accepting either a reminder dict or text to schedule."""
        if isinstance(reminder_data, dict):
            text = reminder_data.get("text", "Eslatma")
            delay = reminder_data.get("delay_seconds")
            if delay is None and reminder_data.get("delay_minutes"):
                delay = int(reminder_data["delay_minutes"]) * 60
            target_time = reminder_data.get("trigger_time") or reminder_data.get("target_time")
            return self.add_reminder(text=text, delay_seconds=delay, target_time=target_time)
        return self.add_reminder(text=str(reminder_data), delay_seconds=delay_seconds)

    def cancel_reminder(self, reminder_id: str) -> bool:
        """
        Cancel a pending reminder by ID.
        
        Returns:
            True if reminder was found and cancelled; False otherwise.
        """
        with self._lock:
            for rem in self._reminders:
                if rem.get("id") == reminder_id and rem.get("status") == "pending":
                    rem["status"] = "cancelled"
                    rem["cancelled_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._save_reminders_locked()
                    logger.info(f"Cancelled reminder: {reminder_id}")
                    return True
        return False

    def list_reminders(self, status: Optional[str] = "pending") -> List[Dict[str, Any]]:
        """
        List reminders. By default returns pending reminders.
        
        Args:
            status: 'pending', 'completed', 'cancelled', or None / 'all' for all reminders.
        """
        with self._lock:
            if status is None or status.lower() == "all":
                return [dict(r) for r in self._reminders]
            return [dict(r) for r in self._reminders if r.get("status") == status]

    # =========================================================================
    # Callbacks & Alert Handling
    # =========================================================================

    def register_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Register an alert callback function to be called when any reminder triggers."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Unregister an alert callback function."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def _dispatch_telegram_alert_async(self, reminder: Dict[str, Any]):
        """Dispatch Telegram notification asynchronously in background thread to avoid blocking callbacks."""
        def _worker():
            try:
                import requests
                from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID, is_telegram_configured
                if is_telegram_configured():
                    text = reminder.get("text", "")
                    trigger_at = reminder.get("trigger_at", "")
                    msg = (
                        f"⏰ <b>JARVIS ESLATMASI</b>\n\n"
                        f"📌 <b>Vazifa:</b> {html.escape(text)}\n"
                        f"⏱ <b>Vaqti:</b> {trigger_at}"
                    )
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(
                        url,
                        json={
                            "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                            "text": msg,
                            "parse_mode": "HTML"
                        },
                        timeout=5
                    )
            except Exception as e:
                logger.debug(f"Telegram reminder alert dispatch failed or unconfigured: {e}")

        t = threading.Thread(target=_worker, name="JarvisSchedulerTelegramAlert", daemon=True)
        t.start()

    def _trigger_callbacks(self, reminder: Dict[str, Any]):
        """Trigger registered callbacks and default alert handler outside lock."""
        text = reminder.get("text", "")
        rem_id = reminder.get("id", "")
        trigger_at = reminder.get("trigger_at", "")
        logger.info(f"⏰ [ALERT] Reminder due ({rem_id}): {text} (Scheduled: {trigger_at})")

        # 1. Registered callbacks execute immediately
        with self._lock:
            callbacks_snapshot = list(self._callbacks)

        for cb in callbacks_snapshot:
            try:
                cb(reminder)
            except Exception as err:
                logger.error(f"Error executing reminder callback {cb}: {err}", exc_info=True)

        # 2. Async Telegram notification
        self._dispatch_telegram_alert_async(reminder)

    # =========================================================================
    # Background Worker Thread
    # =========================================================================

    def _check_due_reminders(self):
        """Examine pending reminders and trigger due ones."""
        now = time.time()
        due_reminders: List[Dict[str, Any]] = []

        with self._lock:
            for rem in self._reminders:
                if rem.get("status") == "pending":
                    trigger_ts = rem.get("trigger_timestamp", 0)
                    if trigger_ts <= now:
                        rem["status"] = "completed"
                        rem["completed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        due_reminders.append(dict(rem))

            if due_reminders:
                self._save_reminders_locked()

        # Trigger callbacks OUTSIDE the lock to avoid blocking other operations
        for rem in due_reminders:
            self._trigger_callbacks(rem)

    def _run_loop(self):
        """Worker thread loop checking every check_interval seconds."""
        logger.info("JarvisScheduler daemon worker started.")
        while not self._stop_event.is_set():
            try:
                self._check_due_reminders()
            except Exception as e:
                logger.error(f"Unexpected error in scheduler loop: {e}", exc_info=True)
            self._stop_event.wait(self.check_interval)
        logger.info("JarvisScheduler daemon worker stopped.")

    def start(self):
        """Start the background reminder monitoring worker thread."""
        with self._lock:
            if self._is_running and self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._run_loop,
                name="JarvisSchedulerWorker",
                daemon=True
            )
            self._worker_thread.start()
            self._is_running = True
            logger.info("JarvisScheduler started successfully.")

    def stop(self, timeout: float = 3.0):
        """Stop the background reminder monitoring worker thread."""
        with self._lock:
            if not self._is_running:
                return
            self._stop_event.set()
            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=timeout)
            self._is_running = False
            logger.info("JarvisScheduler stopped.")


# =============================================================================
# Global Singleton & Module-Level Convenience Functions
# =============================================================================

_global_scheduler: Optional[JarvisScheduler] = None
_global_lock = threading.Lock()


def get_scheduler() -> JarvisScheduler:
    """Retrieve or initialize the global JarvisScheduler instance."""
    global _global_scheduler
    with _global_lock:
        if _global_scheduler is None:
            _global_scheduler = JarvisScheduler()
        return _global_scheduler


def add_reminder(
    text: str,
    delay_seconds: Optional[Union[int, float]] = None,
    target_time: Optional[str] = None
) -> Dict[str, Any]:
    """Module-level helper to schedule a reminder using the global scheduler."""
    return get_scheduler().add_reminder(text=text, delay_seconds=delay_seconds, target_time=target_time)


def list_reminders(status: Optional[str] = "pending") -> List[Dict[str, Any]]:
    """Module-level helper to list reminders from the global scheduler."""
    return get_scheduler().list_reminders(status=status)


def cancel_reminder(reminder_id: str) -> bool:
    """Module-level helper to cancel a reminder using the global scheduler."""
    return get_scheduler().cancel_reminder(reminder_id)


def start():
    """Module-level helper to start the global scheduler."""
    get_scheduler().start()


def stop():
    """Module-level helper to stop the global scheduler."""
    if _global_scheduler is not None:
        _global_scheduler.stop()


# =============================================================================
# Standalone Self-Test Block
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("JARVIS SCHEDULER SELF-TEST SUITE")
    print("=" * 60)

    # 1. Natural Language Parser Tests
    print("\n[Test 1] Testing Natural Language Text Parser...")
    test_cases = [
        ("5 daqiqadan keyin dori ichishni eslat", 300, "dori ichish"),
        ("10 minut choy damlash", 600, "choy damlash"),
        ("1 soat 30 daqiqa yig'ilish", 5400, "yig'ilish"),
        ("30 soniya tezkor test", 30, "tezkor test"),
        ("ertaga 14:30 da shifokor qabuli", None, "shifokor qabuli"),
        ("soat 18:00 da kitob o'qish", None, "kitob o'qish"),
        ("in 15 minutes call team", 900, "call team"),
        ("2026-11-20 10:00 muhim loyiha", None, "muhim loyiha")
    ]
    for raw, expected_sec, expected_msg in test_cases:
        res = parse_reminder_text(raw)
        assert res is not None, f"Failed to parse: {raw}"
        if expected_sec is not None:
            assert res["delay_seconds"] == expected_sec, f"Expected {expected_sec}s, got {res['delay_seconds']}s"
        assert expected_msg in res["text"], f"Expected '{expected_msg}' in '{res['text']}'"
        print(f"  ✓ '{raw}' -> delay: {res['delay_seconds']}s, text: '{res['text']}', target: {res['target_time']}")

    # 2. Live Scheduling & Callback Verification
    print("\n[Test 2] Testing Live Reminder Scheduling & Callback Dispatch...")
    test_storage = BASE_DIR / "test_reminders.json"
    if test_storage.exists():
        test_storage.unlink()

    scheduler = JarvisScheduler(storage_file=test_storage, check_interval=0.5, auto_start=False)
    
    callback_fired = threading.Event()
    received_reminders = []

    def on_reminder_alert(rem: Dict[str, Any]):
        print(f"\n  🔔 Callback triggered! Received reminder: ID={rem.get('id')}, Text='{rem.get('text')}'")
        received_reminders.append(rem)
        callback_fired.set()

    scheduler.register_callback(on_reminder_alert)
    scheduler.start()

    # Schedule a quick 2-second test reminder
    print("  Scheduling 2-second test reminder...")
    rem1 = scheduler.add_reminder("Tezkor 2 soniyalik test vazifasi", delay_seconds=2)
    print(f"  Created reminder: {rem1['id']} (trigger_at: {rem1['trigger_at']})")

    # Verify it is listed in pending
    pending = scheduler.list_reminders()
    assert any(r["id"] == rem1["id"] for r in pending), "Reminder not found in pending list!"
    print(f"  Pending count: {len(pending)}")

    # Wait for callback to trigger
    print("  Waiting up to 4 seconds for reminder to trigger...")
    fired = callback_fired.wait(timeout=4.0)
    assert fired, "Alert callback failed to trigger within 4 seconds!"
    assert len(received_reminders) == 1, f"Expected 1 callback event, got {len(received_reminders)}"
    assert received_reminders[0]["id"] == rem1["id"]

    # Verify reminder is marked completed
    pending_after = scheduler.list_reminders()
    assert not any(r["id"] == rem1["id"] for r in pending_after), "Reminder still pending after trigger!"
    all_rems = scheduler.list_reminders(status="completed")
    assert any(r["id"] == rem1["id"] for r in all_rems), "Reminder not marked completed!"
    print("  ✓ Reminder successfully triggered and transitioned to 'completed' status.")

    # 3. Persistence & Cancellation Test
    print("\n[Test 3] Testing Persistence & Cancellation...")
    rem2 = scheduler.add_reminder("Bekor qilinadigan vazifa", delay_seconds=60)
    assert any(r["id"] == rem2["id"] for r in scheduler.list_reminders())
    cancelled = scheduler.cancel_reminder(rem2["id"])
    assert cancelled is True, "Failed to cancel reminder!"
    assert not any(r["id"] == rem2["id"] for r in scheduler.list_reminders()), "Cancelled reminder still in pending!"
    print(f"  ✓ Cancelled reminder {rem2['id']} successfully.")

    # Stop scheduler and test clean reload from storage file
    scheduler.stop()
    print("  Reloading scheduler from persisted storage...")
    scheduler_reloaded = JarvisScheduler(storage_file=test_storage, check_interval=0.5, auto_start=False)
    reloaded_completed = scheduler_reloaded.list_reminders(status="completed")
    reloaded_cancelled = scheduler_reloaded.list_reminders(status="cancelled")
    assert any(r["id"] == rem1["id"] for r in reloaded_completed), "Completed reminder was not persisted!"
    assert any(r["id"] == rem2["id"] for r in reloaded_cancelled), "Cancelled reminder was not persisted!"
    print("  ✓ Storage persistence verified successfully across restarts.")

    # Clean up test storage file
    if test_storage.exists():
        test_storage.unlink()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED SUCCESSFULLY! JarvisScheduler is ready for use.")
    print("=" * 60)
