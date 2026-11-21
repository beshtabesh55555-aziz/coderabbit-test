"""
JARVIS Dialogue State & Interactive Clarification Manager
=========================================================
Manages multi-turn conversation states, pending clarifications,
and disambiguation resolution for JARVIS (Monika).

Core Responsibilities:
1. Thread-safe, atomic persistence of user dialogue states to `user_dialogue_state.json`.
2. Time-To-Live (TTL) expiration tracking (default 300s / 5 minutes).
3. Ambiguity resolution for user replies (inline callback queries or natural text responses):
   - Storage disambiguation (RAM vs Disk vs Both).
   - Wallpaper disambiguation (System Wallpaper vs Desktop folder file).
   - Missing slot completion (recipients, file targets, parameters).
4. State lifecycle management: set, get, resolve, clear, cleanup.
"""

import os
import sys
import json
import time
import re
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any, Union

logger = logging.getLogger("JarvisDialogue")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-DIALOGUE] %(levelname)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

DEFAULT_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = DEFAULT_BASE_DIR / "user_dialogue_state.json"


class JarvisDialogueManager:
    """Thread-safe persistent manager for user dialogue states and clarification loops."""

    def __init__(
        self,
        state_file: Optional[Union[str, Path]] = None,
        nlp_engine: Optional[Any] = None,
    ):
        """Initialize dialogue manager with state persistence and NLP resolution engine.

        Args:
            state_file: Path to `user_dialogue_state.json`. Defaults to jarvis-system directory.
            nlp_engine: AmbiguityEngine instance from `jarvis_nlp.py` or None (lazy loaded).
        """
        self.state_file = Path(state_file) if state_file else DEFAULT_STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._nlp_engine = nlp_engine
        self.ttl_seconds = 300
        logger.info(f"JarvisDialogueManager initialized with state file: {self.state_file}")

    @property
    def nlp_engine(self) -> Any:
        """Lazy load default AmbiguityEngine if not explicitly injected."""
        if self._nlp_engine is None:
            try:
                from jarvis_nlp import get_ambiguity_engine
                self._nlp_engine = get_ambiguity_engine()
            except Exception as e:
                logger.warning(f"Failed to load AmbiguityEngine from jarvis_nlp: {e}")
                self._nlp_engine = None
        return self._nlp_engine

    # =========================================================================
    # Internal Persistence Helpers (Atomic & Thread-safe)
    # =========================================================================

    def _load_all_unlocked(self) -> Dict[str, Dict[str, Any]]:
        """Load states dictionary without acquiring lock (must be called inside lock)."""
        if not self.state_file.exists():
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Error reading dialogue state from {self.state_file}: {e}")
            return {}

    def _save_all_unlocked(self, states: Dict[str, Dict[str, Any]]) -> None:
        """Atomically persist states dictionary (must be called inside lock)."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(states, f, indent=2, ensure_ascii=False)
            tmp_file.replace(self.state_file)
        except Exception as e:
            logger.error(f"Error saving dialogue state to {self.state_file}: {e}", exc_info=True)
            raise RuntimeError(f"Failed to persist dialogue state: {e}") from e

    # =========================================================================
    # State Management & TTL
    # =========================================================================

    def set_pending_clarification(
        self,
        user_id: Union[str, int],
        original_query: str,
        intent: str,
        ambiguity_type: str,
        question: str,
        options: List[Union[Dict[str, str], str]],
        context_data: Optional[Dict[str, Any]] = None,
        ttl_seconds: int = 300,
    ) -> Dict[str, Any]:
        """Save an ambiguous query and its clarifying question for a user with TTL.

        Args:
            user_id: Unique identifier for the user (Telegram ID or session ID).
            original_query: The raw/normalized user query that triggered ambiguity.
            intent: Detected provisional intent (e.g. 'INQUIRE_STORAGE', 'GET_WALLPAPER').
            ambiguity_type: Type of ambiguity (e.g. 'AMBIGUOUS_STORAGE_METRIC', 'AMBIGUOUS_WALLPAPER', 'MISSING_SLOT').
            question: Clarifying question presented to the user.
            options: Interactive options (list of dicts with text & callback_data, or strings).
            context_data: Optional dictionary containing extracted entities (e.g. recipient, target).
            ttl_seconds: Time-To-Live in seconds before state expires (default 300s = 5 minutes).

        Returns:
            dict: The saved clarification record.
        """
        uid = str(user_id).strip()
        now = time.time()
        ttl = max(1, int(ttl_seconds))
        expires_at = now + ttl

        # Normalize options format
        norm_options = []
        for opt in options:
            if isinstance(opt, dict):
                norm_options.append(opt)
            else:
                norm_options.append({"text": str(opt), "callback_data": f"clarify:{str(opt).lower().strip()}"})

        record = {
            "user_id": uid,
            "state": "AWAITING_CLARIFICATION",
            "original_query": str(original_query).strip(),
            "intent": str(intent).strip(),
            "ambiguity_type": str(ambiguity_type).strip(),
            "question": str(question).strip(),
            "options": norm_options,
            "context_data": dict(context_data) if context_data else {},
            "created_at": now,
            "ttl_seconds": ttl,
            "expires_at": expires_at,
        }

        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.setdefault(uid, {
                "user_id": uid,
                "created_at": now,
                "context": {},
                "chat_history": []
            })
            user_entry["pending_clarification"] = record
            self._save_all_unlocked(states)

        logger.info(
            f"Saved pending clarification for user {uid}: type={ambiguity_type}, "
            f"intent={intent}, expires in {ttl}s"
        )
        return record

    def get_pending_clarification(self, user_id: Union[str, int]) -> Optional[Dict[str, Any]]:
        """Check if user has an active pending clarification.

        If the state has expired according to TTL, automatically purges it and returns None.

        Args:
            user_id: Unique identifier for the user.

        Returns:
            dict or None: Active clarification state dictionary, or None if non-existent or expired.
        """
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.get(uid)
            if not user_entry:
                return None
            record = user_entry.get("pending_clarification")
            if not record:
                return None

            now = time.time()
            expires_at = float(record.get("expires_at", 0))
            if now > expires_at:
                logger.info(f"Clarification state for user {uid} expired (TTL exceeded). Purging prompt.")
                user_entry["pending_clarification"] = None
                self._save_all_unlocked(states)
                return None

            return dict(record)

    def is_pending(self, user_id: Union[str, int]) -> bool:
        """Quick check whether user has an active, unexpired pending clarification.

        Args:
            user_id: Unique identifier for the user.

        Returns:
            bool: True if an active clarification is waiting for user response.
        """
        return self.get_pending_clarification(user_id) is not None

    def clear_pending(self, user_id: Union[str, int]) -> bool:
        """Clear or cancel any pending clarification state for a user.

        Args:
            user_id: Unique identifier for the user.

        Returns:
            bool: True if state was present and removed, False otherwise.
        """
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.get(uid)
            if user_entry and user_entry.get("pending_clarification"):
                user_entry["pending_clarification"] = None
                self._save_all_unlocked(states)
                logger.info(f"Cleared pending clarification for user {uid}.")
                return True
            return False

    def cleanup_expired(self) -> int:
        """Purge all expired states across all users and save updated state.

        Returns:
            int: Number of expired states removed.
        """
        now = time.time()
        removed = 0
        with self._lock:
            states = self._load_all_unlocked()
            for uid, user_entry in states.items():
                rec = user_entry.get("pending_clarification")
                if rec and now > float(rec.get("expires_at", 0)):
                    user_entry["pending_clarification"] = None
                    removed += 1
            if removed > 0:
                self._save_all_unlocked(states)
                logger.info(f"Cleaned up {removed} expired clarification prompt(s).")
        return removed

    def update_context(self, user_id: Union[str, int], **kwargs) -> Dict[str, Any]:
        """Update or create user conversation context variables (e.g. last_file, last_screenshot)."""
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.setdefault(uid, {
                "user_id": uid,
                "created_at": time.time(),
                "expires_at": time.time() + self.ttl_seconds,
                "context": {}
            })
            ctx = user_entry.setdefault("context", {})
            ctx.update(kwargs)
            user_entry["expires_at"] = time.time() + self.ttl_seconds
            self._save_all_unlocked(states)
            return ctx

    def get_context(self, user_id: Union[str, int]) -> Dict[str, Any]:
        """Retrieve the conversational context dictionary for a user."""
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.get(uid, {})
            return dict(user_entry.get("context", {}))

    def append_chat_message(self, user_id: Union[str, int], role: str, text: str, **kwargs) -> Dict[str, Any]:
        """Record a chat message (user or assistant) in persistent dialogue history."""
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.setdefault(uid, {
                "user_id": uid,
                "created_at": time.time(),
                "expires_at": time.time() + (self.ttl_seconds * 10),
                "context": {},
                "chat_history": []
            })
            history = user_entry.setdefault("chat_history", [])
            msg = {
                "role": role,
                "text": text,
                "timestamp": time.time(),
                "time_str": time.strftime("%H:%M:%S"),
            }
            if kwargs:
                msg["extra"] = kwargs
            history.append(msg)
            # Retain last 50 conversation turns
            if len(history) > 50:
                user_entry["chat_history"] = history[-50:]
            self._save_all_unlocked(states)
            return msg

    def get_chat_history(self, user_id: Union[str, int], limit: int = 30) -> List[Dict[str, Any]]:
        """Retrieve recent persistent chat message history for a user."""
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            user_entry = states.get(uid, {})
            history = user_entry.get("chat_history", [])
            return list(history[-limit:])

    def clear_chat_history(self, user_id: Union[str, int]) -> bool:
        """Clear chat history for a specific user."""
        uid = str(user_id).strip()
        with self._lock:
            states = self._load_all_unlocked()
            if uid in states and "chat_history" in states[uid]:
                states[uid]["chat_history"] = []
                self._save_all_unlocked(states)
                return True
            return False

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Retrieve all currently active, non-expired dialogue states.

        Returns:
            dict: Mapping of user_id to active dialogue state.
        """
        self.cleanup_expired()
        with self._lock:
            return self._load_all_unlocked()

    def clear_all(self) -> None:
        """Clear all dialogue states for all users."""
        with self._lock:
            self._save_all_unlocked({})
            logger.info("Cleared all dialogue states.")

    # =========================================================================
    # Ambiguity Resolution Logic
    # =========================================================================

    def resolve_clarification(
        self,
        user_id: Union[str, int],
        user_answer_or_callback: str,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a pending clarification using the user's reply or button press.

        Merges the user's selection with the original query, ambiguity type, and context data.
        Generates concrete execution parameters (`resolved_query`, `intent`, `action`, `params`),
        clears the pending state from persistence, and returns the resolved specification.

        Args:
            user_id: Unique identifier for the user.
            user_answer_or_callback: The inline button callback data (e.g. 'clarify:ram')
                                    or natural text input (e.g. 'ram', 'disk', 'wallpaper', 'oboy').

        Returns:
            dict or None: Resolved specification dictionary with actionable parameters,
                          or None if no pending clarification was active for the user.
        """
        uid = str(user_id).strip()
        pending = self.get_pending_clarification(uid)
        if not pending:
            logger.info(f"No active pending clarification found for user {uid}.")
            return None

        orig_query = pending.get("original_query", "")
        ambiguity_type = pending.get("ambiguity_type", "NONE")
        original_intent = pending.get("intent", "")
        context_data = pending.get("context_data") or {}
        options = pending.get("options") or []
        answer_raw = str(user_answer_or_callback).strip()
        answer_low = answer_raw.lower()

        logger.info(
            f"Resolving clarification for user {uid}: answer='{answer_raw}', "
            f"ambiguity_type={ambiguity_type}, orig_query='{orig_query}'"
        )

        # Check for explicit cancellation
        cancellation_keywords = {"bekor", "bekor qil", "yo'q", "yoq", "kerakmas", "kerak emas", "cancel", "stop", "to'xtat"}
        if answer_low in cancellation_keywords:
            self.clear_pending(uid)
            return {
                "status": "cancelled",
                "user_id": uid,
                "original_query": orig_query,
                "ambiguity_type": ambiguity_type,
                "user_answer": answer_raw,
                "message": "Aniqlashtirish bekor qilindi.",
            }

        # Step 1: Map user answer to a canonical callback_data string
        callback_data = self._map_answer_to_callback(answer_raw, ambiguity_type, options)
        if callback_data == "invalid_choice":
            return {
                "status": "unresolved",
                "user_id": uid,
                "message": "Taklif qilingan variantlardan birini tanlang.",
                "pending": True
            }

        # Step 2: Use AmbiguityEngine or internal rule resolver to form resolution
        resolved_details = None
        if self.nlp_engine and hasattr(self.nlp_engine, "resolve_clarification"):
            try:
                resolved_details = self.nlp_engine.resolve_clarification(orig_query, callback_data)
            except Exception as e:
                logger.warning(f"Error calling nlp_engine.resolve_clarification: {e}")

        if not resolved_details or resolved_details.get("status") == "unresolved":
            resolved_details = self._internal_resolve(orig_query, callback_data, ambiguity_type, original_intent)

        if not resolved_details or resolved_details.get("status") != "resolved":
            return {
                "status": "unresolved",
                "user_id": uid,
                "message": "Taklif qilingan variantlardan birini tanlang.",
                "pending": True
            }

        # Step 3: Combine with context data (e.g. recipient from context_data or original_query)
        params = dict(resolved_details.get("kwargs") or resolved_details.get("params") or {})
        
        # Merge context_data into params
        for k, v in context_data.items():
            if k not in params:
                params[k] = v

        # Extract recipient if present in original query or context
        recipient = params.get("recipient") or context_data.get("recipient")
        if not recipient:
            m_rec = re.search(r'(@[a-zA-Z0-9_]{3,})', orig_query)
            if m_rec:
                recipient = m_rec.group(1)
                params["recipient"] = recipient

        # If metric is present, ensure it's in params
        if "metric" in resolved_details:
            params["metric"] = resolved_details["metric"]

        resolved_query = resolved_details.get("resolved_query") or orig_query
        intent = resolved_details.get("intent") or original_intent
        action = resolved_details.get("action") or "process_command"
        message = resolved_details.get("message") or "So'rov aniqlashtirildi va bajarilmoqda."

        # Contextual adjustment: If user clarified wallpaper and original query was sending to someone
        if intent in ("GET_WALLPAPER", "SEND_WALLPAPER") and recipient:
            intent = "SEND_WALLPAPER"
            action = "send_wallpaper_image"
            params["type"] = "wallpaper"
            params["recipient"] = recipient
            resolved_query = f"Ish stoli fon rasmini (Wallpaper) {recipient} ga yubor"

        result = {
            "status": "resolved",
            "user_id": uid,
            "resolved_query": resolved_query,
            "intent": intent,
            "action": action,
            "params": params,
            "kwargs": params,
            "metric": params.get("metric"),
            "original_query": orig_query,
            "ambiguity_type": ambiguity_type,
            "user_answer": answer_raw,
            "callback_data": callback_data,
            "message": message,
        }

        # Step 4: Clear pending state now that resolution succeeded
        cleared = self.clear_pending(uid)
        if not cleared:
            logger.warning(f"Clarification resolution collision/race condition for user {uid}")
            return {
                "status": "already_handled",
                "user_id": uid,
                "message": "Ushbu so'rov allaqachon bajarilgan."
            }

        logger.info(f"Clarification successfully resolved for user {uid} -> action: {action}, intent: {intent}")
        return result

    def _map_answer_to_callback(
        self,
        answer: str,
        ambiguity_type: str,
        options: List[Dict[str, str]],
    ) -> str:
        """Map natural user answer or button click to canonical callback_data."""
        clean = answer.strip()
        low = clean.lower()

        # 1. Already a callback_data
        if clean.startswith("clarify:"):
            valid_cbs = {opt.get("callback_data") for opt in options if isinstance(opt, dict) and opt.get("callback_data")}
            if valid_cbs and clean not in valid_cbs:
                logger.warning(f"Callback '{clean}' does not match active options: {valid_cbs}")
                return "invalid_choice"
            return clean

        # 2. Number index selection (e.g. '1', '2', '3')
        if clean.isdigit() and options:
            idx = int(clean) - 1
            if 0 <= idx < len(options):
                cb = options[idx].get("callback_data")
                if cb:
                    return cb

        # 3. Match against options text
        for opt in options:
            opt_text = opt.get("text", "").lower()
            opt_cb = opt.get("callback_data", "")
            if low in opt_text or opt_text in low:
                return opt_cb

        # 4. Semantic matching based on ambiguity_type
        if ambiguity_type in ("AMBIGUOUS_STORAGE_METRIC", "AMBIGUOUS_METRIC") or "storage" in low or "xotira" in low:
            if any(w in low for w in ["ram", "operativka", "tezkor", "ozu"]):
                return "clarify:ram"
            if any(w in low for w in ["disk", "ssd", "hdd", "qattiq", "joy", "xotira", "bosh joy", "bo'sh joy"]):
                return "clarify:disk"
            if any(w in low for w in ["ikkalasi", "ikkalasini", "both", "har ikkisi", "hammasi", "barcha"]):
                return "clarify:storage_both"

        if ambiguity_type in ("AMBIGUOUS_WALLPAPER", "AMBIGUOUS_TARGET") or any(w in low for w in ["oboy", "wallpaper", "fon", "rasm"]):
            if any(w in low for w in ["oboy", "wallpaper", "fon", "fon rasmi", "fon rasmini", "orqa fon", "transcoded"]):
                return "clarify:wallpaper"
            if any(w in low for w in ["ish stoli", "ish stolidagi", "desktop", "fayl", "papkadagi", "hujjat"]):
                return "clarify:desktop_file"

        if clean.startswith("@"):
            return f"clarify:recipient:{clean}"

        # Default fallback
        return f"clarify:{low.replace(' ', '_')}"

    def _internal_resolve(
        self,
        orig_query: str,
        callback_data: str,
        ambiguity_type: str,
        intent: str,
    ) -> Dict[str, Any]:
        """Internal rule-based resolver fallback if NLP engine is unavailable."""
        cb = callback_data.strip().lower()

        if cb == "clarify:ram":
            return {
                "status": "resolved",
                "intent": "INQUIRE_STORAGE",
                "action": "get_system_status",
                "resolved_query": "Operativka (RAM) bo'sh joyini ko'rsat",
                "kwargs": {"metric": "ram"},
                "message": "Tezkor xotira (RAM) tahlili tayyorlanmoqda.",
            }
        elif cb == "clarify:disk":
            return {
                "status": "resolved",
                "intent": "INQUIRE_STORAGE",
                "action": "get_system_status",
                "resolved_query": "Diskdagi (SSD) bo'sh joyni ko'rsat",
                "kwargs": {"metric": "disk"},
                "message": "Diskdagi bo'sh joy tekshirilmoqda.",
            }
        elif cb == "clarify:storage_both":
            return {
                "status": "resolved",
                "intent": "INQUIRE_STORAGE",
                "action": "get_system_status",
                "resolved_query": "RAM va Disk xotira holatini ko'rsat",
                "kwargs": {"metric": "both"},
                "message": "RAM va Disk xotira holatlari to'liq ko'rsatilmoqda.",
            }
        elif cb == "clarify:wallpaper":
            return {
                "status": "resolved",
                "intent": "GET_WALLPAPER",
                "action": "send_wallpaper_image",
                "resolved_query": "Ish stoli fon rasmini (Wallpaper) yubor",
                "kwargs": {"type": "wallpaper"},
                "message": "Ish stoli fon rasmi olinib, yuborilmoqda.",
            }
        elif cb == "clarify:desktop_file":
            return {
                "status": "resolved",
                "intent": "GET_DESKTOP_FILE",
                "action": "list_desktop_files",
                "resolved_query": "Ish stoli (Desktop) papkasidagi fayllarni ko'rsat",
                "kwargs": {"type": "desktop_file"},
                "message": "Ish stolidagi fayllar ro'yxati tekshirilmoqda.",
            }
        elif cb == "clarify:recent_contacts":
            return {
                "status": "resolved",
                "intent": "LIST_RECENT_CONTACTS",
                "action": "list_recent_contacts",
                "resolved_query": "So'nggi kontaktlarni ko'rsat",
                "kwargs": {},
                "message": "So'nggi muloqot qilingan kontaktlar ro'yxati olinmoqda.",
            }
        elif cb.startswith("clarify:recipient:"):
            rec = callback_data.split(":", 2)[2]
            return {
                "status": "resolved",
                "intent": "SEND_MESSAGE",
                "action": "send_user_telegram_message",
                "resolved_query": f"{rec} ga xabar yubor",
                "kwargs": {"recipient": rec},
                "message": f"Qabul qiluvchi {rec} tanlandi.",
            }

        return {
            "status": "resolved",
            "intent": intent or "DIRECT_COMMAND",
            "action": "process_command",
            "resolved_query": f"{orig_query} ({callback_data})",
            "kwargs": {"choice": callback_data},
            "message": "Tanlov qabul qilindi.",
        }


# =============================================================================
# Global Singleton
# =============================================================================

_DEFAULT_DIALOGUE_MANAGER: Optional[JarvisDialogueManager] = None


def get_dialogue_manager(state_file: Optional[Union[str, Path]] = None) -> JarvisDialogueManager:
    """Retrieve or instantiate default JarvisDialogueManager singleton.

    Args:
        state_file: Optional path override for state storage.

    Returns:
        JarvisDialogueManager instance.
    """
    global _DEFAULT_DIALOGUE_MANAGER
    if _DEFAULT_DIALOGUE_MANAGER is None or state_file is not None:
        _DEFAULT_DIALOGUE_MANAGER = JarvisDialogueManager(state_file=state_file)
    return _DEFAULT_DIALOGUE_MANAGER
