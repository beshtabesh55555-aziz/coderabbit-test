"""
JARVIS Telegram Gateway Module
==============================
Remote surveillance, security alerts, and command control via Telegram Bot.

Security & Hardening Features:
- Strict Fail-Closed authentication (rejects all commands if admin_chat_id is unset or mismatched).
- Security State Machine: ARM / DISARM / SNOOZE with auto-rearming.
- Asynchronous non-blocking alert queue (queue.Queue + background worker thread).
- Alert rate limiting (minimum 30 seconds interval between photo alerts).
- HTML entity sanitization with html.escape to prevent Telegram parse crashes.
- Whitelist-only command dispatch (/status, /lock, /photo, /arm, /disarm, /snooze, /say).
- Elimination of raw PowerShell /cmd execution.
"""

import os
import sys
import re
import json
import time
import html
import queue
import logging
import threading
import collections
import requests
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    import config
    TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
    TELEGRAM_ADMIN_CHAT_ID = config.TELEGRAM_ADMIN_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

logger = logging.getLogger("JarvisTelegram")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-TELEGRAM] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class JarvisTelegram:
    """Telegram Remote Gateway for JARVIS surveillance and remote control."""

    ALERT_MIN_INTERVAL: float = 30.0  # Minimum 30 seconds between photo alerts

    def __init__(
        self,
        token: Optional[str] = None,
        admin_chat_id: Optional[str] = None,
        core_ref: Any = None,
        bot_token: Optional[str] = None,
        system_controller: Any = None,
    ):
        """
        Initialize the hardened JARVIS Telegram Gateway.
        Supports both token/bot_token and core_ref/system_controller parameter conventions.
        """
        raw_token = token if token is not None else (bot_token if bot_token is not None else TELEGRAM_BOT_TOKEN)
        self.token = str(raw_token or "").strip()

        raw_admin = admin_chat_id if admin_chat_id is not None else TELEGRAM_ADMIN_CHAT_ID
        self.admin_chat_id = str(raw_admin or "").strip()

        self.core = core_ref
        self.system = system_controller
        if self.core and hasattr(self.core, "system") and not self.system:
            self.system = self.core.system

        self.is_running = False
        self._last_update_id = 0
        self._seen_update_ids: collections.deque = collections.deque(maxlen=1000)
        self._seen_update_lock = threading.Lock()
        self.base_url = f"https://api.telegram.org/bot{self.token}"

        # Security state machine: "ARMED", "DISARMED", "SNOOZED"
        self.security_state: str = "ARMED"
        self.snooze_until: float = 0.0

        # Rate limiting state
        self._last_alert_photo_time: float = 0.0
        self._state_lock = threading.Lock()

        # Asynchronous Alert Queue & Worker Thread
        self._alert_queue: queue.Queue = queue.Queue(maxsize=50)
        self._alert_worker_thread = threading.Thread(target=self._alert_worker, name="JARVIS-TG-AlertWorker", daemon=True)
        self._alert_worker_thread.start()

        # Polling thread handle
        self._polling_thread: Optional[threading.Thread] = None

        if not self.token:
            logger.warning("Telegram Bot Token is empty. Remote gateway disabled.")
        elif not self.admin_chat_id:
            logger.warning("TELEGRAM_ADMIN_CHAT_ID is empty! Strict Fail-Closed mode ACTIVE: all commands will be rejected.")
        else:
            logger.info(f"Telegram Gateway initialized. Admin Chat ID: {self.admin_chat_id} (Security Mode: {self.security_state})")

    def get_webapp_url(self) -> str:
        """Dynamically resolve the active Telegram Mini App URL from tunnel supervisor."""
        base_dir = Path(__file__).resolve().parent
        url_file = base_dir / "tunnel_url.txt"
        resolved_url = "https://0787a31ab9541a.app.lhr.life/"
        if url_file.exists():
            try:
                url = url_file.read_text(encoding="utf-8").strip()
                if url.startswith("https://"):
                    resolved_url = url
            except Exception:
                pass
        if "?" not in resolved_url:
            resolved_url = f"{resolved_url.rstrip('/')}/?v=3.0.0"
        return resolved_url

    # =========================================================================
    # 1. Authentication & Security State
    # =========================================================================

    def is_admin(self, chat_id: Any) -> bool:
        """
        Strict Fail-Closed authentication check.
        Rejects all requests if admin_chat_id is unset, empty, or mismatched.
        """
        if not self.admin_chat_id:
            return False
        return str(chat_id).strip() == self.admin_chat_id

    def is_armed(self) -> bool:
        """
        Evaluate current security monitoring state.
        Auto-rearms if a temporary snooze duration has elapsed.
        """
        with self._state_lock:
            if self.security_state == "DISARMED":
                return False
            if self.security_state == "SNOOZED":
                if time.time() < self.snooze_until:
                    return False
                # Snooze expired, auto-rearm
                self.security_state = "ARMED"
                self.snooze_until = 0.0
                logger.info("Security alert snooze period expired. Auto-rearmed to ARMED.")
            return self.security_state == "ARMED"

    def get_security_mode(self) -> str:
        """Return human-readable security mode string."""
        with self._state_lock:
            if self.security_state == "SNOOZED":
                if time.time() < self.snooze_until:
                    rem = int(self.snooze_until - time.time())
                    mins, secs = divmod(rem, 60)
                    return f"SNOOZED ({mins}m {secs}s qoldi)"
                self.security_state = "ARMED"
            return self.security_state

    # =========================================================================
    # 2. Asynchronous Alert Queue Worker & Dispatcher
    # =========================================================================

    def _alert_worker(self):
        """Worker thread processing alert photos asynchronously to avoid blocking vision/core."""
        while True:
            try:
                item = self._alert_queue.get()
                if item is None:
                    break
                photo_path, caption, target_id = item
                
                # Recheck armed state and system lock state before starting upload
                if not self.is_armed():
                    logger.info("Alert worker: Security mode is no longer ARMED. Dropping queued alert.")
                    self._alert_queue.task_done()
                    continue

                if self.system and hasattr(self.system, "is_workstation_locked"):
                    try:
                        if not self.system.is_workstation_locked():
                            logger.info("Alert worker: Workstation is active/unlocked. Dropping queued alert.")
                            self._alert_queue.task_done()
                            continue
                    except Exception as lk_err:
                        logger.warning(f"Alert worker: could not verify lock state: {lk_err}")

                self._send_photo_direct(photo_path, caption, target_id)
                self._alert_queue.task_done()
            except Exception as e:
                logger.error(f"Alert queue worker exception: {e}")

    def _send_photo_direct(self, photo_path: str, caption: str, target_id: str) -> bool:
        """Internal synchronous HTTP dispatch for alert photo."""
        p = Path(photo_path)
        if not p.exists():
            logger.error(f"Alert photo not found: {photo_path}")
            return False

        if not self.token or not target_id:
            return False

        try:
            url = f"{self.base_url}/sendPhoto"
            with open(p, "rb") as f:
                files = {"photo": f}
                data = {
                    "chat_id": target_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                }
                resp = requests.post(url, data=data, files=files, timeout=25)
                if resp.status_code == 200:
                    logger.info("Alert photo dispatched successfully to Telegram.")
                    return True
                else:
                    logger.error(f"Telegram photo upload rejected ({resp.status_code}): {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"HTTP error uploading photo to Telegram: {e}")
            return False

    def send_alert_photo(self, photo_path: str, caption: str = "🚨 Harakat aniqlandi!") -> bool:
        """
        Asynchronously queue a security snapshot for dispatch to admin Telegram.
        Respects ARM / DISARM / SNOOZE state machine and minimum 30s rate limiting.
        Non-blocking.
        """
        # Double protection: Never send alert photos if user is actively working on the PC
        sys_obj = self.system or (self.core.system if self.core and hasattr(self.core, "system") else None)
        if sys_obj:
            try:
                sec_stat = sys_obj.get_security_status()
                if sec_stat == "Active (Unlocked)":
                    logger.info("Alert photo suppressed: Workstation is Active (Unlocked). User is present at desk.")
                    return False
            except Exception as e:
                logger.error(f"Error checking security status: {e}")

        # 1. State machine check
        if not self.is_armed():
            logger.info(f"Alert photo suppressed: security system is {self.get_security_mode()}.")
            return False

        # 2. Rate limit check (minimum 30 seconds interval between alert photos)
        now = time.time()
        with self._state_lock:
            if (now - self._last_alert_photo_time) < self.ALERT_MIN_INTERVAL:
                remaining = int(self.ALERT_MIN_INTERVAL - (now - self._last_alert_photo_time))
                logger.warning(f"Alert photo rate-limited ({remaining}s remaining before next allowed alert).")
                return False
            self._last_alert_photo_time = now

        # 3. Fail-Closed recipient check
        if not self.token or not self.admin_chat_id:
            logger.warning("Telegram token or admin_chat_id not configured. Alert suppressed.")
            return False

        # 4. Enqueue for background worker
        try:
            self._alert_queue.put_nowait((str(photo_path), caption, self.admin_chat_id))
            logger.info(f"Alert photo enqueued for dispatch: {photo_path}")
            return True
        except queue.Full:
            logger.error("Alert queue is full! Dropping alert photo to prevent memory exhaustion.")
            return False

    def send_motion_alert(self, photo_path: str, caption: str = "🚨 Harakat aniqlandi!") -> Dict[str, Any]:
        """Compatibility wrapper for send_alert_photo returning result dict."""
        queued = self.send_alert_photo(photo_path, caption=caption)
        return {
            "success": queued,
            "message": "Alert photo enqueued successfully" if queued else "Alert photo suppressed (rate limit, disarmed, or unconfigured)",
            "photo_path": photo_path,
        }

    def send_photo(self, photo_path: str, caption: str = "", chat_id: Optional[str] = None) -> bool:
        """Upload and send a photo directly to Telegram chat."""
        target = chat_id or self.admin_chat_id
        return self._send_photo_direct(photo_path, caption, target)

    def send_document(self, file_path: str, caption: str = "", chat_id: Optional[str] = None) -> bool:
        """Upload and send a document or file directly to Telegram chat."""
        p = Path(file_path)
        if not p.exists():
            logger.error(f"Document not found: {file_path}")
            return False
        target_id = chat_id or self.admin_chat_id
        if not self.token or not target_id:
            return False
        try:
            url = f"{self.base_url}/sendDocument"
            with open(p, "rb") as f:
                files = {"document": f}
                data = {"chat_id": target_id, "caption": caption, "parse_mode": "HTML"}
                resp = requests.post(url, data=data, files=files, timeout=45)
                if resp.status_code == 200:
                    logger.info(f"Document dispatched successfully to {target_id}: {p.name}")
                    return True
                else:
                    logger.error(f"Telegram document dispatch rejected ({resp.status_code}): {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Error uploading document to Telegram: {e}")
            return False

    def capture_photo(self, filepath: Optional[str] = None) -> str:
        """Capture a photo using the active vision subsystem or generate a fallback image."""
        if self.core and hasattr(self.core, "vision") and self.core.vision:
            res = self.core.vision.capture_snapshot(filepath=filepath)
            if res:
                return str(res)

        # Fallback using PIL if vision module is not attached
        try:
            from PIL import Image, ImageDraw
            base_dir = Path(__file__).resolve().parent
            snap_dir = base_dir / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            p = Path(filepath or (snap_dir / f"test_snap_{int(time.time())}.jpg"))
            img = Image.new("RGB", (640, 480), color=(20, 25, 35))
            d = ImageDraw.Draw(img)
            d.text((20, 20), f"JARVIS DIAGNOSTIC CAPTURE\n{time.strftime('%Y-%m-%d %H:%M:%S')}", fill=(0, 255, 255))
            img.save(str(p), format="JPEG")
            return str(p)
        except Exception as e:
            logger.error(f"Failed to generate photo capture: {e}")
            return ""

    # =========================================================================
    # 3. Message Dispatch & Sanitization
    # =========================================================================

    def send_message(self, text: str, chat_id: Optional[str] = None, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
        """
        Send an HTML-formatted message to Telegram.
        Ensures delivery is made only to authorized recipients.
        """
        target_id = chat_id or self.admin_chat_id
        if not self.token or not target_id:
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": target_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"Telegram message delivered successfully to {target_id} (chars={len(text)}).")
                return True
            elif resp.status_code == 400 and ("can't parse entities" in resp.text or "unsupported start tag" in resp.text.lower()):
                logger.warning(f"Telegram HTML parse error ({resp.text}), retrying with clean plain text fallback...")
                payload.pop("parse_mode", None)
                retry_resp = requests.post(url, json=payload, timeout=10)
                if retry_resp.status_code == 200:
                    logger.info(f"Telegram plain-text fallback delivered successfully to {target_id}.")
                    return True
                else:
                    logger.error(f"Telegram retry rejected ({retry_resp.status_code}): {retry_resp.text}")
                    return False
            else:
                logger.error(f"Telegram sendMessage rejected ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        """Acknowledge Telegram callback query to stop loading spinner on client."""
        if not self.token or not callback_query_id:
            return False
        try:
            url = f"{self.base_url}/answerCallbackQuery"
            payload = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            resp = requests.post(url, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Error answering callback query: {e}")
            return False

    def send_clarification_question(self, chat_id: str, question: str, options: List[Dict[str, str]]) -> bool:
        """Send an interactive clarification question with inline keyboard buttons."""
        keyboard = []
        for opt in options:
            btn_text = opt.get("text", opt.get("label", ""))
            cb_data = opt.get("callback_data", opt.get("value", ""))
            keyboard.append([{"text": btn_text, "callback_data": cb_data}])
        markup = {"inline_keyboard": keyboard}
        msg = (
            f"🤔 <b>Monika (Aniqlashtirish):</b>\n{html.escape(question)}\n\n"
            "<i>Quyidagi tugmalardan birini tanlang yoki javobingizni oddiy matn shaklida yozing:</i>"
        )
        return self.send_message(msg, chat_id=chat_id, reply_markup=markup)

    # =========================================================================
    # 4. Long-Polling & Secure Whitelisted Command Dispatcher
    # =========================================================================

    def _setup_bot_commands_and_menu(self):
        """Register native bot commands and set chat menu button to 'commands'."""
        try:
            commands = [
                {"command": "status", "description": "📊 Tizim holati va diagnostika"},
                {"command": "pwa", "description": "🌐 Monika PWA boshqaruv ilovasi"},
                {"command": "photo", "description": "📸 Kameradan jonli kadr olish"},
                {"command": "lock", "description": "🔒 Kompyuterni zudlik bilan qulflash"},
                {"command": "unread", "description": "📬 O'qilmagan xabarlar sarhisobi"},
                {"command": "help", "description": "ℹ️ Barcha buyruqlar va yordam"}
            ]
            requests.post(
                f"{self.base_url}/setMyCommands",
                json={"commands": commands},
                timeout=10
            )
            requests.post(
                f"{self.base_url}/setChatMenuButton",
                json={"menu_button": {"type": "commands"}},
                timeout=10
            )
            if self.admin_id:
                requests.post(
                    f"{self.base_url}/setChatMenuButton",
                    json={"chat_id": int(self.admin_id), "menu_button": {"type": "commands"}},
                    timeout=10
                )
            logger.info("Bot commands registered and chat menu button set to 'commands'.")
        except Exception as e:
            logger.warning(f"Failed to set bot commands or menu button: {e}")

    def start_polling(self):
        """Start long-polling loop in the current thread."""
        if not self.token:
            logger.warning("Telegram Bot Token is empty. Polling aborted.")
            return

        self.is_running = True
        logger.info("Starting Telegram long-polling loop (Strict Fail-Closed)...")
        self._setup_bot_commands_and_menu()

        while self.is_running:
            try:
                url = f"{self.base_url}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 15,
                }
                resp = requests.get(url, params=params, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        u_id = update.get("update_id")
                        if u_id is not None:
                            with self._seen_update_lock:
                                if u_id in self._seen_update_ids:
                                    logger.debug(f"Skipping already seen update_id: {u_id}")
                                    continue
                                self._seen_update_ids.append(u_id)
                                if u_id > self._last_update_id:
                                    self._last_update_id = u_id
                        self._handle_update(update)
                else:
                    time.sleep(2)
            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                logger.debug(f"Polling exception: {e}")
                time.sleep(3)

    def start_in_background(self) -> threading.Thread:
        """Start long-polling loop in a daemon background thread."""
        if self._polling_thread and self._polling_thread.is_alive():
            return self._polling_thread
        self._polling_thread = threading.Thread(target=self.start_polling, name="JARVIS-TG-Poller", daemon=True)
        self._polling_thread.start()
        return self._polling_thread

    def _has_media(self, message: Dict[str, Any]) -> bool:
        """Check if message contains an attachment or media file."""
        return any(k in message for k in ("document", "photo", "audio", "video", "voice", "video_note"))

    def _download_bot_file(self, file_id: str, dest_path: Path) -> bool:
        """Download file from Telegram Bot API servers using file_id."""
        try:
            get_file_url = f"{self.base_url}/getFile?file_id={file_id}"
            res = requests.get(get_file_url, timeout=15).json()
            if not res.get("ok"):
                logger.error(f"Telegram getFile returned error: {res}")
                return False
            file_path_rel = res["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path_rel}"
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(download_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Error downloading Telegram file {file_id}: {e}")
            return False

    def _handle_incoming_media(self, message: Dict[str, Any], chat_id: str):
        """Process, download, and catalog incoming media from authorized admin."""
        base_dir = Path(__file__).resolve().parent
        downloads_base = base_dir / "downloads"

        file_id = None
        file_name = None
        category = "documents"
        category_label = "📄 Hujjat / Fayl"

        if "document" in message:
            doc = message["document"]
            file_id = doc.get("file_id")
            file_name = doc.get("file_name") or f"doc_{int(time.time())}.dat"
            category = "documents"
            category_label = "📄 Hujjat / Fayl"
        elif "audio" in message:
            aud = message["audio"]
            file_id = aud.get("file_id")
            file_name = aud.get("file_name") or f"{aud.get('performer', 'artist')}_{aud.get('title', 'track')}_{int(time.time())}.mp3"
            category = "music"
            category_label = "🎵 Musiqa (Audio)"
        elif "photo" in message:
            photos = message["photo"]
            largest = photos[-1]
            file_id = largest.get("file_id")
            file_name = f"photo_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
            category = "photos"
            category_label = "🖼️ Fotosurat (Rasm)"
        elif "video" in message:
            vid = message["video"]
            file_id = vid.get("file_id")
            file_name = vid.get("file_name") or f"video_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
            category = "videos"
            category_label = "🎬 Video lavha"
        elif "voice" in message:
            voi = message["voice"]
            file_id = voi.get("file_id")
            file_name = f"voice_{time.strftime('%Y%m%d_%H%M%S')}.ogg"
            category = "voice"
            category_label = "🎙️ Ovozli xabar"

        if not file_id:
            self.send_message("⚠️ Faylni qabul qilib bo'lmadi: file_id mavjud emas.", chat_id=chat_id)
            return

        file_name = re.sub(r'[\\/*?:"<>|]', '_', file_name)
        dest_dir = downloads_base / category
        dest_path = dest_dir / file_name

        if dest_path.exists():
            dest_path = dest_dir / f"{int(time.time())}_{file_name}"

        self.send_message(f"📥 <i>{category_label} qabul qilinmoqda, bir daqiqa...</i>", chat_id=chat_id)
        ok = self._download_bot_file(file_id, dest_path)
        if ok:
            size_mb = round(dest_path.stat().st_size / (1024 * 1024), 2)
            registry_file = downloads_base / "media_registry.json"
            registry = []
            if registry_file.exists():
                try:
                    registry = json.loads(registry_file.read_text(encoding="utf-8"))
                except Exception:
                    registry = []
            entry = {
                "file_name": dest_path.name,
                "category": category,
                "path": str(dest_path),
                "size_mb": size_mb,
                "caption": message.get("caption", ""),
                "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "chat_id": chat_id
            }
            registry.append(entry)
            try:
                registry_file.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.error(f"Error saving media registry: {e}")

            msg_reply = (
                f"✅ <b>{category_label} muvaffaqiyatli saqlandi!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Fayl nomi:</b> <code>{dest_path.name}</code>\n"
                f"• <b>Hajmi:</b> {size_mb} MB\n"
                f"• <b>Katalog:</b> <code>downloads/{category}/</code>\n"
                f"• <b>To'liq manzil:</b> <code>{dest_path}</code>"
            )
            if message.get("caption"):
                msg_reply += f"\n• <b>Izoh:</b> <i>\"{html.escape(message['caption'])}\"</i>"
            self.send_message(msg_reply, chat_id=chat_id)
        else:
            self.send_message(f"⚠️ {category_label} yuklab olishda xatolik yuz berdi.", chat_id=chat_id)

    def _handle_update(self, update: Dict[str, Any]):
        """
        Process incoming update with strict Fail-Closed authentication
        and whitelisted command execution.
        """
        # Handle Callback Query (e.g. from inline clarification buttons)
        callback = update.get("callback_query")
        if callback:
            cb_id = str(callback.get("id"))
            cb_from = callback.get("from", {})
            cb_chat_id = str(cb_from.get("id"))
            cb_data = callback.get("data", "")

            if not self.is_admin(cb_chat_id):
                self.answer_callback_query(cb_id, text="Ruxsat berilmadi.")
                return

            self.answer_callback_query(cb_id, text="Tanlovingiz qabul qilindi.")
            logger.info(f"Telegram callback query from admin ({cb_chat_id}): '{cb_data}'")

            def _handle_cb():
                try:
                    if self.core and hasattr(self.core, "handle_clarification_response"):
                        reply = self.core.handle_clarification_response(cb_chat_id, cb_data)
                        if reply:
                            self.send_message(f"🤖 <b>Monika:</b>\n{reply}", chat_id=cb_chat_id)
                except Exception as err:
                    logger.error(f"Error resolving callback clarification: {err}")
                    self.send_message(f"⚠️ Xatolik yuz berdi: {html.escape(str(err))}", chat_id=cb_chat_id)

            threading.Thread(target=_handle_cb, name="JARVIS-TG-CallbackWorker", daemon=True).start()
            return

        message = update.get("message")
        if not message:
            return

        chat_id = str(message["chat"]["id"])
        raw_user_name = message.get("from", {}).get("first_name", "Foydalanuvchi")
        user_name = html.escape(str(raw_user_name))
        raw_text = (message.get("text") or message.get("caption") or "").strip()
        escaped_text = html.escape(raw_text)

        # ---------------------------------------------------------------------
        # Strict Fail-Closed Authentication Check
        # ---------------------------------------------------------------------
        if not self.is_admin(chat_id):
            logger.warning(f"Unauthorized Telegram access rejected: Chat ID={chat_id}, User={raw_user_name}")
            # Reject sender
            self.send_message(
                f"⛔ <b>Ruxsat etilmadi!</b>\n"
                f"Siz ushbu JARVIS tizimining avtorizatsiyalangan egasi emassiz.\n"
                f"Sizning Chat ID: <code>{html.escape(chat_id)}</code>",
                chat_id=chat_id,
            )
            # Notify real admin if configured
            if self.admin_chat_id:
                self.send_message(
                    f"⚠️ <b>XAVFSIZLIK OGOHLANTIRISHI: Begona foydalanuvchi ulandi!</b>\n"
                    f"• <b>Ismi:</b> {user_name}\n"
                    f"• <b>ID:</b> <code>{html.escape(chat_id)}</code>\n"
                    f"• <b>Matn:</b> <code>{escaped_text}</code>"
                )
            return

        # ---------------------------------------------------------------------
        # Inbound Media / File Ingestion Check
        # ---------------------------------------------------------------------
        if self._has_media(message):
            self._handle_incoming_media(message, chat_id)
            return

        if not raw_text:
            return

        # ---------------------------------------------------------------------
        # Whitelisted Command Processing
        # ---------------------------------------------------------------------
        logger.info(f"Authorized command from admin ({chat_id}): {raw_text}")
        parts = raw_text.split()
        if not parts:
            return
        cmd = parts[0].lower()

        if cmd in ("/start", "/help"):
            sec_mode = html.escape(self.get_security_mode())
            app_url = self.get_webapp_url()
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "🌐 Monika PWA Ilovasini Ochish",
                            "url": app_url
                        }
                    ]
                ]
            }
            help_text = (
                "<b>Monika AI — Shaxsiy Boshqaruv Markazi</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ <b>Xavfsizlik:</b> <code>{sec_mode}</code>\n\n"
                "<b>Barcha vizual boshqaruv mustaqil PWA ilovasida:</b>\n"
                "• 📊 Resurslar va jonli holat\n"
                "• 🖥️ Ekran skrinshoti va kamera\n"
                "• 🖱️ Sensor trekpad va klaviatura\n"
                "• 📁 Antigravity loyihalari va vazifalar\n"
                "• 📬 Telegram xabarlar tahlili (Triage)\n\n"
                "<b>Bot orqali to'g'ridan-to'g'ri buyruqlar:</b>\n"
                "🌐 /pwa - Monika PWA ilovasi havolasi\n"
                "📸 /photo - Kameradan jonli kadr olish\n"
                "📊 /status - Tizim va xavfsizlik diagnostikasi\n"
                "📬 /unread - Shaxsiy va guruh xabarlari sarhisobi\n"
                "🔒 /lock - Kompyuter ekranini zudlik bilan qulflash\n"
                "🛡️ /arm - Xavfsizlik nazoratini yoqish (ARMED)\n"
                "🔓 /disarm - Xavfsizlik nazoratini to'xtatish (DISARMED)\n"
                "⏰ /snooze &lt;daqiqalar&gt; - Bildirishnomalarni vaqtincha pauza qilish\n"
                "🗣 /say &lt;matn&gt; - Kompyuter dinamikida gapirish\n"
                "🛑 /stop - Monika ovozini zudlik bilan to'xtatish"
            )
            self.send_message(help_text, chat_id=chat_id, reply_markup=markup)

        elif cmd in ("/pwa", "/app", "/webapp"):
            app_url = self.get_webapp_url()
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "🌐 PWA Ilovasini Ochish",
                            "url": app_url
                        }
                    ]
                ]
            }
            msg = (
                "<b>Monika AI — PWA Boshqaruv Markazi</b>\n"
                f"🔗 Havola: {app_url}\n\n"
                "<i>Ushbu havolani brauzerda ochib, telefoningiz bosh ekraniga o'rnatib olishingiz mumkin.</i>"
            )
            self.send_message(msg, chat_id=chat_id, reply_markup=markup)

        elif cmd == "/arm":
            with self._state_lock:
                self.security_state = "ARMED"
                self.snooze_until = 0.0
            logger.info("Security state set to ARMED by admin.")
            self.send_message(
                "🛡️ <b>Xavfsizlik tizimi QUROLLANDIRILDI (ARMED).</b>\n"
                "Barcha harakat bildirishnomalari va kuzatuv faol holatda.",
                chat_id=chat_id,
            )

        elif cmd == "/disarm":
            with self._state_lock:
                self.security_state = "DISARMED"
                self.snooze_until = 0.0
            logger.info("Security state set to DISARMED by admin.")
            self.send_message(
                "🔓 <b>Xavfsizlik tizimi O'CHIRILDI (DISARMED).</b>\n"
                "Harakat aniqlansa ham ogohlantirishlar yuborilmaydi.",
                chat_id=chat_id,
            )

        elif cmd == "/snooze":
            mins = 30
            if len(parts) > 1:
                try:
                    parsed_mins = int(parts[1])
                    if 1 <= parsed_mins <= 1440:
                        mins = parsed_mins
                    else:
                        self.send_message("⚠️ Snooze vaqti 1 dan 1440 daqiqagacha bo'lishi kerak.", chat_id=chat_id)
                        return
                except ValueError:
                    self.send_message("⚠️ Noto'g'ri daqiqa formati. Masalan: <code>/snooze 30</code>", chat_id=chat_id)
                    return

            with self._state_lock:
                self.security_state = "SNOOZED"
                self.snooze_until = time.time() + (mins * 60)
            until_str = time.strftime("%H:%M:%S", time.localtime(self.snooze_until))
            logger.info(f"Security state SNOOZED for {mins} minutes until {until_str}.")
            self.send_message(
                f"⏰ <b>Xavfsizlik tizimi {mins} daqiqaga pauza qilindi (SNOOZED).</b>\n"
                f"Soat <b>{until_str}</b> gacha harakat ogohlantirishlari yuborilmaydi.",
                chat_id=chat_id,
            )

        elif cmd == "/photo":
            snap_path = self.capture_photo()
            if snap_path and os.path.exists(snap_path):
                caption = f"📸 <b>Jonli tasvir:</b>\n⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
                self._send_photo_direct(snap_path, caption=caption, target_id=chat_id)
            else:
                self.send_message("⚠️ Kameradan kadr olib bo'lmadi.", chat_id=chat_id)

        elif cmd == "/status":
            sys_obj = self.system or (self.core.system if self.core and hasattr(self.core, "system") else None)
            if sys_obj:
                stats = sys_obj.get_system_stats()
                sec_mode = html.escape(self.get_security_mode())
                msg = (
                    "💻 <b>JARVIS Tizim Holati:</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Xavfsizlik Rejimi:</b> <code>{sec_mode}</code>\n"
                    f"• <b>Ish Stoli Qulflanishi:</b> {html.escape(str(stats.get('security_status', 'Noma\'lum')))}\n"
                    f"• <b>CPU:</b> {stats.get('cpu_percent', 0)}%\n"
                    f"• <b>RAM:</b> {stats.get('ram_percent', 0)}% ({stats.get('ram_used_gb', 0)}GB / {stats.get('ram_total_gb', 0)}GB)\n"
                    f"• <b>Disk (C:):</b> {stats.get('disk_percent', 0)}% ({stats.get('disk_free_gb', 0)}GB bo'sh)\n"
                    f"• <b>Batareya:</b> {stats.get('battery_percent', 'Statsionar')}%\n"
                    f"• <b>Uptime:</b> {html.escape(str(stats.get('uptime_formatted', 'Noma\'lum')))}"
                )
                self.send_message(msg, chat_id=chat_id)
            else:
                self.send_message("📊 Tizim holati: Online (JARVIS Core Active)", chat_id=chat_id)

        elif cmd in ("/unread", "/digest", "/messages"):
            self.send_message("🔍 <i>Telegram xabarlari tahlil qilinmoqda, bir daqiqa...</i>", chat_id=chat_id)
            try:
                from jarvis_user_telegram import JarvisUserTelegram
                summary_res = JarvisUserTelegram.generate_unread_summary(limit_dialogs=25)
                self.send_message(summary_res.get_markdown(), chat_id=chat_id)
            except Exception as e:
                logger.error(f"Error handling /unread: {e}")
                self.send_message(f"⚠️ Xabarlarni tahlil qilishda xatolik: {html.escape(str(e))}", chat_id=chat_id)

        elif cmd == "/lock":
            sys_obj = self.system or (self.core.system if self.core and hasattr(self.core, "system") else None)
            if sys_obj:
                res = sys_obj.lock_workstation()
                if res.get("success"):
                    self.send_message("🔒 Kompyuter ekrani muvaffaqiyatli qulflandi.", chat_id=chat_id)
                else:
                    self.send_message(f"⚠️ Qulflashda xatolik: {html.escape(str(res.get('error', 'Noma\'lum')))}", chat_id=chat_id)
            else:
                self.send_message("⚠️ Tizim boshqaruvchisi mavjud emas.", chat_id=chat_id)

        elif cmd == "/say":
            parts_say = raw_text.split(maxsplit=1)
            if len(parts_say) > 1:
                speak_text = parts_say[1].strip()
                if self.core and hasattr(self.core, "speak"):
                    self.core.speak(speak_text)
                elif self.system and hasattr(self.system, "speak"):
                    self.system.speak(speak_text)
                escaped_spoken = html.escape(speak_text)
                self.send_message(f"🗣 Ovoz chiqarildi: <i>\"{escaped_spoken}\"</i>", chat_id=chat_id)
            else:
                self.send_message("Qaysi matnni aytish kerak? Masalan: <code>/say Salom</code>", chat_id=chat_id)

        elif cmd in ("/stop", "/mute"):
            if self.core and hasattr(self.core, "voice") and self.core.voice:
                self.core.voice.stop_speaking()
            self.send_message("🛑 <b>Monika ovozi zudlik bilan to'xtatildi.</b>", chat_id=chat_id)

        elif raw_text.startswith("/"):
            # Rejection of unwhitelisted slash commands (including raw /cmd execution)
            logger.warning(f"Rejected unwhitelisted or disabled command: '{cmd}'")
            self.send_message(
                f"⚠️ <b>Noma'lum buyruq:</b> <code>{html.escape(cmd)}</code>\n\n"
                "<b>Mavjud buyruqlar:</b>\n"
                "📸 /photo - Jonli kadr olish\n"
                "📊 /status - Tizim holati\n"
                "🔒 /lock - Ekranni qulflash\n"
                "🛡️ /arm - Nazoratni yoqish\n"
                "🔓 /disarm - Nazoratni o'chirish\n"
                "⏰ /snooze &lt;daqiqalar&gt; - Vaqtincha pauza\n"
                "🗣 /say &lt;matn&gt; - Ovoz chiqarish\n"
                "💬 <i>Yoki botga oddiy matn yozing, Monika sizga javob qaytaradi!</i>",
                chat_id=chat_id,
            )

        else:
            # Natural Conversational Chat with Monika
            logger.info(f"Conversational message from admin ({chat_id}): '{raw_text}'")
            def _handle_chat():
                try:
                    reply = ""
                    clarification_sent = False
                    media_sent = False
                    if self.core and hasattr(self.core, "process_command"):
                        # Synchronize delivery state per command execution
                        cmd_lock = getattr(self.core, "_command_lock", None) or threading.Lock()
                        with cmd_lock:
                            setattr(self.core, "_clarification_dispatched_via_tg", False)
                            setattr(self.core, "_media_dispatched_via_tg", False)
                            raw_reply = self.core.process_command(raw_text, speak_output=False, user_id=chat_id)
                            reply = str(raw_reply) if raw_reply is not None else ""
                            clarification_sent = getattr(self.core, "_clarification_dispatched_via_tg", False)
                            media_sent = getattr(self.core, "_media_dispatched_via_tg", False)
                    else:
                        reply = "Salom! Monika tizimi faol, lekin tahlil moduli ulanmagan."

                    if reply:
                        # Avoid duplicate message if clarification or direct media was already dispatched via Telegram
                        if not clarification_sent and not media_sent:
                            self.send_message(f"🤖 <b>Monika:</b>\n{reply}", chat_id=chat_id)
                except Exception as err:
                    logger.error(f"Telegram chat error: {err}")
                    self.send_message(f"⚠️ Xatolik yuz berdi: {html.escape(str(err))}", chat_id=chat_id)

            threading.Thread(target=_handle_chat, name="JARVIS-TG-ChatWorker", daemon=True).start()

    def stop(self):
        """Stop polling and cleanly terminate workers."""
        self.is_running = False
        try:
            self._alert_queue.put_nowait(None)
        except Exception:
            pass


if __name__ == "__main__":
    tg = JarvisTelegram()
    if tg.token:
        print("Starting Jarvis Telegram Bot listener (Hardened Fail-Closed)...")
        tg.start_polling()
    else:
        print("TELEGRAM_BOT_TOKEN is not set. Set it in .env to enable remote bot.")
