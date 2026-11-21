"""JARVIS Safety Manager (Monika)
==============================
Enforces confirmation gating for destructive and high-impact operations.
User Policy: "eng muhim ishlar uchun ruxsat so'rasin, ayniqsa o'chirish uchun"
(Ask for permission for the most important actions, especially for deletion).

Key Responsibilities:
1. Register dangerous actions with an expiration timer (default 60 seconds).
2. Generate human-friendly confirmation prompts in Uzbek.
3. Validate affirmative ('ha', 'tasdiqlayman', 'yes', 'confirm', 'albatta')
   vs negative ('yoq', "yo'q", 'bekor', 'no', 'cancel') responses.
4. Execute confirmed callbacks safely or abort gracefully.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("JARVIS-SAFETY")


class PendingAction:
    """Represents a sensitive or destructive action awaiting explicit user confirmation."""

    def __init__(
        self,
        action_type: str,
        description: str,
        callback: Callable,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> None:
        self.action_type = action_type
        self.description = description
        self.callback = callback
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.created_at = time.time()
        self.timeout = timeout

    def is_expired(self) -> bool:
        """Check if action confirmation window has elapsed."""
        return (time.time() - self.created_at) > self.timeout

    def time_remaining(self) -> float:
        """Remaining seconds before auto-expiration."""
        return max(0.0, self.timeout - (time.time() - self.created_at))


class SafetyManager:
    """Confirmation gatekeeper for Monika/JARVIS operations."""

    CONFIRMATION_TEMPLATE = (
        "⚠️ <b>DIQQAT:</b> Siz <b>{description}</b> amalini so'radingiz. "
        "Bu muhim amal hisoblanadi. Buni tasdiqlaysizmi? "
        "(Tasdiqlash uchun <i>'Ha'</i> yoki <i>'Tasdiqlayman'</i>, bekor qilish uchun <i>'Yo'q'</i> deng)."
    )

    AFFIRMATIVE_KEYWORDS = {
        "ha",
        "tasdiqlayman",
        "tasdiqlash",
        "tasdiq",
        "yes",
        "confirm",
        "albatta",
        "mayli",
        "ruxsat",
        "bajar",
        "o'chir",
        "ochir",
        "roziman",
        "ha albatta",
        "ha bajar",
        "ha bajargin",
    }

    NEGATIVE_KEYWORDS = {
        "yoq",
        "yo'q",
        "yo`q",
        "yo’q",
        "bekor",
        "bekor qil",
        "bekor_qil",
        "cancel",
        "no",
        "rad",
        "rad et",
        "rad etaman",
        "kerak emas",
        "kerakmas",
        "to'xtat",
        "toxtat",
    }

    DESTRUCTIVE_KEYWORDS = [
        "o'chirish",
        "ochirish",
        "o'chir",
        "ochir",
        "delete",
        "remove",
        "format",
        "tozalash",
        "tozala",
        "kill",
        "shutdown",
        "restart",
        "reboot",
        "destroy",
        "wipe",
        "rmdir",
    ]

    def __init__(self, default_timeout: float = 60.0) -> None:
        self.default_timeout = default_timeout
        self._pending: Optional[PendingAction] = None
        self._lock = threading.Lock()
        logger.info(f"SafetyManager initialized (default confirmation timeout: {default_timeout}s).")

    def is_destructive(self, text: str) -> bool:
        """Check if a natural language query or command contains destructive keywords."""
        if not text:
            return False
        clean = text.lower().strip()

        # Non-destructive exceptions (e.g. mute volume or stop voice)
        if any(exc in clean for exc in ["ovozni o'chir", "ovozni ochir", "to'xta", "toxta", "stop", "jim", "bas"]):
            return False

        for kw in self.DESTRUCTIVE_KEYWORDS:
            if re.search(r'\b' + re.escape(kw) + r'\b', clean) or kw in clean:
                return True
        return False

    def request_confirmation(
        self,
        action_type: str,
        description: str,
        callback_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Register a sensitive action and return the Uzbek confirmation prompt.

        Args:
            action_type: Short identifier for the action (e.g. 'delete_file', 'kill_process', 'shutdown').
            description: Human-readable description (e.g. "'test.txt' faylini o'chirish").
            callback_fn: Callable to execute upon affirmative user response.
            *args: Positional arguments for callback_fn.
            **kwargs: Keyword arguments for callback_fn.

        Returns:
            Formatted Uzbek prompt string.
        """
        with self._lock:
            self._pending = PendingAction(
                action_type=action_type,
                description=description,
                callback=callback_fn,
                args=args,
                kwargs=kwargs,
                timeout=self.default_timeout,
            )
            logger.warning(
                f"[SAFETY GATE ENGAGED] Action: '{action_type}' - '{description}'. "
                f"Awaiting confirmation (expires in {self.default_timeout}s)."
            )

        return self.CONFIRMATION_TEMPLATE.format(description=description)

    def has_pending(self) -> bool:
        """Check whether there is an active, unexpired pending confirmation."""
        with self._lock:
            if self._pending is None:
                return False
            if self._pending.is_expired():
                logger.info(f"[SAFETY GATE EXPIRED] Pending action '{self._pending.description}' expired.")
                self._pending = None
                return False
            return True

    def get_pending(self) -> Optional[PendingAction]:
        """Return the current active pending action if not expired."""
        with self._lock:
            if self._pending and self._pending.is_expired():
                self._pending = None
            return self._pending

    def cancel_pending(self) -> Optional[str]:
        """Manually cancel any pending action."""
        with self._lock:
            if self._pending is None:
                return None
            desc = self._pending.description
            self._pending = None
            logger.info(f"[SAFETY GATE CANCELLED] Action '{desc}' cancelled.")
            return f"❌ Amal bekor qilindi. <b>{desc}</b> bajarilmadi."

    def handle_confirmation_reply(self, reply_text: str) -> Optional[str]:
        """Validate user response to a pending confirmation.

        Args:
            reply_text: User input (e.g. 'ha', 'yoq', 'tasdiqlayman', 'bekor qil').

        Returns:
            str: Result of action or cancellation message.
            None: If there was no pending confirmation.
        """
        with self._lock:
            if self._pending is None:
                return None

            if self._pending.is_expired():
                desc = self._pending.description
                self._pending = None
                return f"⚠️ Tasdiqlash vaqti tugadi (60 soniya o'tdi). <b>{desc}</b> amali bekor qilindi."

            pending = self._pending

        clean = (reply_text or "").strip().lower()
        # Clean leading/trailing punctuation except Uzbek apostrophes
        clean_norm = re.sub(r'^[^\w\'\`\’]+|[^\w\'\`\’]+$', '', clean).strip()

        # 1. Check Negative first to ensure fast, safe abortion
        is_negative = clean_norm in self.NEGATIVE_KEYWORDS

        if is_negative:
            with self._lock:
                if self._pending is pending:
                    self._pending = None
            logger.info(f"[SAFETY GATE DENIED] User declined: '{reply_text}'. Action aborted.")
            return f"❌ Amal bekor qilindi. <b>{pending.description}</b> bajarilmadi."

        # 2. Check Affirmative from bounded set only
        is_affirmative = clean_norm in self.AFFIRMATIVE_KEYWORDS

        if is_affirmative:
            with self._lock:
                if self._pending is not pending or pending.is_expired():
                    return f"⚠️ Tasdiqlash vaqti tugadi yoki amal o'zgardi. <b>{pending.description}</b> bajarilmadi."
                self._pending = None

            logger.info(f"[SAFETY GATE CONFIRMED] User confirmed: '{reply_text}'. Executing callback...")
            try:
                res = pending.callback(*pending.args, **pending.kwargs)
                if isinstance(res, dict):
                    if res.get("success", False):
                        msg = res.get("message") or f"✅ <b>{pending.description}</b> muvaffaqiyatli bajarildi."
                    else:
                        msg = f"❌ Xatolik yuz berdi: {res.get('error') or res.get('message', 'Amal bajarilmadi.')}"
                    return msg
                elif isinstance(res, str):
                    return res
                else:
                    return f"✅ <b>{pending.description}</b> muvaffaqiyatli bajarildi."
            except Exception as e:
                logger.error(f"[SAFETY GATE CALLBACK ERROR] Failed executing '{pending.action_type}': {e}")
                return f"❌ Amalni bajarishda xatolik yuz berdi: {e}"

        # 3. Ambiguous response while pending action is active
        rem = int(pending.time_remaining())
        return (
            f"⚠️ <b>DIQQAT:</b> <b>{pending.description}</b> amali tasdiqlash kutilmoqda (qolgan vaqt: {rem} soniya).\n"
            f"Amalni bajarish uchun <i>'Ha'</i> yoki <i>'Tasdiqlayman'</i>, bekor qilish uchun <i>'Yo'q'</i> deng."
        )
