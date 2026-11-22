"""
JARVIS Omni-Knowledge & Semantic Desktop/Telegram Indexer
=========================================================
Provides comprehensive, unified knowledge of the user's host environment:
1. PC Applications: Start Menu shortcuts, Program Files, installed apps index.
2. PC User Files: Desktop, Documents, Downloads, Projects, Media files index.
3. Telegram Direct Chats: All 1-on-1 conversations, usernames, phones, last messages.
4. Telegram Saved Messages: Notes, links, test codes, personal reminders.
5. Telegram Groups & Channels: Active study/work groups and channels.

Thread-safe, non-blocking asynchronous background synchronization with atomic caching.
"""

import os
import sys
import re
import json
import time
import uuid
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("JarvisOmniKnowledge")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-OMNI-KNOWLEDGE] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
INDEX_DIR = BASE_DIR / "knowledge_indexes"
INDEX_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# 1. PC Installed Applications Indexer
# =============================================================================

class PCAppsIndexer:
    """Scans Windows Start Menu, Program Files, and AppData to index all installed apps."""

    CACHE_FILE = INDEX_DIR / "apps_index.json"

    COMMON_START_MENUS = [
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"),
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    ]

    COMMON_APP_DIRS = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    ]

    @classmethod
    def scan_apps(cls) -> List[Dict[str, Any]]:
        """Scan all shortcuts and common directories to discover installed applications."""
        apps: Dict[str, Dict[str, Any]] = {}

        # 1. Start Menu Shortcuts via win32com
        shell = None
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
        except Exception as e:
            logger.debug(f"win32com Dispatch not available: {e}")

        for sm_dir in cls.COMMON_START_MENUS:
            if not sm_dir.exists():
                continue
            for lnk in sm_dir.rglob("*.lnk"):
                try:
                    app_name = lnk.stem
                    # Ignore uninstallation, help, website shortcuts
                    low_name = app_name.lower()
                    if any(w in low_name for w in ["uninstall", "o'chirish", "help", "readme", "documentation", "website"]):
                        continue

                    target_path = ""
                    if shell:
                        try:
                            shortcut = shell.CreateShortCut(str(lnk))
                            target_path = shortcut.TargetPath or ""
                        except Exception:
                            target_path = ""

                    if not target_path or not os.path.exists(target_path):
                        target_path = str(lnk)

                    key = low_name
                    if key not in apps or target_path.lower().endswith(".exe"):
                        apps[key] = {
                            "name": app_name,
                            "path": target_path,
                            "shortcut": str(lnk),
                            "is_exe": target_path.lower().endswith(".exe"),
                            "category": cls._categorize_app(app_name, target_path)
                        }
                except Exception:
                    continue

        # 2. LocalAppData and ProgramFiles Standalone Executables
        for app_dir in cls.COMMON_APP_DIRS:
            if not app_dir.exists():
                continue
            try:
                for exe in app_dir.glob("*/*.exe"):
                    exe_name = exe.stem.lower()
                    if exe_name not in apps and not any(w in exe_name for w in ["unins", "setup", "update", "helper", "crash"]):
                        apps[exe_name] = {
                            "name": exe.stem,
                            "path": str(exe),
                            "shortcut": "",
                            "is_exe": True,
                            "category": cls._categorize_app(exe.stem, str(exe))
                        }
            except Exception:
                pass

        results = list(apps.values())
        cls._save_cache(results)
        logger.info(f"PC Applications indexed successfully: {len(results)} apps found.")
        return results

    @classmethod
    def _categorize_app(cls, name: str, path: str) -> str:
        s = f"{name} {path}".lower()
        if any(w in s for w in ["chrome", "edge", "firefox", "browser", "opera", "brave"]):
            return "browser"
        if any(w in s for w in ["telegram", "discord", "whatsapp", "skype", "slack", "zoom", "teams"]):
            return "communication"
        if any(w in s for w in ["code", "visual studio", "python", "pycharm", "sublime", "git", "terminal", "cmd", "powershell"]):
            return "developer"
        if any(w in s for w in ["word", "excel", "powerpoint", "calc", "writer", "libreoffice", "notepad", "pdf"]):
            return "office"
        if any(w in s for w in ["vlc", "player", "music", "spotify", "video", "media"]):
            return "media"
        if any(w in s for w in ["photoshop", "figma", "illustrator", "paint", "gimp", "canva"]):
            return "design"
        if any(w in s for w in ["game", "steam", "hl", "cs", "dota"]):
            return "games"
        return "system"

    @classmethod
    def _save_cache(cls, data: List[Dict[str, Any]]):
        try:
            tmp = cls.CACHE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(cls.CACHE_FILE)
        except Exception as e:
            logger.error(f"Error saving apps cache: {e}")

    @classmethod
    def load_cache(cls) -> List[Dict[str, Any]]:
        if cls.CACHE_FILE.exists():
            try:
                with open(cls.CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return cls.scan_apps()

    @classmethod
    def find_app(cls, query: str) -> Optional[Dict[str, Any]]:
        """Find an installed app by fuzzy name matching."""
        q = (query or "").lower().strip()
        q = re.sub(r'\b(?:dasturi|dasturini|dasturni|ilova|ilovasi|program|app)\b', '', q).strip()
        if not q:
            return None

        apps = cls.load_cache()
        # 1. Exact match
        for a in apps:
            if a["name"].lower() == q or Path(a["path"]).stem.lower() == q:
                return a
        # 2. Starts with or substring
        for a in apps:
            if a["name"].lower().startswith(q) or q in a["name"].lower():
                return a
        for a in apps:
            if q in Path(a["path"]).stem.lower():
                return a
        return None


# =============================================================================
# 2. PC User Files Indexer
# =============================================================================

class PCFilesIndexer:
    """Scans and indexes user files across Desktop, Documents, Downloads, and Projects."""

    CACHE_FILE = INDEX_DIR / "files_index.json"

    EXT_CATEGORIES = {
        "document": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".epub"],
        "spreadsheet": [".xls", ".xlsx", ".csv", ".tsv"],
        "presentation": [".ppt", ".pptx"],
        "code": [".py", ".js", ".ts", ".html", ".css", ".json", ".dart", ".cpp", ".java", ".sql", ".sh", ".bat", ".ps1"],
        "photo": [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".svg", ".ico"],
        "audio": [".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"],
        "video": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
        "archive": [".zip", ".rar", ".7z", ".tar", ".gz"]
    }

    EXCLUDE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode",
        "appdata", "local settings", "temp", "tmp", "caches"
    }

    @classmethod
    def scan_files(cls) -> List[Dict[str, Any]]:
        """Scan key user workspaces and index file metadata."""
        user_home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
        downloads_base = BASE_DIR / "downloads"

        search_roots = [
            user_home / "OneDrive" / "Desktop",
            user_home / "Desktop",
            user_home / "Documents",
            user_home / "Downloads",
            user_home / "Pictures",
            user_home / "Videos",
            user_home / "Music",
            downloads_base,
            user_home / ".gemini" / "antigravity" / "scratch"
        ]

        indexed_files = []
        seen_paths = set()

        for root_dir in search_roots:
            if not root_dir.exists():
                continue
            for root, dirs, files in os.walk(root_dir):
                # Filter out excluded noisy directories
                dirs[:] = [d for d in dirs if d.lower() not in cls.EXCLUDE_DIRS and not d.startswith(".")]

                depth = len(Path(root).relative_to(root_dir).parts)
                if depth >= 2:
                    dirs[:] = []

                for f in files:
                    if f.startswith("~$") or f.startswith("."):
                        continue
                    p = Path(root) / f
                    p_str = str(p)
                    if p_str in seen_paths:
                        continue
                    seen_paths.add(p_str)

                    ext = p.suffix.lower()
                    cat = "other"
                    for c_name, exts in cls.EXT_CATEGORIES.items():
                        if ext in exts:
                            cat = c_name
                            break

                    try:
                        stat = p.stat()
                        indexed_files.append({
                            "name": p.name,
                            "stem": p.stem,
                            "path": p_str,
                            "extension": ext,
                            "category": cat,
                            "size_kb": round(stat.st_size / 1024, 1),
                            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
                        })
                    except Exception:
                        continue

        cls._save_cache(indexed_files)
        logger.info(f"PC User Files indexed successfully: {len(indexed_files)} files cataloged.")
        return indexed_files

    @classmethod
    def _save_cache(cls, data: List[Dict[str, Any]]):
        try:
            tmp = cls.CACHE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(cls.CACHE_FILE)
        except Exception as e:
            logger.error(f"Error saving files cache: {e}")

    @classmethod
    def load_cache(cls) -> List[Dict[str, Any]]:
        if cls.CACHE_FILE.exists():
            try:
                with open(cls.CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return cls.scan_files()

    @classmethod
    def search_files(cls, query: str, category: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Search indexed user files by keyword tokens."""
        q = (query or "").lower().strip()
        tokens = [t for t in re.split(r'[\s_\-\.]+', q) if len(t) > 1 and t not in ["fayl", "faylini", "hujjat", "top", "qidir"]]
        if not tokens:
            return []

        all_files = cls.load_cache()
        results = []

        for f in all_files:
            if category and f["category"] != category:
                continue
            name_lower = f["name"].lower()
            if all(t in name_lower for t in tokens):
                results.append(f)
                if len(results) >= limit:
                    break

        return results


# =============================================================================
# 3. Telegram Omni-Knowledge Indexer (Chats & Saved Messages)
# =============================================================================

class TelegramOmniIndexer:
    """Extracts, indexes, and maintains semantic knowledge of Telegram personal chats, saved messages, and contacts."""

    CACHE_FILE = INDEX_DIR / "telegram_knowledge.json"

    @classmethod
    def index_telegram_knowledge(cls, direct_limit: int = 35, saved_limit: int = 50) -> Dict[str, Any]:
        """Deep index of Telegram dialogs, contacts, and Saved Messages via Telethon."""
        try:
            from jarvis_user_telegram import JarvisUserTelegram, TELETHON_AVAILABLE
            if not TELETHON_AVAILABLE:
                logger.warning("Telethon not available for Telegram indexing.")
                return cls.load_cache()
        except Exception as e:
            logger.error(f"Cannot import JarvisUserTelegram: {e}")
            return cls.load_cache()

        knowledge = {
            "last_synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "direct_chats": [],
            "saved_messages": [],
            "key_contacts": [],
            "groups_and_channels": []
        }

        async def _run():
            from telethon.tl.functions.contacts import GetContactsRequest
            from telethon.tl.types import User, Channel, Chat

            async with JarvisUserTelegram.session_scope() as c:
                # 1. Index All Telegram Contacts
                try:
                    contacts_res = await c(GetContactsRequest(hash=0))
                    for u in contacts_res.users:
                        first = u.first_name or ""
                        last = u.last_name or ""
                        full_name = f"{first} {last}".strip() or "Noma'lum"
                        uname = f"@{u.username}" if u.username else ""
                        knowledge["key_contacts"].append({
                            "id": u.id,
                            "name": full_name,
                            "username": uname,
                            "phone": u.phone or "",
                            "mutual": getattr(u, "mutual_contact", False)
                        })
                except Exception as c_err:
                    logger.warning(f"Error fetching contacts list: {c_err}")

                # 2. Index All Dialogs (Direct Chats & Groups)
                try:
                    dialogs = await c.get_dialogs(limit=direct_limit)
                    for d in dialogs:
                        last_text = d.message.text if d.message and d.message.text else ("<Media / Fayl>" if d.message and d.message.media else "")
                        d_info = {
                            "id": d.id,
                            "name": d.name,
                            "unread_count": d.unread_count,
                            "last_message": last_text[:120],
                            "date": str(d.date) if d.date else "",
                            "is_user": d.is_user,
                            "is_group": d.is_group,
                            "is_channel": d.is_channel
                        }

                        if d.is_user and d.id != (await c.get_me()).id:
                            username = getattr(d.entity, "username", None)
                            d_info["username"] = f"@{username}" if username else ""
                            d_info["phone"] = getattr(d.entity, "phone", "")
                            # Extract last 3 conversation snippets
                            history = []
                            try:
                                async for m in c.iter_messages(d.entity, limit=3):
                                    sender_label = "Siz" if m.out else d.name
                                    m_text = m.text or ("<Media>" if m.media else "")
                                    if m_text:
                                        history.append(f"{sender_label}: {m_text[:100]}")
                            except Exception:
                                pass
                            d_info["recent_history"] = list(reversed(history))
                            knowledge["direct_chats"].append(d_info)
                        else:
                            knowledge["groups_and_channels"].append(d_info)
                except Exception as d_err:
                    logger.warning(f"Error indexing dialogs: {d_err}")

                # 3. Index Saved Messages ('me')
                try:
                    saved_items = []
                    async for m in c.iter_messages("me", limit=saved_limit):
                        text = m.text or ""
                        if not text and m.media:
                            text = f"<Saqlangan media / fayl: {type(m.media).__name__}>"

                        has_links = bool(re.search(r'https?://[^\s]+', text))
                        is_test_or_code = bool(re.search(r'\b(?:kod|test|rasch|model|javob|kalit|\d{4,6})\b', text, re.IGNORECASE))

                        saved_items.append({
                            "id": m.id,
                            "date": str(m.date),
                            "text": text,
                            "has_links": has_links,
                            "is_test_or_code": is_test_or_code,
                            "snippet": text[:150]
                        })
                    knowledge["saved_messages"] = saved_items
                except Exception as s_err:
                    logger.warning(f"Error indexing Saved Messages: {s_err}")

                return knowledge

        try:
            res = JarvisUserTelegram._run_sync(_run())
            cls._save_cache(res)
            logger.info(
                f"Telegram Knowledge indexed: {len(res['direct_chats'])} direct chats, "
                f"{len(res['saved_messages'])} saved messages, {len(res['key_contacts'])} contacts."
            )
            return res
        except Exception as e:
            logger.error(f"Error during Telegram indexing: {e}")
            return cls.load_cache()

    @classmethod
    def _save_cache(cls, data: Dict[str, Any]):
        try:
            tmp = cls.CACHE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(cls.CACHE_FILE)
        except Exception as e:
            logger.error(f"Error saving telegram knowledge cache: {e}")

    @classmethod
    def load_cache(cls) -> Dict[str, Any]:
        if cls.CACHE_FILE.exists():
            try:
                with open(cls.CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "last_synced_at": None,
            "direct_chats": [],
            "saved_messages": [],
            "key_contacts": [],
            "groups_and_channels": []
        }

    @classmethod
    def search_contacts_and_chats(cls, query: str) -> List[Dict[str, Any]]:
        """Search across Telegram direct chats and contact list."""
        q = (query or "").lower().strip().replace("@", "")
        if not q:
            return []

        data = cls.load_cache()
        results = []
        seen_ids = set()

        for c in data.get("direct_chats", []):
            name = c.get("name", "").lower()
            uname = c.get("username", "").lower().replace("@", "")
            if q in name or q in uname:
                results.append(c)
                seen_ids.add(c["id"])

        for k in data.get("key_contacts", []):
            if k["id"] in seen_ids:
                continue
            name = k.get("name", "").lower()
            uname = k.get("username", "").lower().replace("@", "")
            if q in name or q in uname:
                results.append(k)

        return results

    @classmethod
    def search_saved_messages(cls, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search user's 'Saved Messages' for notes, codes, links, or texts."""
        q = (query or "").lower().strip()
        tokens = [t for t in q.split() if len(t) > 1 and t not in ["saved", "messages", "saqlangan", "xabardagi", "xabar"]]
        if not tokens:
            return []

        data = cls.load_cache()
        matches = []
        for s in data.get("saved_messages", []):
            txt = s.get("text", "").lower()
            if all(t in txt for t in tokens):
                matches.append(s)
                if len(matches) >= limit:
                    break
        return matches


# =============================================================================
# 4. Unified Omni-Knowledge Engine
# =============================================================================

class JarvisOmniKnowledge:
    """Unified Facade aggregating Apps, Files, Telegram Direct Chats, and Saved Messages."""

    _instance = None
    _lock = threading.RLock()

    def __init__(self):
        self.base_dir = BASE_DIR
        self._is_syncing = False
        self._last_full_sync = 0.0

    @classmethod
    def get_instance(cls) -> "JarvisOmniKnowledge":
        with cls._lock:
            if cls._instance is None:
                cls._instance = JarvisOmniKnowledge()
            return cls._instance

    def sync_all(self, force: bool = False, async_bg: bool = True):
        """Trigger background synchronization of Apps, Files, and Telegram knowledge."""
        now = time.time()
        if not force and (now - self._last_full_sync < 600):  # 10 minutes cache
            return

        if self._is_syncing:
            return

        def _worker():
            with self._lock:
                self._is_syncing = True
            logger.info("Starting complete Omni-Knowledge indexing (Apps, Files, Telegram)...")
            try:
                PCAppsIndexer.scan_apps()
                PCFilesIndexer.scan_files()
                TelegramOmniIndexer.index_telegram_knowledge()
                self._last_full_sync = time.time()
                logger.info("Omni-Knowledge background synchronization completed successfully.")
            except Exception as e:
                logger.error(f"Error during Omni-Knowledge sync: {e}")
            finally:
                with self._lock:
                    self._is_syncing = False

        if async_bg:
            t = threading.Thread(target=_worker, name="JARVIS-OmniSyncWorker", daemon=True)
            t.start()
        else:
            _worker()

    def omni_search(self, query: str) -> Dict[str, Any]:
        """Perform search across all indexed desktop & Telegram entities."""
        q = (query or "").strip()
        return {
            "query": q,
            "apps": [PCAppsIndexer.find_app(q)] if PCAppsIndexer.find_app(q) else [],
            "files": PCFilesIndexer.search_files(q, limit=5),
            "telegram_chats": TelegramOmniIndexer.search_contacts_and_chats(q),
            "saved_messages": TelegramOmniIndexer.search_saved_messages(q, limit=3)
        }

    def resolve_contact(self, name_or_username: str) -> Optional[Dict[str, Any]]:
        """Resolve a contact or chat from indexed Telegram knowledge."""
        results = TelegramOmniIndexer.search_contacts_and_chats(name_or_username)
        return results[0] if results else None

    def get_app(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Locate an application on the PC."""
        return PCAppsIndexer.find_app(app_name)

    def find_file(self, file_name: str) -> Optional[Dict[str, Any]]:
        """Find an indexed file on the PC."""
        res = PCFilesIndexer.search_files(file_name, limit=1)
        return res[0] if res else None

    def find_saved_note(self, keyword: str) -> Optional[Dict[str, Any]]:
        """Retrieve relevant note from Saved Messages."""
        res = TelegramOmniIndexer.search_saved_messages(keyword, limit=1)
        return res[0] if res else None


def get_omni_knowledge() -> JarvisOmniKnowledge:
    """Convenience getter for singleton Omni-Knowledge engine."""
    return JarvisOmniKnowledge.get_instance()


if __name__ == "__main__":
    print("=== TESTING OMNI-KNOWLEDGE ENGINE ===")
    omni = get_omni_knowledge()
    omni.sync_all(force=True, async_bg=False)
    
    print("\n[SEARCH: Xurshid]")
    print(json.dumps(omni.omni_search("xurshid"), indent=2, ensure_ascii=False))

    print("\n[SEARCH: Chrome]")
    print(json.dumps(omni.omni_search("chrome"), indent=2, ensure_ascii=False))

    print("\n[SEARCH: Kimyo]")
    print(json.dumps(omni.omni_search("kimyo"), indent=2, ensure_ascii=False))
