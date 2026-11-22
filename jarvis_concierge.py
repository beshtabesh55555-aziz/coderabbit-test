"""
JARVIS Monika AI Concierge / Auto-Responder Module
==================================================
Architected for safe, autonomous auto-response on user's personal Telegram account (via Telethon).

Core Responsibilities & State Machine:
1. Strict Filter Guardrails:
   - ONLY replies in private 1-on-1 human chats (event.is_private, not bot, not self).
   - Never responds to groups, supergroups, channels, or bots.
   - Activates ONLY when Away mode is active:
     * Windows workstation locked (detected via JarvisSystem.get_security_status()) OR
     * Manual toggle flag is enabled (toggle_concierge_mode(True)).
2. State Machine per Sender:
   - Initial Message:
     "Assalomu alaykum! Men Azizbek Maripovning adminiman. Azizbek hozir bandlar. Onlayn bo'lishi bilan sizga javob yozadi."
   - Active Phase: Up to 10 minutes window (or max 4-5 dialogue turns). In this phase, Monika
     politely converses, answers questions via Gemini with strict privacy guardrails, or accepts notes.
   - Note-taking: When the sender leaves a request or note for Azizbek, Monika records it, notifies
     Azizbek via the admin bot (MyJarvis), and confirms:
     "Xabaringizni qayd etdim, Azizbekka yetkazaman."
   - Courteous Closing & Cooldown:
     After 10 minutes or concluding dialogue, Monika gracefully closes:
     "Barcha ma'lumotlarni qayd etdim. Azizbek onlayn bo'lishlari bilan albatta aloqaga chiqadilar. Rahmat!"
     and enters 30-minute Cooldown.
   - Cooldown: For 30 minutes, Monika stays completely silent even if the sender keeps writing.
   - Re-engagement: After 30 minutes, if the sender writes again, a fresh session starts.
3. Strict Privacy Guardrails:
   - NEVER leaks phone numbers, passwords, precise locations, or private details.
   - NEVER makes binding promises on behalf of Azizbek.
4. Persistent State:
   - Thread-safe, atomic persistence in `user_concierge_state.json`.
"""

from __future__ import annotations

import os
import sys
import re
import json
import time
import html
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Union

# Set up logging
logger = logging.getLogger("JarvisConcierge")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-CONCIERGE] %(levelname)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# Base directories
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "user_concierge_state.json"
TASHKENT_TZ = timezone(timedelta(hours=5))

# Import configuration
try:
    import config
    TELEGRAM_BOT_TOKEN = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_ADMIN_CHAT_ID = getattr(config, "TELEGRAM_ADMIN_CHAT_ID", "")
except ImportError:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# Standard canonical messages
INITIAL_MESSAGE = (
    "Assalomu alaykum! Men Azizbek Maripovning adminiman. "
    "Azizbek hozir bandlar. Onlayn bo'lishi bilan sizga javob yozadi."
)

NOTE_CONFIRMED_REPLY = "Xabaringizni qayd etdim, Azizbekka yetkazaman."

COURTESY_CLOSING = (
    "Barcha ma'lumotlarni qayd etdim. "
    "Azizbek onlayn bo'lishlari bilan albatta aloqaga chiqadilar. Rahmat!"
)

# Constants for Timing & Dialogue
SESSION_ACTIVE_TTL_SECONDS = 600       # 10 minutes active window
COOLDOWN_SECONDS = 1800                 # 30 minutes cooldown
MAX_ACTIVE_TURNS = 5                    # Max 4-5 dialogue turns before cooldown
USER_OUTGOING_SUPPRESSION_SECONDS = 1200  # 20 minutes suppression if Azizbek sent a message in this chat

# Regex patterns for concluding dialogue from user
CONCLUDING_PATTERNS = [
    r"^(?:rahmat|katta\s+rahmat|tashakkur|raxmat)[\.!\s]*$",
    r"^(?:bo['`ʼ]?ldi\s*rahmat|tushundim\s*rahmat|xo['`ʼ]?p\s*rahmat)[\.!\s]*$",
    r"^(?:xo['`ʼ]?p|mayli|tushunarli|tushundim|yaxshi|kelishdik|tushundim\s*aka)[\.!\s]*$",
    r"^(?:sog['`ʼ]?\s*bo['`ʼ]?ling|omon\s*bo['`ʼ]?ling|xayr|ko['`ʼ]?rishguncha)[\.!\s]*$",
]

# Regex patterns indicating user is leaving a request or note for Azizbek
NOTE_TRIGGER_PATTERNS = [
    r"\b(?:ayt(?:ib)?\s*qo['`ʼ]?y(?:ing|sin|ingizlar)?)\b",
    r"\b(?:yetkaz(?:ib)?\s*qo['`ʼ]?y(?:ing|sin)?)\b",
    r"\b(?:unga\s*ayting)\b",
    r"\b(?:azizbekka\s*ayting)\b",
    r"\b(?:xabar\s*qoldir(?:moqchiman|ay|moqchiman)?)\b",
    r"\b(?:iltimos\s*aytib)\b",
    r"\b(?:qo['`ʼ]?ng['`ʼ]?iroq\s*qilib\s*yuborsin)\b",
    r"\b(?:telefon\s*qil(?:sin|ib\s*yuborsin))\b",
    r"\b(?:bog['`ʼ]?lan(?:sin|ib\s*chiqsin))\b",
    r"\b(?:kutayotganimni\s*ayting)\b",
    r"\b(?:yubordim|tashladim|tashlab\s*qo['`ʼ]?ydim)\b",
    r"\b(?:uchrashuv|majlis|dars|imtihon|hisobot|fayl)\b",
]

# Privacy guardrail regex patterns (strictly blocked from leaking)
PHONE_REGEX = re.compile(r"(?:\+?998[\s.-]?\d{2}[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}|\b\d{9,13}\b)")
BINDING_PROMISE_PATTERNS = [
    r"\b(?:va['`ʼ]?da\s*beraman)\b",
    r"\b(?:albatta\s*boradi)\b",
    r"\b(?:albatta\s*keladi)\b",
    r"\b(?:albatta\s*to['`ʼ]?laydi)\b",
    r"\b(?:kelishdik|shartnoma\s*imzolaydi)\b",
    r"\b(?:kafolat\s*beraman)\b",
]


class JarvisConcierge:
    """Monika AI Concierge State Machine and Auto-Responder Engine."""

    _instance: Optional[JarvisConcierge] = None
    _lock = threading.RLock()

    def __init__(self, state_file: Optional[Union[str, Path]] = None) -> None:
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state: Dict[str, Any] = self._load_state_unlocked()

    @classmethod
    def get_instance(cls) -> JarvisConcierge:
        """Singleton accessor for JarvisConcierge."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = JarvisConcierge()
            return cls._instance

    # =========================================================================
    # Persistent State Storage
    # =========================================================================

    def _load_state_unlocked(self) -> Dict[str, Any]:
        """Load state JSON without acquiring lock."""
        default_state: Dict[str, Any] = {
            "toggle_enabled": False,
            "sessions": {},
            "inbox_notes": [],
            "user_last_outgoing": {},
            "global_last_outgoing_time": 0.0,
            "stats": {
                "total_messages_handled": 0,
                "total_notes_taken": 0,
                "total_sessions": 0,
            }
        }
        if not self.state_file.exists():
            return default_state

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "toggle_enabled" not in data:
                        data["toggle_enabled"] = False
                    if "sessions" not in data:
                        data["sessions"] = {}
                    if "inbox_notes" not in data:
                        data["inbox_notes"] = []
                    if "user_last_outgoing" not in data:
                        data["user_last_outgoing"] = {}
                    if "global_last_outgoing_time" not in data:
                        data["global_last_outgoing_time"] = 0.0
                    if "stats" not in data:
                        data["stats"] = default_state["stats"]
                    return data
        except Exception as e:
            logger.warning(f"Failed to read concierge state from {self.state_file}: {e}")
        return default_state

    def _save_state_unlocked(self) -> None:
        """Atomically persist state JSON without acquiring lock."""
        try:
            tmp_file = self.state_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            tmp_file.replace(self.state_file)
        except Exception as e:
            logger.error(f"Failed to persist concierge state: {e}", exc_info=True)

    def load_state(self) -> Dict[str, Any]:
        """Thread-safe state loader."""
        with self._lock:
            self._state = self._load_state_unlocked()
            return self._state

    def save_state(self) -> None:
        """Thread-safe state saver."""
        with self._lock:
            self._save_state_unlocked()

    # =========================================================================
    # Helper & Control Methods
    # =========================================================================

    @staticmethod
    def _format_date(dt: Optional[datetime] = None) -> str:
        """Format datetime in Tashkent timezone with friendly Uzbek string."""
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(TASHKENT_TZ)
        now = datetime.now(TASHKENT_TZ)

        months = [
            "", "yanvar", "fevral", "mart", "aprel", "may", "iyun",
            "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"
        ]
        m_name = months[local_dt.month] if 1 <= local_dt.month <= 12 else str(local_dt.month)

        if local_dt.date() == now.date():
            return f"Bugun, {local_dt.strftime('%H:%M')}"
        elif local_dt.date() == (now - timedelta(days=1)).date():
            return f"Kecha, {local_dt.strftime('%H:%M')}"
        else:
            return f"{local_dt.day}-{m_name}, {local_dt.strftime('%H:%M')}"

    def is_away_mode_active(self) -> bool:
        """Check if Concierge Away mode is currently active.
        
        Active if:
        1. toggle_enabled flag is True, OR
        2. Windows workstation desktop is locked (via JarvisSystem).
        """
        with self._lock:
            if self._state.get("toggle_enabled", False):
                return True

        # Check workstation lock
        try:
            from jarvis_system import JarvisSystem
            sys_ctrl = JarvisSystem()
            status = sys_ctrl.get_security_status()
            status_str = str(status).lower()
            if "locked" in status_str and "unlocked" not in status_str:
                return True
        except Exception as e:
            logger.debug(f"Could not query JarvisSystem security status: {e}")

        return False

    def get_security_status_str(self) -> str:
        """Return human-readable security status of workstation."""
        try:
            from jarvis_system import JarvisSystem
            return JarvisSystem().get_security_status()
        except Exception:
            return "Unknown"

    def get_concierge_status(self) -> Dict[str, Any]:
        """Return comprehensive status of the Concierge / Auto-Responder."""
        with self._lock:
            now = time.time()
            sessions = self._state.get("sessions", {})
            active_count = 0
            cooldown_count = 0

            for s in sessions.values():
                phase = s.get("phase", "active")
                cooldown_until = s.get("cooldown_until", 0.0)
                start_time = s.get("session_start_time", 0.0)

                if phase == "cooldown" and now < cooldown_until:
                    cooldown_count += 1
                elif phase == "active" and (now - start_time) <= SESSION_ACTIVE_TTL_SECONDS:
                    active_count += 1

            inbox = self._state.get("inbox_notes", [])
            toggle_flag = self._state.get("toggle_enabled", False)
            away_active = self.is_away_mode_active()

            return {
                "enabled": away_active,
                "toggle_enabled": toggle_flag,
                "away_mode_active": away_active,
                "security_status": self.get_security_status_str(),
                "active_sessions_count": active_count,
                "cooldown_sessions_count": cooldown_count,
                "total_sessions": len(sessions),
                "total_inbox_notes": len(inbox),
                "stats": self._state.get("stats", {}),
            }

    def toggle_concierge_mode(self, enabled: Optional[bool] = None) -> Dict[str, Any]:
        """Toggle or explicitly set the concierge toggle flag and persist."""
        with self._lock:
            current = self._state.get("toggle_enabled", False)
            new_val = not current if enabled is None else bool(enabled)
            self._state["toggle_enabled"] = new_val
            self._save_state_unlocked()
            logger.info(f"Concierge toggle mode set to: {new_val}")
            return self.get_concierge_status()

    def get_inbox_notes(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the latest inbox notes, sorted newest first."""
        with self._lock:
            notes = self._state.get("inbox_notes", [])
            sorted_notes = sorted(notes, key=lambda x: x.get("timestamp", 0), reverse=True)
            return sorted_notes[:limit]

    def clear_inbox_notes(self) -> bool:
        """Clear all stored inbox notes and persist state."""
        with self._lock:
            self._state["inbox_notes"] = []
            self._save_state_unlocked()
            logger.info("Concierge inbox notes cleared.")
            return True

    # =========================================================================
    # User Outgoing Activity Tracking (20-Minute Anti-Interference Guard)
    # =========================================================================

    def record_user_outgoing(self, chat_id: Union[str, int], timestamp: Optional[float] = None) -> None:
        """Record that the user (Azizbek) sent an outgoing message in this chat.
        Suppresses concierge auto-reply in this chat for 20 minutes (1200 seconds)
        and clears any active concierge session for this chat since the user took over.
        """
        cid = str(chat_id).strip()
        if not cid:
            return
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._lock:
            outgoing = self._state.setdefault("user_last_outgoing", {})
            outgoing[cid] = ts
            self._state["global_last_outgoing_time"] = ts

            # If there was an active concierge session for this sender, cancel it
            sessions = self._state.setdefault("sessions", {})
            if cid in sessions:
                logger.info(f"Azizbek actively messaged in chat {cid}. Cancelling active concierge session.")
                sessions.pop(cid, None)

            self._save_state_unlocked()
            logger.info(f"Recorded Azizbek's outgoing message in chat {cid} at {ts:.0f}. Auto-reply suppressed for 20m.")

    def is_user_recently_active_in_chat(
        self,
        chat_id: Union[str, int],
        window_seconds: float = USER_OUTGOING_SUPPRESSION_SECONDS
    ) -> bool:
        """Check if Azizbek sent a message in this chat within window_seconds (default: 20 min)."""
        cid = str(chat_id).strip()
        if not cid:
            return False
        with self._lock:
            outgoing = self._state.get("user_last_outgoing", {})
            last_ts = outgoing.get(cid, 0.0)
            if (time.time() - last_ts) < window_seconds:
                return True
        return False

    def get_user_last_outgoing_time(self, chat_id: Union[str, int]) -> float:
        """Return the timestamp of the last outgoing message sent by Azizbek in this chat."""
        cid = str(chat_id).strip()
        with self._lock:
            outgoing = self._state.get("user_last_outgoing", {})
            return outgoing.get(cid, 0.0)

    # =========================================================================
    # Dialogue & Guardrail Evaluation
    # =========================================================================

    @staticmethod
    def is_concluding_message(text: str) -> bool:
        """Check if user message signals conclusion/gratitude/wrap-up.
        
        Matches concluding expressions such as:
        - "Rahmat", "Katta rahmat", "Tashakkur"
        - "Rahmat, tushunarli", "Bo'ldi rahmat", "Xo'p rahmat"
        - "Xo'p", "Mayli", "Tushundim", "Yaxshi", "Kelishdik"
        - "Sog' bo'ling", "Omon bo'ling", "Xayr", "Ko'rishguncha"
        """
        clean = text.strip().lower()
        if not clean:
            return False

        # If it's a question or contains a note trigger, do not treat as mere conclusion
        if "?" in clean:
            return False
        if JarvisConcierge.is_note_trigger(clean):
            return False

        # Check regex patterns
        if any(bool(re.search(pat, clean)) for pat in CONCLUDING_PATTERNS):
            return True

        # Check multi-word concluding tokens
        words = re.findall(r"\b\w+[\'\`ʼ]?\w*\b", clean)
        if not words or len(words) > 7:
            return False

        concluding_tokens = {
            "rahmat", "raxmat", "tashakkur", "tushundim", "tushunarli",
            "mayli", "xo'p", "xop", "yaxshi", "kelishdik", "bo'ldi", "boldi",
            "xayr", "omon", "sog'", "sog", "salomat", "kutaman"
        }

        matching = [w for w in words if any(ct in w for ct in concluding_tokens)]
        return len(matching) > 0

    @staticmethod
    def is_note_trigger(text: str) -> bool:
        """Check if message contains an explicit request or message for Azizbek."""
        clean = text.strip().lower()
        return any(bool(re.search(pat, clean, re.IGNORECASE)) for pat in NOTE_TRIGGER_PATTERNS)

    @classmethod
    def sanitize_concierge_response(cls, text: str) -> str:
        """Enforce strict privacy guardrails on generated response.
        
        Guarantees:
        - No phone numbers leak
        - No binding promises leak
        - Safe replacement if violated
        """
        if not text:
            return INITIAL_MESSAGE

        # Check phone number leakage
        if PHONE_REGEX.search(text):
            logger.warning("Guardrail alert: Leaked phone number detected in AI response. Sanitizing.")
            return (
                "Xavfsizlik va maxfiylik talablariga ko'ra shaxsiy telefon raqamlarini ulasha olmayman. "
                "Xabaringizni yozib qoldirsangiz, Azizbek onlayn bo'lishlari bilan o'zlari siz bilan bog'lanadilar."
            )

        # Check binding promises
        for pat in BINDING_PROMISE_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                logger.warning(f"Guardrail alert: Leaked binding promise detected ('{pat}'). Sanitizing.")
                return (
                    "Men Azizbek nomidan qat'iy va'da bera olmayman, ammo bu xabaringizni albatta ularga yetkazaman. "
                    "Onlayn bo'lishlari bilan o'zlari sizga aniqlik kiritadilar."
                )

        return text

    # =========================================================================
    # Gemini AI Response Generation & Safe Fallback
    # =========================================================================

    @classmethod
    def generate_concierge_reply(
        cls,
        user_text: str,
        history: List[Dict[str, Any]],
        sender_name: str
    ) -> str:
        """Generate polite, context-aware AI response using Gemini with fallback."""
        # 1. Try Gemini if configured
        try:
            import config
            if hasattr(config, "is_gemini_configured") and config.is_gemini_configured():
                import google.generativeai as genai
                genai.configure(api_key=config.GEMINI_API_KEY)

                system_prompt = (
                    "Siz Azizbek Maripovning shaxsiy kotibasi va adminisiz (Monika).\n"
                    "Hozir Azizbek band bo'lganligi sababli uning shaxsiy Telegramiga kelgan xabarga javob beryapsiz.\n\n"
                    "QAT'IY XAVFSIZLIK VA MAXFIYLIK QOIDALARI:\n"
                    "1. MAXFIYLIK: Hech qachon Azizbekning telefon raqami, paroli, aniq joyi (lokatsiyasi) yoki shaxsiy ma'lumotlarini oshkor qilmang!\n"
                    "2. VA'DA BERMASLIK: Azizbek nomidan hech qanday qat'iy va'da bermang (masalan 'albatta keladi', 'pulini to'laydi' demang). Faqat 'Xabaringizni yetkazaman' deng.\n"
                    "3. HURMAT VA QISQALIK: O'zbek tilida muloyim, hurmat bilan va 1-2 jumlada qisqa javob bering.\n"
                    "4. XABAR QABUL QILISH: Agar murojaatchi xabar yoki iltimos qoldirmoqchi bo'lsa, xabarni qabul qilishingizni bildiring.\n"
                    f"Murojaatchi ismi: {sender_name}."
                )

                candidate_models = getattr(config, "GEMINI_FALLBACK_MODELS", [
                    "models/gemini-3.5-flash",
                    "models/gemini-3.5-flash-lite",
                    "models/gemini-flash-lite-latest"
                ])

                for m_name in candidate_models:
                    try:
                        model = genai.GenerativeModel(
                            m_name,
                            system_instruction=system_prompt,
                            generation_config={"temperature": 0.3, "max_output_tokens": 120}
                        )
                        # Format clean alternating history
                        chat_history = []
                        prior_items = history[:-1] if history and history[-1].get("role") == "user" and history[-1].get("text") == user_text else history
                        last_role = None
                        for h_item in prior_items[-6:]:
                            raw_role = h_item.get("role")
                            role = "user" if raw_role == "user" else "model"
                            if not chat_history and role != "user":
                                continue
                            if role == last_role:
                                continue
                            chat_history.append({"role": role, "parts": [h_item.get("text", "")]})
                            last_role = role

                        if chat_history and chat_history[-1]["role"] == "user":
                            chat_history.pop()

                        chat = model.start_chat(history=chat_history)
                        resp = chat.send_message(user_text)
                        if resp and resp.text:
                            reply = resp.text.strip()
                            return cls.sanitize_concierge_response(reply)
                    except Exception as mod_err:
                        err_str = str(mod_err).lower()
                        logger.warning(f"Gemini error with {m_name}: {mod_err}")
                        if "429" in err_str or "quota" in err_str:
                            break
                        continue
        except Exception as e:
            logger.debug(f"Gemini concierge call error: {e}")

        # 2. Smart local rule-based fallback
        return cls._generate_local_fallback(user_text, sender_name)

    @classmethod
    def _generate_local_fallback(cls, query: str, sender_name: str) -> str:
        """Safe, polite local fallback response in pure Uzbek."""
        q = query.lower()
        if any(w in q for w in ["qayerda", "qattasiz", "qayerdasiz", "manzil", "joy", "qatta"]):
            return (
                "Azizbek hozir muhim ishlar bilan bandlar, aniq manzillarini ayta olmayman. "
                "Biror zarur xabaringiz bo'lsa qoldiring, onlayn bo'lishlari bilan albatta yetkazaman."
            )
        elif any(w in q for w in ["nomer", "raqam", "telefon", "nomeri"]):
            return (
                "Xavfsizlik va maxfiylik siyosatiga ko'ra shaxsiy telefon raqamlarini ulasha olmayman. "
                "Xabaringizni yozib qoldirsangiz, Azizbek o'zlari siz bilan bog'lanadilar."
            )
        elif any(w in q for w in ["qachon", "qachon keladi", "qachon bo'shaydi", "vaqti bormi"]):
            return (
                "Azizbek hozir bandlar. Bo'shashlari bilan xabaringizni ko'rib, "
                "onlayn bo'lishlari bilan sizga aloqaga chiqadilar."
            )
        elif any(w in q for w in ["salom", "assalom", "qalesiz", "yaxshimisiz", "bormisiz"]):
            return (
                "Assalomu alaykum! Azizbek hozirda bandlar. "
                "Biror xabar yoki iltimosingiz bo'lsa, bemalol yozib qoldirishingiz mumkin."
            )
        else:
            return (
                "Tushundim. Azizbek hozir band bo'lganliklari sababli, xabaringizni "
                "yozib qoldirsangiz, onlayn bo'lishlari bilan o'zlari sizga javob yozadilar."
            )

    # =========================================================================
    # Notification to Azizbek (Admin Bot Chat)
    # =========================================================================

    def notify_admin_bot(
        self,
        sender_name: str,
        username: str,
        note_text: str,
        bot_instance: Optional[Any] = None
    ) -> bool:
        """Format and dispatch notification to Azizbek's admin bot (MyJarvis).
        
        Format:
        📩 <b>[Kotiba Rejimi] Yangi xabar:</b>
        👤 <b>{sender_name}</b> ({username}):
        💬 <i>"{note_text}"</i>
        """
        esc_name = html.escape(sender_name or "Noma'lum")
        clean_user = username.lstrip("@").strip() if username else ""
        esc_uname = f"@{html.escape(clean_user)}" if clean_user else "(username yo'q)"
        esc_text = html.escape(note_text or "")

        notification = (
            "📩 <b>[Kotiba Rejimi] Yangi xabar:</b>\n"
            f"👤 <b>{esc_name}</b> ({esc_uname}):\n"
            f"💬 <i>\"{esc_text}\"</i>"
        )

        # 1. Try provided bot_instance
        if bot_instance:
            try:
                if hasattr(bot_instance, "send_message"):
                    bot_instance.send_message(notification)
                    logger.info(f"Admin bot notified via bot_instance for {esc_name}")
                    return True
            except Exception as e:
                logger.warning(f"Failed to notify admin via bot_instance: {e}")

        # 2. Try JarvisTelegram gateway
        try:
            from jarvis_telegram import JarvisTelegram
            tg_gateway = JarvisTelegram()
            if tg_gateway.token and tg_gateway.admin_chat_id:
                sent = tg_gateway.send_message(notification)
                if sent:
                    logger.info(f"Admin bot notified via JarvisTelegram gateway for {esc_name}")
                    return True
        except Exception as e:
            logger.debug(f"JarvisTelegram notification fallback error: {e}")

        # 3. Direct Telegram Bot API HTTP POST fallback
        token = TELEGRAM_BOT_TOKEN
        admin_chat_id = TELEGRAM_ADMIN_CHAT_ID
        if token and admin_chat_id:
            try:
                import requests
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {
                    "chat_id": admin_chat_id,
                    "text": notification,
                    "parse_mode": "HTML"
                }
                resp = requests.post(url, json=payload, timeout=4.0)
                if resp.status_code == 200:
                    logger.info(f"Admin bot notified directly via HTTP for {esc_name}")
                    return True
                else:
                    logger.warning(f"Telegram HTTP notification returned status {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.error(f"Telegram direct HTTP notification failed: {e}")

        logger.warning(f"Could not deliver admin notification: Admin chat/token not configured.")
        return False

    # =========================================================================
    # Main Event Handler (Telethon)
    # =========================================================================

    async def handle_telethon_event(
        self,
        client: Any,
        event: Any,
        bot_instance: Optional[Any] = None
    ) -> bool:
        """Process incoming Telethon event through Monika AI Concierge state machine.
        
        Returns:
            bool: True if event was handled (responded to or recorded), False otherwise.
        """
        # ---------------------------------------------------------------------
        # 1. FILTER GUARDRAILS
        # ---------------------------------------------------------------------
        # If message was sent by Azizbek himself (outgoing):
        if getattr(event, "out", False):
            chat_id = str(getattr(event, "chat_id", "") or getattr(event, "to_id", "") or "")
            if chat_id:
                self.record_user_outgoing(chat_id)
            return False

        # Strictly 1-on-1 private human chats ONLY
        if not getattr(event, "is_private", False):
            return False
        if getattr(event, "is_group", False) or getattr(event, "is_channel", False):
            return False

        # Get sender details
        try:
            sender = await event.get_sender()
        except Exception as e:
            logger.debug(f"Failed to fetch event sender: {e}")
            sender = None

        if not sender:
            return False

        # Never reply to bots or self
        if getattr(sender, "bot", False) or getattr(sender, "is_self", False):
            return False

        sender_id = str(getattr(sender, "id", "") or event.sender_id)
        if not sender_id:
            return False

        first_name = getattr(sender, "first_name", "") or ""
        last_name = getattr(sender, "last_name", "") or ""
        sender_name = f"{first_name} {last_name}".strip() or "Do'st"
        username = getattr(sender, "username", "")
        username_str = f"@{username}" if username else ""

        message_text = (getattr(event, "raw_text", "") or getattr(event, "text", "") or "").strip()
        if not message_text:
            return False

        now = time.time()

        # ---------------------------------------------------------------------
        # CRITICAL USER CONVERSATION ENGAGEMENT CHECK (20-Minute Anti-Interference Rule):
        # If Azizbek has sent any message in this chat within the last 20 minutes (1200s),
        # DO NOT intervene! The user is actively engaged in a conversation with this person.
        # ---------------------------------------------------------------------
        if self.is_user_recently_active_in_chat(sender_id, window_seconds=USER_OUTGOING_SUPPRESSION_SECONDS):
            last_out = self.get_user_last_outgoing_time(sender_id)
            elapsed = int(now - last_out)
            logger.info(
                f"🚫 [CONCIERGE SUPPRESSED]: Azizbek was active in chat with {sender_name} ({sender_id}) "
                f"{elapsed}s ago (<{USER_OUTGOING_SUPPRESSION_SECONDS}s). Suppressing auto-reply to avoid interrupting conversation."
            )
            return False

        # Safety Net via Telethon Chat History:
        # Check actual messages on Telegram servers in case of restart or message sent from another device
        if client and hasattr(client, "get_messages"):
            try:
                chat_target = getattr(event, "chat_id", None) or sender
                recent_msgs = await client.get_messages(chat_target, limit=6)
                user_recently_chatted = False
                for m in recent_msgs:
                    if getattr(m, "out", False) and getattr(m, "date", None):
                        m_ts = m.date.timestamp()
                        if (now - m_ts) < USER_OUTGOING_SUPPRESSION_SECONDS:
                            user_recently_chatted = True
                            self.record_user_outgoing(sender_id, m_ts)
                            logger.info(
                                f"🚫 [CONCIERGE SUPPRESSED via History]: Azizbek sent message in chat with "
                                f"{sender_name} at {m.date} ({int(now - m_ts)}s ago < 1200s). Suppressing auto-reply."
                            )
                            break
                if user_recently_chatted:
                    return False
            except Exception as e:
                logger.debug(f"History outgoing check exception (ignored): {e}")

        # Only activate when Away mode is active (system locked OR toggle flag)
        if not self.is_away_mode_active():
            return False

        # ---------------------------------------------------------------------
        # 2. STATE MACHINE DISPATCH PER SENDER
        # ---------------------------------------------------------------------
        with self._lock:
            sessions = self._state.setdefault("sessions", {})
            session = sessions.get(sender_id)

            # Check if previous session exists
            if session:
                phase = session.get("phase", "active")
                cooldown_until = session.get("cooldown_until", 0.0)

                # COOLDOWN CHECK:
                if phase == "cooldown":
                    if now < cooldown_until:
                        # Strictly stay silent during cooldown
                        rem = int(cooldown_until - now)
                        logger.info(f"Sender {sender_name} ({sender_id}) in cooldown for {rem}s. Remaining silent.")
                        return False
                    else:
                        # Cooldown expired! Reset and begin a new session
                        logger.info(f"Cooldown expired for sender {sender_name} ({sender_id}). Starting new session.")
                        session = None

            # New session initialization
            if session is None:
                session = {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "username": username_str,
                    "phase": "active",
                    "session_start_time": now,
                    "last_interaction_time": now,
                    "turn_count": 0,
                    "dialogue_history": [],
                    "cooldown_until": 0.0,
                    "closing_sent": False,
                }
                sessions[sender_id] = session
                self._state["stats"]["total_sessions"] = self._state["stats"].get("total_sessions", 0) + 1

            # Update stats and interaction time
            session["last_interaction_time"] = now
            session["turn_count"] = session.get("turn_count", 0) + 1
            current_turn = session["turn_count"]
            start_time = session.get("session_start_time", now)
            elapsed_session_time = now - start_time

            # Record incoming user text in history
            session.setdefault("dialogue_history", []).append({
                "role": "user",
                "text": message_text,
                "timestamp": now,
            })
            self._state["stats"]["total_messages_handled"] = (
                self._state["stats"].get("total_messages_handled", 0) + 1
            )

            # -----------------------------------------------------------------
            # 3. STATE MACHINE DECISION TREE
            # -----------------------------------------------------------------

            # Case A: 10-Minute Window Expired
            if elapsed_session_time > SESSION_ACTIVE_TTL_SECONDS:
                logger.info(f"Session TTL expired for {sender_name} ({elapsed_session_time:.1f}s > 600s). Closing dialogue.")
                reply_text = COURTESY_CLOSING
                session["phase"] = "cooldown"
                session["cooldown_until"] = now + COOLDOWN_SECONDS
                session["closing_sent"] = True
                self._save_state_unlocked()
                await self._send_reply(event, client, reply_text)
                return True

            # Case B: First Message of a New Session (Initial Message)
            if current_turn == 1:
                # Check if first message also contained an immediate request/note
                has_note = self.is_note_trigger(message_text)
                if has_note:
                    self._record_note_unlocked(sender_id, sender_name, username_str, message_text, bot_instance)
                    reply_text = f"{INITIAL_MESSAGE}\n\n{NOTE_CONFIRMED_REPLY}"
                else:
                    reply_text = INITIAL_MESSAGE

                session.setdefault("dialogue_history", []).append({
                    "role": "concierge",
                    "text": reply_text,
                    "timestamp": time.time(),
                })
                self._save_state_unlocked()
                await self._send_reply(event, client, reply_text)
                return True

            # Case C: User sends concluding message (e.g. "Rahmat", "Xo'p", "Mayli")
            if self.is_concluding_message(message_text):
                logger.info(f"User {sender_name} concluded dialogue with: '{message_text}'. Sending closing.")
                reply_text = COURTESY_CLOSING
                session["phase"] = "cooldown"
                session["cooldown_until"] = now + COOLDOWN_SECONDS
                session["closing_sent"] = True

                session.setdefault("dialogue_history", []).append({
                    "role": "concierge",
                    "text": reply_text,
                    "timestamp": time.time(),
                })
                self._save_state_unlocked()
                await self._send_reply(event, client, reply_text)
                return True

            # Case D: User sends a note or request for Azizbek
            if self.is_note_trigger(message_text):
                logger.info(f"Note detected from {sender_name}: '{message_text}'")
                self._record_note_unlocked(sender_id, sender_name, username_str, message_text, bot_instance)

                # Check if turn limit reached
                if current_turn >= MAX_ACTIVE_TURNS:
                    reply_text = f"{NOTE_CONFIRMED_REPLY}\n\n{COURTESY_CLOSING}"
                    session["phase"] = "cooldown"
                    session["cooldown_until"] = now + COOLDOWN_SECONDS
                    session["closing_sent"] = True
                else:
                    reply_text = NOTE_CONFIRMED_REPLY

                session.setdefault("dialogue_history", []).append({
                    "role": "concierge",
                    "text": reply_text,
                    "timestamp": time.time(),
                })
                self._save_state_unlocked()
                await self._send_reply(event, client, reply_text)
                return True

            # Case E: General Active Conversation (Turn 2 to MAX_ACTIVE_TURNS)
            ai_reply = self.generate_concierge_reply(
                message_text,
                session.get("dialogue_history", []),
                sender_name
            )

            # Check if this turn reaches maximum turns
            if current_turn >= MAX_ACTIVE_TURNS:
                reply_text = f"{ai_reply}\n\n{COURTESY_CLOSING}"
                session["phase"] = "cooldown"
                session["cooldown_until"] = now + COOLDOWN_SECONDS
                session["closing_sent"] = True
            else:
                reply_text = ai_reply

            session.setdefault("dialogue_history", []).append({
                "role": "concierge",
                "text": reply_text,
                "timestamp": time.time(),
            })
            self._save_state_unlocked()
            await self._send_reply(event, client, reply_text)
            return True

    def _record_note_unlocked(
        self,
        sender_id: str,
        sender_name: str,
        username: str,
        note_text: str,
        bot_instance: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Record note into inbox, notify admin, and update stats."""
        now = time.time()
        note_record = {
            "id": f"note_{int(now)}_{sender_id}",
            "sender_id": sender_id,
            "sender_name": sender_name,
            "username": username,
            "note_text": note_text,
            "timestamp": now,
            "date_str": self._format_date(datetime.now(timezone.utc)),
            "notified_admin": False
        }
        self._state.setdefault("inbox_notes", []).append(note_record)
        self._state["stats"]["total_notes_taken"] = self._state["stats"].get("total_notes_taken", 0) + 1

        # Dispatch notification to admin bot asynchronously or in background thread
        def _notify():
            notified = self.notify_admin_bot(sender_name, username, note_text, bot_instance)
            with self._lock:
                note_record["notified_admin"] = notified
                self._save_state_unlocked()

        threading.Thread(target=_notify, name=f"ConciergeNotify-{sender_id}", daemon=True).start()
        return note_record

    async def _send_reply(self, event: Any, client: Any, text: str) -> None:
        """Safely send reply via event or fallback to client."""
        try:
            if hasattr(event, "reply"):
                await event.reply(text)
                return
        except Exception as e:
            logger.debug(f"event.reply failed: {e}. Trying client.send_message...")

        try:
            chat_id = getattr(event, "chat_id", None) or event.sender_id
            if client and hasattr(client, "send_message"):
                await client.send_message(chat_id, text)
        except Exception as e:
            logger.error(f"Failed to send concierge reply to {getattr(event, 'sender_id', '?')}: {e}")


# =============================================================================
# Standalone Module Helper Functions (Required by API Specification)
# =============================================================================

def get_concierge_status() -> Dict[str, Any]:
    """Return status of the Concierge / Auto-Responder."""
    return JarvisConcierge.get_instance().get_concierge_status()


def toggle_concierge_mode(enabled: Optional[bool] = None) -> Dict[str, Any]:
    """Toggle or explicitly set the concierge toggle flag."""
    return JarvisConcierge.get_instance().toggle_concierge_mode(enabled)


def get_inbox_notes(limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent notes recorded by the Concierge."""
    return JarvisConcierge.get_instance().get_inbox_notes(limit=limit)


def clear_inbox_notes() -> bool:
    """Clear all recorded inbox notes."""
    return JarvisConcierge.get_instance().clear_inbox_notes()


async def process_incoming_event(
    client: Any,
    event: Any,
    bot_instance: Optional[Any] = None
) -> bool:
    """Asynchronous entry point for Telethon incoming message events."""
    return await JarvisConcierge.get_instance().handle_telethon_event(client, event, bot_instance)


def process_incoming_event_sync(
    client: Any,
    event: Any,
    bot_instance: Optional[Any] = None
) -> bool:
    """Synchronous wrapper for process_incoming_event."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                process_incoming_event(client, event, bot_instance)
            ).result()
    else:
        return asyncio.run(process_incoming_event(client, event, bot_instance))


if __name__ == "__main__":
    print("=== JARVIS CONCIERGE / AUTO-RESPONDER STATUS ===")
    status = get_concierge_status()
    print(json.dumps(status, indent=2))
