"""
JARVIS / Monika Telegram Mini App (TMA) Backend Server
======================================================
Asynchronous aiohttp web server hosting the Monika WebApp interface and
providing secure REST APIs for remote control, monitoring, and Antigravity.

Port: 8088
Features:
- Full CORS support for Telegram WebApp SDK and browser sandboxes
- System telemetry (CPU, RAM, Disk, Battery, Lock status, Security mode)
- Live desktop screenshot & webcam snapshot feeds (JPEG)
- Virtual remote touchpad & mouse control (click, double click, right click, relative move)
- Remote keyboard input & shortcut triggers (Win+R, Alt+Tab, Ctrl+C, etc.)
- Voice synthesis (PC speaker TTS) & instant audio mute
- Antigravity projects workspace manager & tasks delegation
- Personal memory, owner profile & goals
- Personal Telegram unread message triage digest
"""

import sys
import os
import io
import re
import time
import json
import logging
import asyncio
import hmac
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any

from aiohttp import web
from PIL import Image

# Ensure current directory is on sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Setup logging
logger = logging.getLogger("JarvisWebApp")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-WEBAPP] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Import core subsystems (available in repo)
from jarvis_system import JarvisSystem
from jarvis_desktop import JarvisDesktop
from jarvis_tracer import JarvisTracer
import config

# Subsystems with graceful fallback for standalone modular operation
try:
    from jarvis_voice import JarvisVoice
except ImportError:
    class JarvisVoice:
        def speak(self, text: str) -> None: pass
        def stop_speaking(self) -> None: pass

try:
    from jarvis_antigravity import JarvisAntigravity
except ImportError:
    class JarvisAntigravity:
        def list_projects(self) -> List[Any]: return []
        def create_project(self, name: str, desc: str) -> Dict[str, Any]: return {"name": name, "description": desc}
        def get_delegation_history(self) -> List[Any]: return []

try:
    from jarvis_memory import JarvisMemory
except ImportError:
    class JarvisMemory:
        data: Dict[str, Any] = {}
        def get_owner(self) -> str: return "Azizbek Maripov"
        def get_location(self) -> str: return "Toshkent, O'zbekiston"
        def get_plans(self) -> List[str]: return []

try:
    from jarvis_vision import JarvisVision
except ImportError:
    JarvisVision = None

try:
    from jarvis_user_telegram import JarvisUserTelegram
except ImportError:
    class JarvisUserTelegram:
        @staticmethod
        def generate_unread_summary(*args, **kwargs) -> List[Any]: return []
        @staticmethod
        async def generate_unread_summary_async(*args, **kwargs) -> List[Any]: return []
        @staticmethod
        def send_telegram_message(*args, **kwargs) -> Dict[str, Any]: return {"success": False, "error": "Telegram client offline"}

try:
    from jarvis_pc_sensor import JarvisPCSensor
except ImportError:
    class JarvisPCSensor:
        def __init__(self, *args, **kwargs): pass
        def get_active_window(self) -> Dict[str, Any]: return {"title": "Desktop", "process": "explorer.exe"}
        def get_sensory_context(self) -> Dict[str, Any]: return {}
        def wake_lock_screen(self) -> Dict[str, Any]: return {"success": True}

try:
    from jarvis_tool_registry import JarvisToolRegistry
except ImportError:
    class JarvisToolRegistry:
        def __init__(self, *args, **kwargs): pass
        def get_system_status(self) -> Dict[str, Any]: return {"cpu": 0, "ram": 0, "is_locked": False}
        def get_gemini_tools(self) -> List[Any]: return []
        def dispatch(self, *args, **kwargs) -> Dict[str, Any]: return {"status": "ok"}
        def search_files(self, *args, **kwargs) -> Dict[str, Any]: return {"results": []}
        def launch_application(self, *args, **kwargs) -> Dict[str, Any]: return {"status": "ok"}
        def close_application(self, *args, **kwargs) -> Dict[str, Any]: return {"status": "ok"}
        def take_screenshot(self, *args, **kwargs) -> Dict[str, Any]: return {"status": "ok"}

# Try importing JarvisConcierge with safe fallback if still being finalized
try:
    from jarvis_concierge import JarvisConcierge
    _real_concierge = JarvisConcierge()
    logger.info("Successfully loaded JarvisConcierge module.")
except Exception as _e:
    _real_concierge = None
    logger.info(f"JarvisConcierge module not directly loaded ({_e}). Running with built-in resilient WebAppConciergeManager.")

# Ensure single instance execution across OS
def _enforce_single_webapp_instance():
    """Ensure port 8088 is free by terminating any stale process holding it."""
    try:
        import psutil
        my_pid = os.getpid()
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == 8088 and conn.status == 'LISTEN':
                if conn.pid and conn.pid != my_pid:
                    try:
                        p = psutil.Process(conn.pid)
                        logger.warning(f"Freeing port 8088: terminating stale listener PID {conn.pid} ({p.name()})...")
                        p.kill()
                        p.wait(timeout=2)
                    except Exception:
                        pass
    except Exception as e:
        logger.debug(f"Single instance check note: {e}")

# Note: _enforce_single_webapp_instance() is called in main() to avoid killing the running server during tests/imports

# Global state & singleton subsystem instances
WEBAPP_DIR = BASE_DIR / "webapp"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
HEARTBEAT_FILE = BASE_DIR / "heartbeat.json"

tracer = JarvisTracer()
system = JarvisSystem()
desktop = JarvisDesktop(failsafe=False)
voice = JarvisVoice()
antigravity = JarvisAntigravity()
memory = JarvisMemory()
sensor = JarvisPCSensor(desktop_controller=desktop)
tool_registry = JarvisToolRegistry(
    sensor=sensor,
    desktop=desktop,
    system=system,
    memory=memory,
    base_dir=BASE_DIR
)
vision: Optional[JarvisVision] = None


class WebAppConciergeManager:
    """Manages AI Concierge (Kotiba Rejimi) state, inbox, and bridge to JarvisConcierge."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.state_file = base_dir / "user_concierge_state.json"
        self.inbox_file = base_dir / "concierge_inbox.json"
        self._enabled = True
        self._away_state = False
        self._load_state()

    def _get_active_instance(self):
        global _real_concierge
        if _real_concierge is not None:
            return _real_concierge
        try:
            from jarvis_concierge import JarvisConcierge
            if hasattr(JarvisConcierge, "get_instance"):
                _real_concierge = JarvisConcierge.get_instance()
            else:
                _real_concierge = JarvisConcierge()
            logger.info("Dynamically linked finalized JarvisConcierge instance.")
            return _real_concierge
        except Exception:
            return None

    def _load_state(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._enabled = data.get("enabled", True)
                    self._away_state = data.get("away_state", False)
            except Exception as e:
                logger.warning(f"Failed to load concierge state: {e}")
        else:
            self._save_state()

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self._enabled,
                    "away_state": self._away_state,
                    "updated_at": time.time()
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save concierge state: {e}")

    def is_enabled(self) -> bool:
        inst = self._get_active_instance()
        if inst is not None:
            if hasattr(inst, "is_away_mode_active"):
                try: return bool(inst.is_away_mode_active())
                except Exception: pass
            if hasattr(inst, "get_concierge_status"):
                try:
                    st = inst.get_concierge_status()
                    return bool(st.get("enabled", st.get("toggle_enabled", False)))
                except Exception: pass
            if hasattr(inst, "is_enabled"):
                try: return bool(inst.is_enabled())
                except Exception: pass
            if hasattr(inst, "enabled"):
                try: return bool(inst.enabled)
                except Exception: pass
        return self._enabled

    def is_away(self) -> bool:
        inst = self._get_active_instance()
        if inst is not None:
            if hasattr(inst, "is_away_mode_active"):
                try: return bool(inst.is_away_mode_active())
                except Exception: pass
            if hasattr(inst, "is_away"):
                try: return bool(inst.is_away())
                except Exception: pass
            if hasattr(inst, "away_state"):
                try: return bool(inst.away_state)
                except Exception: pass
        return self._away_state

    def get_status(self) -> Dict[str, Any]:
        inst = self._get_active_instance()
        if inst is not None:
            if hasattr(inst, "get_concierge_status"):
                try:
                    st = inst.get_concierge_status()
                    inbox = inst.get_inbox_notes() if hasattr(inst, "get_inbox_notes") else self.get_inbox()
                    return {
                        "status": "success",
                        "enabled": bool(st.get("enabled", st.get("toggle_enabled", False))),
                        "toggle_enabled": bool(st.get("toggle_enabled", False)),
                        "away_state": bool(st.get("away_mode_active", False)),
                        "unread_notes_count": st.get("total_inbox_notes", len(inbox)),
                        "active_sessions_count": st.get("active_sessions_count", 0),
                        "total_notes": st.get("total_inbox_notes", len(inbox)),
                        "security_status": st.get("security_status", "Active")
                    }
                except Exception as e:
                    logger.warning(f"JarvisConcierge.get_concierge_status error: {e}")
            elif hasattr(inst, "get_status"):
                try:
                    st = inst.get_status()
                    if isinstance(st, dict):
                        st.setdefault("status", "success")
                        st.setdefault("enabled", self.is_enabled())
                        st.setdefault("away_state", self.is_away())
                        st.setdefault("unread_notes_count", self.get_unread_count())
                        st.setdefault("active_sessions_count", 0)
                        return st
                except Exception as e:
                    logger.warning(f"JarvisConcierge.get_status error: {e}")

        inbox = self.get_inbox()
        unread = len([m for m in inbox if not m.get("read", False)])
        return {
            "status": "success",
            "enabled": self.is_enabled(),
            "away_state": self.is_away(),
            "unread_notes_count": unread,
            "active_sessions_count": 0,
            "total_notes": len(inbox)
        }

    def toggle(self, target_state: Optional[bool] = None) -> bool:
        inst = self._get_active_instance()
        if inst is not None and hasattr(inst, "toggle_concierge_mode"):
            try:
                res = inst.toggle_concierge_mode(enabled=target_state)
                new_state = bool(res.get("toggle_enabled", res.get("enabled", False)))
                self._enabled = new_state
                self._save_state()
                return new_state
            except Exception as e:
                logger.warning(f"toggle_concierge_mode error: {e}")

        if target_state is None:
            new_state = not self.is_enabled()
        else:
            new_state = bool(target_state)

        if inst is not None:
            if hasattr(inst, "toggle"):
                try:
                    inst.toggle(enabled=new_state)
                except TypeError:
                    try: inst.toggle()
                    except Exception: pass
            elif hasattr(inst, "set_enabled"):
                try: inst.set_enabled(new_state)
                except Exception: pass
            if hasattr(inst, "enabled"):
                try: inst.enabled = new_state
                except Exception: pass

        self._enabled = new_state
        self._save_state()
        return self._enabled

    def get_inbox(self) -> List[Dict[str, Any]]:
        inst = self._get_active_instance()
        if inst is not None and hasattr(inst, "get_inbox_notes"):
            try:
                res = inst.get_inbox_notes(limit=50)
                if isinstance(res, list):
                    for n in res:
                        if "text" not in n:
                            n["text"] = n.get("note_text") or n.get("note") or ""
                        if "sender" not in n:
                            n["sender"] = n.get("sender_name") or "Noma'lum"
                        if "time" not in n:
                            n["time"] = n.get("date_str") or (n.get("timestamp") and time.strftime("%H:%M", time.localtime(n["timestamp"]))) or "--:--"
                    return res
            except Exception as e:
                logger.warning(f"get_inbox_notes error: {e}")

        if inst is not None:
            if hasattr(inst, "get_inbox"):
                try:
                    res = inst.get_inbox()
                    if isinstance(res, list): return res
                except Exception: pass
            if hasattr(inst, "inbox") and isinstance(inst.inbox, list):
                return inst.inbox
            if hasattr(inst, "get_notes"):
                try:
                    res = inst.get_notes()
                    if isinstance(res, list): return res
                except Exception: pass

        if self.inbox_file.exists():
            try:
                with open(self.inbox_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict) and "notes" in data:
                        return data["notes"]
            except Exception as e:
                logger.warning(f"Failed reading concierge inbox file: {e}")
                return []
        else:
            seed = [
                {
                    "id": "note-1",
                    "sender": "Jamshid Sodiqov",
                    "username": "@Jamsh1d_Sodiqov",
                    "phone": "+998933174277",
                    "time": "14:20",
                    "text": "Salom Azizbek! Spektroskopiya tahlillari hisoboti tayyor bo'ldi. Bo'sh vaqtingizda ko'rib chiqsangiz yaxshi bo'lardi.",
                    "read": False,
                    "urgency": "high",
                    "created_at": time.time() - 3600
                }
            ]
            try:
                with open(self.inbox_file, "w", encoding="utf-8") as f:
                    json.dump(seed, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            return seed

    def get_unread_count(self) -> int:
        inbox = self.get_inbox()
        return len([m for m in inbox if not m.get("read", False)])

    def clear_inbox(self) -> bool:
        inst = self._get_active_instance()
        if inst is not None:
            if hasattr(inst, "clear_inbox_notes"):
                try:
                    inst.clear_inbox_notes()
                except Exception as e:
                    logger.warning(f"clear_inbox_notes error: {e}")
            elif hasattr(inst, "clear_inbox"):
                try: inst.clear_inbox()
                except Exception: pass
            elif hasattr(inst, "clear_notes"):
                try: inst.clear_notes()
                except Exception: pass
            if hasattr(inst, "inbox"):
                try: inst.inbox = []
                except Exception: pass

        try:
            with open(self.inbox_file, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.warning(f"Failed clearing concierge inbox file: {e}")
            return False

        try:
            with open(self.inbox_file, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.warning(f"Failed clearing concierge inbox file: {e}")
            return False


concierge_manager = WebAppConciergeManager(BASE_DIR)

current_security_mode = "ARMED"
snooze_until = 0.0


def get_current_security_mode() -> str:
    """Read latest security mode from heartbeat.json or internal state."""
    global current_security_mode, snooze_until
    if snooze_until > 0:
        if time.time() < snooze_until:
            rem = int(snooze_until - time.time())
            mins, secs = divmod(rem, 60)
            return f"SNOOZED ({mins}m {secs}s)"
        else:
            snooze_until = 0.0
            current_security_mode = "ARMED"

    if HEARTBEAT_FILE.exists():
        try:
            with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                mode = data.get("security_mode")
                if mode:
                    current_security_mode = mode
                    return mode
        except Exception:
            pass
    return current_security_mode


def update_security_mode(mode: str, snooze_minutes: int = 30) -> str:
    """Update security mode and reflect in heartbeat file if accessible."""
    global current_security_mode, snooze_until
    mode_upper = mode.upper().strip()
    if mode_upper == "SNOOZED":
        current_security_mode = "SNOOZED"
        snooze_until = time.time() + (snooze_minutes * 60)
    elif mode_upper in ("ARMED", "DISARMED"):
        current_security_mode = mode_upper
        snooze_until = 0.0
    else:
        current_security_mode = mode_upper

    # Update heartbeat.json if it exists
    if HEARTBEAT_FILE.exists():
        try:
            with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                hb = json.load(f)
            hb["security_mode"] = current_security_mode
            hb["is_armed"] = (current_security_mode == "ARMED")
            tmp = HEARTBEAT_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hb, f, indent=2)
            tmp.replace(HEARTBEAT_FILE)
        except Exception as e:
            logger.debug(f"Could not update heartbeat.json: {e}")

    return current_security_mode


# =============================================================================
# CORS & Security Middleware
# =============================================================================
SENSITIVE_PREFIXES = (
    "/v1/",
    "/api/lock",
    "/api/mute",
    "/api/say",
    "/api/command",
    "/api/security",
    "/api/mouse/",
    "/api/keyboard/",
    "/api/projects",
    "/api/traces",
    "/api/screenshot",
    "/api/camera",
    "/api/unread",
    "/api/memory",
    "/api/tasks",
    "/api/safety",
    "/api/confirm",
    "/api/chat",
    "/api/action",
    "/api/omni",
    "/api/screen/",
)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add restricted CORS headers and handle OPTIONS preflight safely."""
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
        except Exception as ex:
            logger.error(f"Unhandled error in handler {request.path}: {ex}", exc_info=True)
            response = web.json_response({"error": str(ex), "status": "error"}, status=500)

    origin = request.headers.get("Origin", "")

    # Check allowed origin strictly by parsed hostname
    is_allowed = False
    if not origin:
        is_allowed = True
    else:
        try:
            parsed = urllib.parse.urlparse(origin)
            origin_host = (parsed.hostname or "").lower()
            current_host = request.host.split(":")[0].lower()
            allowed_exact = {"localhost", "127.0.0.1", current_host}
            allowed_suffixes = (".lhr.life", ".loca.lt", ".trycloudflare.com")
            if origin_host in allowed_exact or any(origin_host.endswith(s) for s in allowed_suffixes):
                is_allowed = True
        except Exception:
            is_allowed = False

    if is_allowed and origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    elif request.path in ["/", "/index.html", "/api/status"]:
        response.headers["Access-Control-Allow-Origin"] = "*"

    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE, HEAD"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Cache-Control, X-API-Key"
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Authenticate and protect sensitive endpoints against unauthorized access with Fail-Closed policy."""
    if request.method == "OPTIONS":
        return await handler(request)

    path = request.path
    is_sensitive = any(path.startswith(prefix) for prefix in SENSITIVE_PREFIXES)

    if is_sensitive:
        expected_key = (getattr(config, "WEBAPP_API_KEY", "") or os.environ.get("WEBAPP_API_KEY", "")).strip()

        # Fail-closed check: Unconfigured or dangerously weak key must immediately reject all requests
        if not expected_key or len(expected_key) < 8:
            logger.error(f"Security halt: WEBAPP_API_KEY is not properly set (must be >= 8 chars). Blocking {path}")
            return web.json_response(
                {"error": {"message": "Server security configuration error: Insecure or unconfigured API key.", "type": "configuration_error"}},
                status=401
            )

        auth_header = request.headers.get("Authorization", "").strip()
        token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        x_key = request.headers.get("X-API-Key", "").strip()
        query_key = request.query.get("api_key", "").strip()

        # Constant-time comparison preventing timing attacks and empty token evaluation
        is_auth = (
            (bool(token) and hmac.compare_digest(token, expected_key)) or
            (bool(x_key) and hmac.compare_digest(x_key, expected_key)) or
            (bool(query_key) and hmac.compare_digest(query_key, expected_key))
        )

        if not is_auth:
            logger.warning(f"Unauthorized access attempt to sensitive endpoint '{path}' from {request.remote}")
            return web.json_response(
                {"error": {"message": "Unauthorized. Valid Bearer API key or X-API-Key required.", "type": "authentication_error"}},
                status=401
            )

    return await handler(request)


# =============================================================================
# Route Handlers
# =============================================================================

async def handle_index(request: web.Request) -> web.Response:
    """Serve the Telegram Mini App HTML frontend."""
    index_file = WEBAPP_DIR / "index.html"
    if not index_file.exists():
        return web.Response(text="<h1>WebApp index.html not found</h1>", content_type="text/html", status=404)
    content = index_file.read_text(encoding="utf-8")
    return web.Response(
        text=content,
        content_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


async def handle_avatar(request: web.Request) -> web.Response:
    """Serve Monika's profile avatar image."""
    avatar_file = WEBAPP_DIR / "monika_avatar.jpg"
    if not avatar_file.exists():
        avatar_file = BASE_DIR / "monika_avatar.jpg"
    if avatar_file.exists():
        return web.FileResponse(avatar_file, headers={"Cache-Control": "public, max-age=86400"})
    return web.Response(status=404)


async def handle_manifest(request: web.Request) -> web.Response:
    """Serve standard W3C Web App Manifest for PWA installation."""
    manifest_file = WEBAPP_DIR / "manifest.json"
    if not manifest_file.exists():
        return web.Response(text="{}", content_type="application/manifest+json", status=404)
    content = manifest_file.read_text(encoding="utf-8")
    return web.Response(
        text=content,
        content_type="application/manifest+json",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        }
    )


async def handle_service_worker(request: web.Request) -> web.Response:
    """Serve Service Worker script with root scope authority."""
    sw_file = WEBAPP_DIR / "sw.js"
    if not sw_file.exists():
        return web.Response(text="// sw.js not found", content_type="application/javascript", status=404)
    content = sw_file.read_text(encoding="utf-8")
    return web.Response(
        text=content,
        content_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


async def handle_favicon(request: web.Request) -> web.Response:
    """Serve favicon.ico."""
    ico_file = WEBAPP_DIR / "favicon.ico"
    if not ico_file.exists():
        ico_file = WEBAPP_DIR / "icons" / "favicon.ico"
    if ico_file.exists():
        return web.FileResponse(ico_file, headers={"Cache-Control": "public, max-age=86400"})
    return web.Response(status=404)


async def handle_pwa_icon(request: web.Request) -> web.Response:
    """Serve standardized PWA icons from webapp/icons/."""
    filename = request.match_info.get("filename", "")
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return web.Response(status=400, text="Invalid icon name")
    icon_path = WEBAPP_DIR / "icons" / filename
    if icon_path.exists() and icon_path.is_file():
        content_type = "image/png"
        if filename.endswith(".ico"):
            content_type = "image/x-icon"
        elif filename.endswith(".svg"):
            content_type = "image/svg+xml"
        return web.FileResponse(
            icon_path,
            headers={
                "Cache-Control": "public, max-age=604800",
                "Content-Type": content_type
            }
        )
    return web.Response(status=404, text="Icon not found")


async def handle_status(request: web.Request) -> web.Response:
    """Return real-time system metrics, battery, active window, volume, lock state, and security status."""
    loop = asyncio.get_running_loop()
    def _collect_stats():
        stats = system.get_system_stats()
        try:
            active_win = sensor.get_active_window()
            stats["active_window"] = active_win.get("title", "Desktop")
            stats["active_process"] = active_win.get("process", active_win.get("process_name", "explorer.exe"))
        except Exception:
            stats["active_window"] = "Noma'lum"
            stats["active_process"] = ""
        try:
            import psutil
            bat = psutil.sensors_battery()
            if bat:
                stats["battery_percent"] = round(bat.percent, 1)
                stats["battery_power_plugged"] = bat.power_plugged
        except Exception:
            pass

        # Live state properties for dynamic UI badges
        sec_status = stats.get("security_status", "Active (Unlocked)")
        stats["is_locked"] = (sec_status == "Locked")
        vol = system.get_volume()
        stats["volume"] = vol.get("volume", 50)
        stats["is_muted"] = vol.get("muted", False)
        return stats

    stats = await loop.run_in_executor(None, _collect_stats)
    mode = get_current_security_mode()
    stats["security_mode"] = mode
    stats["is_armed"] = (mode == "ARMED")
    stats["concierge_enabled"] = concierge_manager.is_enabled()
    stats["concierge_unread_count"] = concierge_manager.get_unread_count()
    return web.json_response(stats)


# =============================================================================
# v3.0 Chat & State-Aware Action Handlers
# =============================================================================

def _process_chat_message(cmd: str, user_id: str) -> str:
    """Intelligent command dispatcher for Mini App chat with Fast-Paths and Gemini FC."""
    raw_q = (cmd or "").strip()
    q = raw_q.lower()

    if not raw_q:
        return "👋 Salom! Men Monika (Jarvis)man. Sizga qanday yordam bera olaman?"

    # -------------------------------------------------------------------------
    # 0. Kotiba Rejimi (AI Concierge) Fast-Path
    # -------------------------------------------------------------------------
    if any(w in q for w in ["kotiba", "concierge"]):
        if any(w in q for w in ["yoq", "faollashtir", "start", "enable"]):
            concierge_manager.toggle(True)
            return "🛡️ <b>Kotiba Rejimi (AI Concierge) yoqildi.</b> Monika siz band bo'lganingizda shaxsiy chatlarga muloyim javob beradi va xabarlarni yozib oladi."
        elif any(w in q for w in ["o'chir", "to'xtat", "pauza", "disable", "stop"]):
            concierge_manager.toggle(False)
            return "⏸️ <b>Kotiba Rejimi pauzaga qo'yildi.</b> Shaxsiy chatlarga avtomatik javob berish to'xtatildi."
        elif any(w in q for w in ["xabarlar", "quti", "inbox", "qaydlar"]):
            inbox = concierge_manager.get_inbox()
            if not inbox:
                return "📭 Kotiba qutisida hozircha yangi xabarlar yo'q."
            lines = [f"📩 <b>Kotiba Qutisidagi Xabarlar ({len(inbox)} ta):</b>\n"]
            for note in inbox[:5]:
                lines.append(f"• <b>{note.get('sender', 'Noma''lum')}</b> ({note.get('time', '--')}): {note.get('text', '')}")
            return "\n".join(lines)
        else:
            st = concierge_manager.get_status()
            en = "FAOL (Yoqilgan)" if st.get("enabled") else "PAUZADA (O'chirilgan)"
            return f"🛡️ <b>Kotiba Rejimi holati:</b> {en}\nQabul qilingan xabarlar soni: {st.get('unread_notes_count', 0)} ta."

    # -------------------------------------------------------------------------
    # 1. Master Audio Control (Volume / Mute)
    # -------------------------------------------------------------------------
    if any(w in q for w in ["ovozni o'chir", "ovozni ochir", "mute", "tinchlik", "jim"]):
        try:
            system.mute_volume(True)
            return "🔇 Ovoz o'chirildi."
        except Exception as e:
            return f"❌ Ovozni o'chirishda xatolik: {e}"

    if any(w in q for w in ["ovozni yoq", "unmute"]):
        try:
            system.mute_volume(False)
            return "🔊 Ovoz yoqildi."
        except Exception as e:
            return f"❌ Ovozni yoqishda xatolik: {e}"

    # -------------------------------------------------------------------------
    # 2. Workstation Screen Lock & Reliable Unlock via Chat (Requirement 2.f)
    # -------------------------------------------------------------------------
    # Lock action: match "qulfla", "lock", "blokla"
    if any(w in q for w in ["qulfla", "lock", "blokla"]) and not any(w in q for w in ["och", "unlock", "uyg'ot", "uygot", "wake", "blokdan chiqar"]):
        try:
            res = system.lock_workstation()
            if isinstance(res, dict) and not res.get("success", True):
                return f"❌ Ekranni qulflashda xatolik: {res.get('message', 'Noma''lum xatolik')}"
            return "🔒 Kompyuter ekrani muvaffaqiyatli qulflandi."
        except Exception as e:
            logger.error(f"Screen lock chat error: {e}")
            return f"❌ Ekranni qulflashda diagnostik xatolik: {e}"

    # Unlock action: match "och", "unlock", "uyg'ot", "wake" targeting screen/PC or standalone
    is_screen_unlock = False
    if any(w in q for w in [
        "ekranni och", "ekran och", "kompyuterni och", "kompyuter och",
        "ekranni unlock", "ekranni uyg'ot", "ekranni uygot", "kompyuterni uyg'ot",
        "blokdan chiqar", "ekranni blokdan chiqar", "unlock screen"
    ]):
        is_screen_unlock = True
    elif q.strip() in ["och", "unlock", "uyg'ot", "uygot", "wake"]:
        is_screen_unlock = True
    elif re.match(r'^(?:monika\s+)?(?:ekranni\s+|kompyuterni\s+)?(?:och|unlock|uyg[\'\'\`\’]?ot|wake)(?:\s+qil)?$', q.strip()):
        is_screen_unlock = True

    if is_screen_unlock:
        try:
            pin = getattr(config, "ADMIN_PIN", "1234")
            res = system.unlock_workstation(pin)
            if isinstance(res, dict) and (not res.get("success", False) or res.get("is_locked", False)):
                err_msg = res.get("error") or res.get("message") or "PIN noto'g'ri yoki ekranni ochib bo'lmadi"
                return f"❌ Ekranni blokdan chiqarishda xatolik: {err_msg}\n\nDiagnostika: Win32 ish stoli sessiyasiga ulanishni va ADMIN_PIN to'g'riligini tekshiring."
            return "🔓 Kompyuter ekrani muvaffaqiyatli uyg'otildi va blokdan chiqarildi."
        except Exception as e:
            logger.error(f"Screen unlock chat error: {e}")
            return f"❌ Ekranni blokdan chiqarishda xatolik: {e}\n\nDiagnostika: Win32 ish stoli sessiyasiga ulanishni va ADMIN_PIN to'g'riligini tekshiring."

    # -------------------------------------------------------------------------
    # 3. Screenshot Capture (Requirement 2.a)
    # -------------------------------------------------------------------------
    if any(w in q for w in ["skrinshot", "screenshot", "ekranni rasmga ol", "ekran surati", "capture screen"]):
        try:
            sc = tool_registry.take_screenshot("live_desktop.jpg")
            if sc.get("status") == "success" or sc.get("success"):
                dims = sc.get("resolution") or (f"{sc.get('width')}x{sc.get('height')}" if sc.get("width") else "1920x1080")
                timestamp = int(time.time())
                expected_key = (getattr(config, "WEBAPP_API_KEY", "") or os.environ.get("WEBAPP_API_KEY", "")).strip()
                api_param = f"&api_key={expected_key}" if expected_key else ""
                return (
                    f"📸 **Kompyuter ekrani surati olindi** ({dims}):\n\n"
                    f"![Ekran surati](/api/screenshot?t={timestamp}{api_param})"
                )
            else:
                err_msg = sc.get("message") or sc.get("error") or "Skrinshot olish imkoni bo'lmadi"
                return (
                    f"❌ **Ekranni suratga olishda xatolik yuz berdi:** {err_msg}\n\n"
                    "Diagnostika: Desktop displey sessiyasiga ulanishda xatolik yuz berdi yoki video drayver ruxsat bermadi."
                )
        except Exception as e:
            logger.error(f"Screenshot capture error: {e}")
            return (
                f"❌ **Ekranni suratga olishda kutilmagan xatolik:** {e}\n\n"
                "Diagnostika: Windows Desktop sessiyasi va PIL/Win32 kutubxonalarini tekshiring."
            )

    # -------------------------------------------------------------------------
    # 4. System Telemetry & Resource Status (Requirement 2.b)
    # -------------------------------------------------------------------------
    if any(w in q for w in ["tizim holati", "status", "resurslar", "telemetriya", "system info", "kompyuter qanday ishlayapti", "holat"]):
        try:
            res = tool_registry.get_system_status()
            stats = res.get("stats", res) if isinstance(res, dict) else {}
            active_win = sensor.get_active_window() if hasattr(sensor, "get_active_window") else {}

            cpu = stats.get("cpu_percent", "--")
            ram = stats.get("ram_percent", "--")
            ram_used = stats.get("ram_used_gb", "--")
            ram_total = stats.get("ram_total_gb", "--")
            disk_free = stats.get("disk_free_gb", "--")
            disk_pct = stats.get("disk_percent", "--")

            batt = stats.get("battery_percent")
            plugged = stats.get("battery_plugged")
            if batt is not None:
                batt_str = f"{batt}% ({'Zaryadlanmoqda' if plugged else 'Batareyada'})"
            else:
                batt_str = "Stasionar PC / Doimiy quvvat"

            active_title = active_win.get("title") or "Noma'lum"
            active_proc = active_win.get("process") or "explorer.exe"

            uptime_sec = stats.get("uptime_seconds", 0)
            if uptime_sec:
                hours = int(uptime_sec // 3600)
                mins = int((uptime_sec % 3600) // 60)
                uptime_str = f"{hours} soat {mins} daqiqa"
            else:
                uptime_str = stats.get("uptime_formatted", "--")

            sec_status = stats.get("security_status") or "Active (Unlocked)"

            return (
                f"💻 **Tizim holati & Resurslar telemetriyasi:**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• **CPU yuklamasi:** {cpu}%\n"
                f"• **Operativ xotira (RAM):** {ram}% ({ram_used} GB / {ram_total} GB)\n"
                f"• **Qattiq disk (C:):** {disk_free} GB bo'sh ({disk_pct}% band)\n"
                f"• **Batareya quvvati:** {batt_str}\n"
                f"• **Faol oyna:** `{active_title}` ({active_proc})\n"
                f"• **Ish vaqti (Uptime):** {uptime_str}\n"
                f"• **Xavfsizlik holati:** {sec_status}"
            )
        except Exception as e:
            logger.error(f"System telemetry error: {e}")
            return (
                f"❌ **Tizim telemetriyasini olishda xatolik:** {e}\n\n"
                "Diagnostika: psutil drayveri yoki sensor holatini tekshiring."
            )

    # -------------------------------------------------------------------------
    # 5. Telegram Triage & Actions (Requirement 2.e)
    # -------------------------------------------------------------------------
    # 5.1 Unread message summary / triage
    if any(w in q for w in ["o'qilmagan xabar", "o'qilmagan xabarlar", "xabarlarni tekshir", "telegram triage", "unread", "yangi xabarlar", "chatlar sarhisobi"]):
        try:
            summary = JarvisUserTelegram.generate_unread_summary(limit_dialogs=20)
            if hasattr(summary, "markdown") and summary.markdown:
                return summary.markdown
            elif isinstance(summary, dict) and summary.get("markdown"):
                return summary["markdown"]
            else:
                return "📬 **Telegram Triage:** Barcha xabarlar o'qilgan, yangi o'qilmagan xabarlar yo'q."
        except Exception as e:
            logger.warning(f"Telegram triage error: {e}")
            return (
                f"⚠️ **Telegram Triage Diagnostikasi:** Telethon foydalanuvchi sessiyasiga ulanib bo'lmadi ({e}).\n\n"
                "Diagnostika: `user_session.session` sessiya fayli mavjud emas yoki Telegram akkaunti avtorizatsiyadan o'tmagan."
            )

    # 5.2 Contact messaging
    m_tg_send = re.search(r'(?:telegramda\s+)?([a-zA-Z0-9_\'`@]+?)(?:ga|ka|qa)?\s+["\']?(.+?)["\']?\s+deb\s+yoz', raw_q, re.IGNORECASE)
    if not m_tg_send:
        m_tg_send = re.search(r'(?:telegramda\s+)?([a-zA-Z0-9_\'`@]+?)(?:ga|ka|qa)?\s+xabar\s+yubor[\s:]+["\']?(.+?)["\']?$', raw_q, re.IGNORECASE)
    if not m_tg_send:
        m_tg_send = re.search(r'send\s+(?:telegram\s+)?message\s+to\s+([a-zA-Z0-9_\'`@]+)[\s:]+(.+)$', raw_q, re.IGNORECASE)

    if m_tg_send:
        tg_target = m_tg_send.group(1).strip()
        tg_text = m_tg_send.group(2).strip().strip("'\"")
        try:
            res = JarvisUserTelegram.send_telegram_message(tg_target, tg_text)
            if isinstance(res, dict) and res.get("success"):
                dest = res.get("target") or tg_target
                return f"✈️ **Telegram xabari muvaffaqiyatli yuborildi.**\n\n• **Qabul qiluvchi:** {dest}\n• **Xabar:** {tg_text}"
            else:
                err = res.get("error") if isinstance(res, dict) else "Noma'lum xatolik"
                return (
                    f"❌ **Telegram xabari yuborilmadi:** {err}\n\n"
                    f"Diagnostika: '{tg_target}' nomli kontakt topilmadi yoki Telethon sessiyasi faol emas."
                )
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")
            return (
                f"❌ **Telegram xabari yuborilmadi:** {e}\n\n"
                "Diagnostika: Telethon sessiyasini va internet aloqasini tekshiring."
            )

    # 5.3 Telegram Saved Messages & Notes lookup
    if any(w in q for w in ["saqlangan xabarlar", "saved messages", "eslatmalarim", "qaydlarim"]):
        try:
            from jarvis_omni_indexer import TelegramOmniIndexer
            kw_m = re.search(r'(?:saqlangan\s+xabarlar(?:dan|imdan)?|saved\s+messages(?:dan)?)\s+(?:haqida\s+)?(.+)', q)
            term = kw_m.group(1).strip() if kw_m else ""
            term = re.sub(r'\b(?:ni|qidir|top|ko\'r|kor|ayt|ber)\b', '', term).strip()
            if term:
                matches = TelegramOmniIndexer.search_saved_messages(term, limit=5)
                if matches:
                    lines = [f"📌 <b>Telegram 'Saved Messages' dan topildi ('{term}'):</b>"]
                    for idx, m in enumerate(matches, 1):
                        lines.append(f"{idx}. {m.get('snippet', '')}")
                    return "\n\n".join(lines)
                return f"Telegram 'Saved Messages' da '{term}' bo'yicha qayd topilmadi."
            else:
                data = TelegramOmniIndexer.load_cache()
                saved = data.get("saved_messages", [])[:5]
                if saved:
                    lines = [f"📌 <b>Telegram 'Saved Messages' (oxirgi {len(saved)} ta):</b>"]
                    for idx, s in enumerate(saved, 1):
                        lines.append(f"{idx}. {s.get('snippet', '')}")
                    return "\n\n".join(lines)
                return "Saved Messages bo'sh."
        except Exception as e:
            logger.warning(f"Saved messages lookup error: {e}")

    # 5.4 Telegram Contact Details lookup
    if any(w in q for w in ["xurshid", "jamshid", "kontaktlar", "shaxsiy chatlar"]):
        try:
            from jarvis_omni_indexer import TelegramOmniIndexer
            m_t = re.search(r'([a-zA-Z0-9_\'`]+)\s+(?:kim|haqida|bilan\s+yozishmalar|ning\s+raqami)', q)
            t_name = m_t.group(1).strip() if m_t else ("xurshid" if "xurshid" in q else "")
            if t_name:
                c = TelegramOmniIndexer.search_contacts_and_chats(t_name)
                if c:
                    target = c[0]
                    lines = [
                        f"👤 <b>Telegram:</b> {target.get('name')}",
                        f"📱 <b>Username:</b> {target.get('username') or 'Mavjud emas'}",
                        f"📞 <b>Telefon:</b> {target.get('phone') or 'Mavjud emas'}",
                    ]
                    if target.get("recent_history"):
                        lines.append("\n💬 <b>Oxirgi yozishmalar:</b>")
                        for h in target["recent_history"]:
                            lines.append(f"• {h}")
                    return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Contact lookup error: {e}")

    # -------------------------------------------------------------------------
    # 6. File Search (Requirement 2.c)
    # -------------------------------------------------------------------------
    file_search_term = None
    m_f = re.search(r'(?:fayl\s+qidir|faylini\s+qidir|faylni\s+top|faylini\s+top|fayl\s+top|kompyuterdan\s+qidir|search\s+file|find\s+file)[\s:]+(.+)$', raw_q, re.IGNORECASE)
    if m_f:
        file_search_term = m_f.group(1).strip()
    else:
        m_f2 = re.search(r'([a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]+)\s+(?:faylini\s+top|faylni\s+top|faylini\s+qidir|faylni\s+qidir)', raw_q, re.IGNORECASE)
        if m_f2:
            file_search_term = m_f2.group(1).strip()

    if file_search_term:
        clean_term = file_search_term.strip("'\"? ")
        try:
            from jarvis_omni_indexer import PCFilesIndexer
            matches = PCFilesIndexer.search_files(clean_term, limit=5)
            if matches:
                lines = [f"📁 **Kompyuterdan topilgan fayllar ('{clean_term}'):**\n"]
                for idx, f in enumerate(matches, 1):
                    sz = f.get('size_mb') or round(f.get('size_kb', 0) / 1024, 2)
                    lines.append(f"{idx}. 📄 **{f.get('name')}** ({sz} MB)\n   `{f.get('path')}`")
                return "\n".join(lines)

            # Fallback to tool_registry.search_files
            reg_res = tool_registry.search_files(clean_term, root_folder=str(BASE_DIR))
            reg_matches = reg_res.get("matches") or reg_res.get("files") or []
            if reg_matches:
                lines = [f"📁 **Fayllar qidiruvi natijasi ('{clean_term}'):**\n"]
                for idx, f in enumerate(reg_matches[:5], 1):
                    sz = round(f.get('size_kb', 0) / 1024, 2)
                    lines.append(f"{idx}. 📄 **{f.get('name')}** ({sz} MB)\n   `{f.get('path')}`")
                return "\n".join(lines)

            return (
                f"🔍 **Kompyuterda '{clean_term}' bo'yicha hech qanday fayl topilmadi.**\n\n"
                f"Diagnostika: Fayl nomini to'liqroq yoki kengaytmasi bilan kiriting (masalan: '{clean_term}.txt'), yoki fayl boshqa jildda ekanligini tekshiring."
            )
        except Exception as e:
            logger.warning(f"File search error: {e}")
            return (
                f"❌ **Fayl qidiruvida xatolik yuz berdi:** {e}\n\n"
                "Diagnostika: PCFilesIndexer keshini yoki fayl tizimi ruxsatlarini tekshiring."
            )

    # -------------------------------------------------------------------------
    # 7. Desktop Application Control (Launch & Close) (Requirement 2.d)
    # -------------------------------------------------------------------------
    # 7.1 Query installed apps
    if any(w in q for w in ["qanday dasturlar bor", "dasturlar ro'yxati", "o'rnatilgan dasturlar"]):
        try:
            from jarvis_omni_indexer import PCAppsIndexer
            apps = PCAppsIndexer.load_cache()
            return f"💻 Kompyuteringizda jami <b>{len(apps)} ta</b> dastur o'rnatilgan (Google Chrome, VS Code, Telegram, Word va h.k.). Istalgan birini ochish uchun '... ni och' deb buyruq bering."
        except Exception:
            pass

    # 7.2 Launch Application
    # Patterns: "<app> ni och", "<app> dasturini och", "och <app>", "ishga tushir <app>", "start <app>", "launch <app>", "open <app>"
    launch_target = None
    m_l1 = re.search(r'^([a-zA-Z0-9_\-\.\s]+?)(?:\s*ni|\s+dasturini|\s+ilovasini)?\s+(?:och|ishga\s+tushir)$', raw_q, re.IGNORECASE)
    if m_l1:
        launch_target = m_l1.group(1).strip()
    else:
        m_l2 = re.search(r'^(?:monika\s+)?(?:och|ishga\s+tushir|start|launch|open)\s+([a-zA-Z0-9_\-\.\s]+)$', raw_q, re.IGNORECASE)
        if m_l2:
            launch_target = m_l2.group(1).strip()

    if launch_target:
        launch_target = re.sub(r'(?:ni|dasturini|ilovasini)$', '', launch_target, flags=re.IGNORECASE).strip() or launch_target
        lt_lower = launch_target.lower()
        if lt_lower not in ["ekran", "ekranni", "kompyuter", "kompyuterni", "display", "ovoz", "ovozni", "kotiba", "kotibani"]:
            try:
                res = tool_registry.launch_application(launch_target)
                if isinstance(res, dict) and res.get("success"):
                    app_name = res.get("app") or launch_target
                    return f"🚀 **'{app_name}' dasturi muvaffaqiyatli ishga tushirildi.**"
                else:
                    err = res.get("error") or res.get("message") if isinstance(res, dict) else "Dastur topilmadi"
                    return (
                        f"❌ **'{launch_target}' dasturini ishga tushirishda xatolik:** {err}\n\n"
                        "Diagnostika: Dastur kompyuterda o'rnatilganligini va uning nomi to'g'ri ko'rsatilganligini tekshiring."
                    )
            except Exception as e:
                logger.error(f"Launch application error: {e}")
                return (
                    f"❌ **'{launch_target}' dasturini ochishda xatolik:** {e}\n\n"
                    "Diagnostika: Windows operatsion tizimida jarayonni ishga tushirib bo'lmadi."
                )

    # 7.3 Close Application
    # Patterns: "<app> ni yop", "<app> dasturini to'xtat", "yop <app>", "to'xtat <app>", "close <app>", "kill <app>", "exit <app>"
    close_target = None
    m_c1 = re.search(r'^([a-zA-Z0-9_\-\.\s]+?)(?:\s*ni|\s+dasturini|\s+ilovasini)?\s+(?:yop|to[\'\'\`\’]?xtat)$', raw_q, re.IGNORECASE)
    if m_c1:
        close_target = m_c1.group(1).strip()
    else:
        m_c2 = re.search(r'^(?:monika\s+)?(?:yop|to[\'\'\`\’]?xtat|close|kill|exit)\s+([a-zA-Z0-9_\-\.\s]+)$', raw_q, re.IGNORECASE)
        if m_c2:
            close_target = m_c2.group(1).strip()

    if close_target:
        close_target = re.sub(r'(?:ni|dasturini|ilovasini)$', '', close_target, flags=re.IGNORECASE).strip() or close_target
        ct_lower = close_target.lower()
        if ct_lower not in ["ovoz", "ovozni", "kotiba", "kotibani"]:
            try:
                res = tool_registry.close_application(close_target)
                if isinstance(res, dict) and res.get("success"):
                    return f"🛑 **'{close_target}' dasturi muvaffaqiyatli to'xtatildi (yopildi).**"
                else:
                    err = res.get("error") or res.get("message") if isinstance(res, dict) else "Jarayon topilmadi"
                    return (
                        f"⚠️ **'{close_target}' dasturini yopishda ogohlantirish:** {err}\n\n"
                        "Diagnostika: Bu nomdagi faol dastur yoki jarayon topilmadi."
                    )
            except Exception as e:
                logger.error(f"Close application error: {e}")
                return (
                    f"⚠️ **'{close_target}' dasturini to'xtatishda xatolik:** {e}\n\n"
                    "Diagnostika: Jarayonni to'xtatish uchun huquqlar yetarli emas."
                )

    # -------------------------------------------------------------------------
    # 8. Gemini Autonomous Multimodal & Function Calling Engine (Requirement 2.h)
    # -------------------------------------------------------------------------
    if config.is_gemini_configured():
        try:
            import google.generativeai as genai
            genai.configure(api_key=config.GEMINI_API_KEY, transport="rest")
            tools = tool_registry.get_gemini_tools()

            # Valid model candidates (requirement 2.h)
            candidate_models = ["gemini-1.5-flash", "gemini-2.0-flash", "models/gemini-1.5-flash", "gemini-1.5-pro"]
            model = None
            for m_name in candidate_models:
                try:
                    model = genai.GenerativeModel(m_name, tools=tools)
                    break
                except Exception:
                    continue
            if not model:
                model = genai.GenerativeModel("gemini-1.5-flash", tools=tools)

            try:
                sensory_info = sensor.get_sensory_context()
            except Exception:
                sensory_info = ""

            sys_prompt = (
                "Siz Monika (Jarvis)siz — Azizbek Maripovning AI yordamchisisiz. "
                "Telegram Mini App chatidan kelgan xabarga javob bering. "
                "Qisqa, do'stona, o'zbek tilida javob bering. Zarur hollarda berilgan tizim vositalarini chaqiring.\n"
                f"{sensory_info}"
            )
            chat = model.start_chat(enable_automatic_function_calling=True)
            resp = chat.send_message(
                f"{sys_prompt}\n\nFoydalanuvchi: {cmd}",
                request_options={"timeout": 20}
            )
            if resp and resp.text:
                return resp.text.strip()
        except Exception as e:
            err_str = str(e)
            logger.error(f"Gemini chat error: {err_str[:120]}")
            if "429" in err_str or "quota" in err_str.lower():
                return "⚠️ **Gemini API kvotasi cheklandi.** Biroz kuting yoki oddiy buyruqlardan foydalaning (masalan: `skrinshot`, `tizim holati`, `qulfla`)."
            if "timeout" in err_str.lower() or "timed out" in err_str.lower():
                return "⏱️ **AI so'rovi vaqti tugadi (Timeout).** Internet yoki API sekin ishlamoqda. Mahalliy buyruqlar faol."
            return f"⚠️ **AI xizmatida xatolik:** {err_str[:80]}\n\nDiagnostika: GEMINI_API_KEY yoki tarmoq ulanishini tekshiring."

    # -------------------------------------------------------------------------
    # 9. Informative Guidance & Troubleshooting (Requirement 2.g - NO CANNED TEXT)
    # -------------------------------------------------------------------------
    return (
        f"🤖 **Monika AI:** '{cmd}' buyrug'i bo'yicha tizim vositasi topilmadi.\n\n"
        "Quyidagi buyruqlardan foydalanishingiz mumkin:\n"
        "• 📸 **Skrinshot:** `skrinshot`, `ekranni rasmga ol`\n"
        "• 💻 **Tizim holati:** `tizim holati`, `status`, `resurslar`\n"
        "• 📁 **Fayl qidiruv:** `fayl qidir <nomi>`, `<fayl> faylini top`\n"
        "• 🚀 **Dasturlarni boshqarish:** `<dastur> ni och`, `<dastur> ni yop`\n"
        "• 📬 **Telegram triage:** `o'qilmagan xabarlar`, `telegram triage`\n"
        "• 🔒 **Ekran xavfsizligi:** `qulfla`, `ekranni och`\n\n"
        "Diagnostika: Erkin suhbat va savollar uchun `GEMINI_API_KEY` to'g'ri sozlanganligini tekshiring."
    )


async def handle_chat_send(request: web.Request) -> web.Response:
    """Send message to Monika AI and receive structured conversational reply."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    message = data.get("message", "").strip()
    user_id = str(data.get("user_id") or getattr(config, "TELEGRAM_ADMIN_CHAT_ID", "7710339509"))
    if not message:
        return web.json_response({"status": "error", "message": "Xabar bo'sh bo'lishi mumkin emas."}, status=400)

    try:
        from jarvis_dialogue import get_dialogue_manager
        dm = get_dialogue_manager()
        dm.append_chat_message(user_id, "user", message)
    except Exception as d_err:
        logger.warning(f"Could not record user message in dialogue history: {d_err}")
        dm = None

    loop = asyncio.get_running_loop()
    try:
        reply = await asyncio.wait_for(
            loop.run_in_executor(None, _process_chat_message, message, user_id),
            timeout=25.0
        )
    except asyncio.TimeoutError:
        reply = "⏱️ Buyruq vaqti tugdi. Oddiy buyruqlar (skrinshot, qulfla, holat) ishlayapti."
    except Exception as ex:
        logger.error(f"Chat processing exception: {ex}")
        reply = f"⚠️ Xatolik yuz berdi: {str(ex)[:80]}"

    if dm:
        try:
            dm.append_chat_message(user_id, "assistant", reply)
        except Exception:
            pass

    return web.json_response({
        "status": "success",
        "reply": reply,
        "timestamp": time.time(),
        "time_str": time.strftime("%H:%M:%S")
    })


async def handle_chat_history(request: web.Request) -> web.Response:
    """Retrieve two-way synced chat history between Telegram Bot and Mini App."""
    user_id = str(request.query.get("user_id") or getattr(config, "TELEGRAM_ADMIN_CHAT_ID", "7710339509"))
    limit = int(request.query.get("limit", 30))
    try:
        from jarvis_dialogue import get_dialogue_manager
        dm = get_dialogue_manager()
        history = dm.get_chat_history(user_id, limit=limit)
    except Exception as e:
        logger.warning(f"Error fetching chat history: {e}")
        history = []
    return web.json_response({"status": "success", "messages": history})


async def handle_chat_clear(request: web.Request) -> web.Response:
    """Clear dialogue history for the active session."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    user_id = str(data.get("user_id") or getattr(config, "TELEGRAM_ADMIN_CHAT_ID", "7710339509"))
    try:
        from jarvis_dialogue import get_dialogue_manager
        dm = get_dialogue_manager()
        dm.clear_chat_history(user_id)
    except Exception as e:
        logger.warning(f"Error clearing chat history: {e}")
    return web.json_response({"status": "success", "message": "Chat tarixi tozalandi."})


async def handle_screen_lock(request: web.Request) -> web.Response:
    """Lock the active Windows workstation session."""
    MAX_PAYLOAD = 1024 * 1024
    if request.content_length is not None and request.content_length > MAX_PAYLOAD:
        return web.json_response({"status": "failed", "error": "Payload exceeds 1MB limit"}, status=413)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, system.lock_workstation)
    success = res.get("success", False) if isinstance(res, dict) else bool(res)
    logger.info(f"Workstation locked via /api/screen/lock request: success={success}")
    return web.json_response({
        "status": "success" if success else "failed",
        "is_locked": True if success else False,
        "message": res.get("message", "Screen locked") if isinstance(res, dict) else str(res),
    })


async def handle_screen_unlock(request: web.Request) -> web.Response:
    """Wake physical display, dismiss lock screen, and inject PIN to unlock workstation."""
    loop = asyncio.get_running_loop()
    MAX_PAYLOAD = 1024 * 1024
    if request.content_length is not None and request.content_length > MAX_PAYLOAD:
        return web.json_response({"status": "failed", "error": "Payload exceeds 1MB limit"}, status=413)

    raw_body = b""
    try:
        raw_body = await request.read()
    except Exception as e:
        return web.json_response({"status": "failed", "error": f"Failed to read payload: {e}"}, status=400)

    if len(raw_body) > MAX_PAYLOAD:
        return web.json_response({"status": "failed", "error": "Payload exceeds 1MB limit"}, status=413)

    data = {}
    if raw_body and raw_body.strip():
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return web.json_response({"status": "failed", "error": f"Invalid JSON body: {e}"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"status": "failed", "error": "JSON body must be an object"}, status=400)

    pin = data.get("pin")
    res = await loop.run_in_executor(None, lambda: system.unlock_workstation(pin))
    logger.info(f"Workstation unlock executed via /api/screen/unlock: is_locked={res.get('is_locked')}")
    return web.json_response({
        "status": "success" if not res.get("is_locked") else "failed",
        "is_locked": res.get("is_locked", False),
        "message": res.get("message", ""),
    })


async def handle_screen_status(request: web.Request) -> web.Response:
    """Get active Windows session lock state and security telemetry."""
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(None, system.get_lock_status)
    return web.json_response({
        "status": "success",
        "is_locked": status["is_locked"],
        "security_status": status["security_status"],
        "timestamp": str(status["timestamp"]),
    })


async def handle_action_lock(request: web.Request) -> web.Response:
    """State-aware action: lock workstation (backwards-compatible alias)."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, system.lock_workstation)
    success = res.get("success", False) if isinstance(res, dict) else bool(res)
    return web.json_response({
        "status": "success" if success else "failed",
        "is_locked": True if success else False,
        "message": res.get("message", "Kompyuter ekrani qulflandi.") if isinstance(res, dict) else str(res)
    })


async def handle_action_unlock(request: web.Request) -> web.Response:
    """State-aware action: wake lock screen and unlock with PIN (backwards-compatible alias)."""
    loop = asyncio.get_running_loop()
    MAX_PAYLOAD = 1024 * 1024
    if request.content_length is not None and request.content_length > MAX_PAYLOAD:
        return web.json_response({"status": "failed", "error": "Payload exceeds 1MB limit"}, status=413)

    raw_body = b""
    try:
        raw_body = await request.read()
    except Exception as e:
        return web.json_response({"status": "failed", "error": f"Failed to read payload: {e}"}, status=400)

    if len(raw_body) > MAX_PAYLOAD:
        return web.json_response({"status": "failed", "error": "Payload exceeds 1MB limit"}, status=413)

    data = {}
    if raw_body and raw_body.strip():
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return web.json_response({"status": "failed", "error": f"Invalid JSON body: {e}"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"status": "failed", "error": "JSON body must be an object"}, status=400)

    pin = data.get("pin")
    res = await loop.run_in_executor(None, lambda: system.unlock_workstation(pin))
    return web.json_response({
        "status": "success" if not res.get("is_locked") else "failed",
        "is_locked": res.get("is_locked", False),
        "message": res.get("message", ""),
    })


async def handle_action_mute(request: web.Request) -> web.Response:
    """State-aware action: toggle volume mute."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, system.mute_volume)
    is_muted = system.get_volume().get("muted", False)
    msg = "Ovoz butunlay o'chirildi (Mute)." if is_muted else "Ovoz qayta yoqildi (Unmute)."
    return web.json_response({"status": "success", "is_muted": is_muted, "message": msg})


async def handle_action_key(request: web.Request) -> web.Response:
    """State-aware action: send keyboard shortcut."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    key = data.get("key", "").strip()
    if not key:
        return web.json_response({"status": "error", "message": "Klavish ko'rsatilmadi."}, status=400)
    loop = asyncio.get_running_loop()
    def _press():
        desktop.hotkey(*key.split("+"))
    await loop.run_in_executor(None, _press)
    return web.json_response({"status": "success", "key": key, "message": f"Klavish bosildi: {key}"})


async def handle_omni_search(request: web.Request) -> web.Response:
    """Search across PC apps, files, Telegram personal chats, and Saved Messages."""
    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"status": "success", "results": {}})
    loop = asyncio.get_running_loop()
    def _search():
        try:
            from jarvis_omni_indexer import get_omni_knowledge
            return get_omni_knowledge().omni_search(q)
        except Exception as e:
            logger.error(f"Error during omni search in webapp: {e}")
            return {}
    res = await loop.run_in_executor(None, _search)
    return web.json_response({"status": "success", "results": res})


async def handle_command(request: web.Request) -> web.Response:
    """Execute natural language or tool commands directly from WebApp Arc Reactor HUD."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    cmd = data.get("command", "").strip()
    if not cmd:
        return web.json_response({"status": "error", "message": "Buyruq bo'sh bo'lishi mumkin emas."}, status=400)

    loop = asyncio.get_running_loop()

    def _execute():
        q = cmd.lower()
        # Fast-path local actions & common tools
        if any(w in q for w in ["qulfla", "lock", "blokla"]) and not any(w in q for w in ["och", "unlock", "uyg'ot"]):
            res = system.lock_workstation()
            if isinstance(res, dict) and not res.get("success", False):
                err_msg = res.get("error") or res.get("message") or "Qulflashda xatolik yuz berdi"
                return f"❌ Ekranni qulflashda xatolik: {err_msg}"
            return "🔒 Kompyuter ekrani muvaffaqiyatli qulflandi."
        if any(w in q for w in ["och", "unlock", "uyg'ot", "wake"]):
            res = sensor.wake_lock_screen()
            return f"🔓 Ekran uyg'otildi: {res.get('message', 'Parol kiritish maydoni faol.')}"
        if any(w in q for w in ["ovozni o'chir", "mute", "tinchlik", "jim"]):
            system.mute_volume()
            return "🔇 Ovoz butunlay o'chirildi."
        if any(w in q for w in ["tizim holati", "holat", "resurslar", "status"]):
            res = tool_registry.dispatch("get_system_status")
            return f"💻 <b>Tizim holati:</b> CPU: {res.get('cpu_percent')}%, RAM: {res.get('ram_percent')}%, Batareya: {res.get('battery_percent', '--')}%"
        if "skrinshot" in q or "screenshot" in q:
            desktop.take_screenshot("webapp_cmd_snap.jpg")
            return "📸 Yangi ekran surati olindi."

        # Intelligent tool execution via Gemini Function Calling
        if config.is_gemini_configured():
            try:
                import google.generativeai as genai
                genai.configure(api_key=config.GEMINI_API_KEY, transport="rest")
                tools = tool_registry.get_gemini_tools()
                m = genai.GenerativeModel("models/gemini-3.5-flash-lite", tools=tools)
                sensory_info = sensor.get_sensory_context()
                sys_prompt = (
                    "Siz Monika (Jarvis)siz - Azizbek Maripovning sun'iy intellektli shaxsiy yordamchisisiz. "
                    "MARK LV HUD boshqaruv markazidan kelgan buyruqni bajaring. "
                    "Javobni o'ta qisqa (1-2 gap), aniq va lo'nda o'zbek tilida bering. "
                    "Kerakli vositalardan (tools) foydalaning.\n\n"
                    f"{sensory_info}"
                )
                chat = m.start_chat()
                resp = chat.send_message(f"{sys_prompt}\n\nFoydalanuvchi buyrug'i: {cmd}", request_options={"timeout": 15})

                # Check if function call occurred
                if resp.candidates and resp.candidates[0].content.parts:
                    for part in resp.candidates[0].content.parts:
                        if hasattr(part, "function_call") and part.function_call:
                            fn = part.function_call.name
                            args = dict(part.function_call.args)
                            tool_res = tool_registry.dispatch(fn, **args)
                            logger.info(f"WebApp executed tool {fn}: {tool_res}")
                            res_val = tool_res.get("result", tool_res.get("message", "Muvaffaqiyatli bajarildi"))
                            return f"⚡ <b>Amal bajarildi:</b> {fn}\n📌 {res_val}"
                return resp.text.strip()
            except Exception as e:
                logger.error(f"Error processing command via Gemini: {e}")

        return f"Buyruq qabul qilindi: '{cmd}'"

    reply = await loop.run_in_executor(None, _execute)
    return web.json_response({"status": "success", "reply": reply})


async def handle_openai_models(request: web.Request) -> web.Response:
    """OpenAI-compatible models catalog endpoint (OpenJarvis style)."""
    return web.json_response({
        "object": "list",
        "data": [
            {"id": "monika-ai", "object": "model", "created": int(time.time()), "owned_by": "jarvis-system"},
            {"id": "gemini-3.5-flash", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "gemini-3.5-flash-lite", "object": "model", "created": int(time.time()), "owned_by": "google"}
        ]
    })


async def handle_openai_chat_completions(request: web.Request) -> web.Response:
    """OpenAI-compatible chat completions endpoint (OpenJarvis style)."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    messages = body.get("messages", [])
    model_name = body.get("model", "monika-ai")

    # Extract last user prompt
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                user_query = content.strip()
            elif isinstance(content, list):
                user_query = " ".join([p.get("text", "") for p in content if isinstance(p, dict)])
            break

    if not user_query:
        return web.json_response({"error": {"message": "No user message found", "type": "invalid_request_error"}}, status=400)

    trace_ctx = tracer.start_trace(user_query, source="openai_api")
    loop = asyncio.get_running_loop()

    def _generate():
        q = user_query.lower()
        if any(w in q for w in ["qulfla", "lock", "blokla"]) and not any(w in q for w in ["och", "unlock"]):
            res = system.lock_workstation()
            if isinstance(res, dict) and not res.get("success", False):
                return f"Ekranni qulflashda xatolik: {res.get('message', 'Noma\'lum xatolik')}"
            return "Kompyuter ekrani qulflandi."
        if any(w in q for w in ["tizim holati", "status", "holat"]):
            res = tool_registry.dispatch("get_system_status")
            stats = res.get("stats", res) if isinstance(res, dict) else {}
            cpu = stats.get("cpu_percent", "--")
            ram = stats.get("ram_percent", "--")
            bat = stats.get("battery_percent", "--")
            return f"Tizim holati: CPU: {cpu}%, RAM: {ram}%, Batareya: {bat}%"

        if config.is_gemini_configured():
            try:
                import google.generativeai as genai
                genai.configure(api_key=config.GEMINI_API_KEY, transport="rest")
                tools = tool_registry.get_gemini_tools()
                m = genai.GenerativeModel("models/gemini-3.5-flash-lite", tools=tools)
                chat = m.start_chat()
                sensory_info = sensor.get_sensory_context()
                sys_prompt = (
                    "Siz Monika (Jarvis)siz. OpenAI API mijozi orqali bog'langan foydalanuvchiga yordam bering. "
                    "Qat'iy ravishda real fizik faktlarga tayaning va aslo yolg'on da'vo qilmang. O'zbek tilida lo'nda javob bering.\n\n"
                    f"{sensory_info}"
                )
                resp = chat.send_message(f"{sys_prompt}\n\nFoydalanuvchi: {user_query}", request_options={"timeout": 20})
                if resp.candidates and resp.candidates[0].content.parts:
                    for part in resp.candidates[0].content.parts:
                        if hasattr(part, "function_call") and part.function_call:
                            fn = part.function_call.name
                            args = dict(part.function_call.args)
                            tool_res = tool_registry.dispatch(fn, **args)
                            v_rec = tracer.verify_tool_execution(fn, args, tool_res, sensor=sensor)
                            trace_ctx["tools_called"].append({"tool": fn, "args": args, "result": tool_res, "verification": v_rec})
                            if v_rec and v_rec.get("verified") is False:
                                return f"Diqqat, '{fn}' amali muvaffaqiyatsiz bo'ldi: {v_rec.get('reason', 'Fizik tekshiruvdan o\'tmadi')}."
                            return f"Amal bajarildi ({fn}): {tool_res.get('result', tool_res.get('message', 'Bajarildi'))}"
                return resp.text.strip()
            except Exception as e:
                logger.error(f"OpenAI completion error: {e}")
        return f"Buyruq qabul qilindi: '{user_query}'"

    reply_text = await loop.run_in_executor(None, _generate)
    tracer.complete_trace(trace_ctx, reply_text, status="success")

    cmpl_id = f"chatcmpl-{int(time.time() * 1000)}"
    return web.json_response({
        "id": cmpl_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply_text
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": len(user_query.split()),
            "completion_tokens": len(reply_text.split()),
            "total_tokens": len(user_query.split()) + len(reply_text.split())
        }
    })


async def handle_traces(request: web.Request) -> web.Response:
    """Retrieve recent execution traces and verification status."""
    limit = int(request.query.get("limit", 15))
    traces = tracer.get_recent_traces(limit=limit)
    return web.json_response({"status": "success", "count": len(traces), "traces": traces})


async def handle_security(request: web.Request) -> web.Response:
    """Change security mode (ARMED, DISARMED, SNOOZED)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = data.get("mode", "ARMED")
    snooze_min = int(data.get("snooze_minutes", 30))
    new_mode = update_security_mode(mode, snooze_minutes=snooze_min)
    logger.info(f"Security mode updated via WebApp to: {new_mode}")
    return web.json_response({"status": "success", "mode": new_mode, "security_mode": new_mode})


async def handle_say(request: web.Request) -> web.Response:
    """Speak text on the PC speaker using JarvisVoice."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    text = data.get("text", "").strip()
    if not text:
        return web.json_response({"status": "error", "message": "Text parameter is empty"}, status=400)

    voice.speak(text)
    logger.info(f"Spoken text on PC speaker: '{text[:50]}'")
    return web.json_response({"status": "success", "spoken": text})


async def handle_lock(request: web.Request) -> web.Response:
    """Immediately lock the workstation."""
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, system.lock_workstation)
    success = res.get("success", False) if isinstance(res, dict) else bool(res)
    logger.info(f"Workstation locked via WebApp request: success={success}")
    return web.json_response({
        "status": "success" if success else "failed",
        "result": res,
        "message": res.get("message", "Kompyuter ekrani muvaffaqiyatli qulflandi.") if isinstance(res, dict) else "Kompyuter ekrani muvaffaqiyatli qulflandi."
    })


async def handle_mute(request: web.Request) -> web.Response:
    """Immediately stop voice speech playback."""
    voice.stop_speaking()
    logger.info("Voice playback stopped via WebApp mute.")
    return web.json_response({"status": "success", "message": "Ovoz muvaffaqiyatli to'xtatildi."})


async def handle_screenshot(request: web.Request) -> web.Response:
    """Return current desktop screenshot as JPEG image."""
    loop = asyncio.get_running_loop()

    def _grab_jpeg() -> bytes:
        res = desktop.take_screenshot("live_desktop.jpg")
        file_path = res.get("path")
        if file_path and os.path.exists(file_path):
            try:
                with Image.open(file_path) as im:
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, format="JPEG", quality=75)
                    return buf.getvalue()
            except Exception:
                with open(file_path, "rb") as f:
                    return f.read()

        # Fallback dummy image if capture fails
        img = Image.new("RGB", (640, 360), color=(15, 23, 42))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    try:
        jpeg_bytes = await loop.run_in_executor(None, _grab_jpeg)
        return web.Response(
            body=jpeg_bytes,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
        )
    except Exception as e:
        logger.error(f"Error handling screenshot: {e}")
        return web.Response(text=f"Screenshot error: {e}", status=500)


async def handle_camera(request: web.Request) -> web.Response:
    """Return latest camera frame via IPC Shared Buffer. Never block or fight for DirectShow device lock."""
    loop = asyncio.get_running_loop()

    def _get_camera_jpeg() -> bytes:
        # 1. Primary: IPC Shared Buffer continuously updated by jarvis_vision
        buffer_file = SNAPSHOTS_DIR / "live_webcam_buffer.jpg"
        if buffer_file.exists():
            try:
                # If buffer was updated within last 20 seconds, serve immediately
                if time.time() - buffer_file.stat().st_mtime < 20.0:
                    with open(buffer_file, "rb") as f:
                        return f.read()
            except Exception:
                pass

        # 2. Secondary: Check for recent genuine camera alerts or webcam frames (< 60 seconds)
        if SNAPSHOTS_DIR.exists():
            camera_snaps = sorted(
                [
                    p for p in SNAPSHOTS_DIR.glob("*")
                    if (p.name.lower().startswith("alert_") or p.name.lower().startswith("webcam_") or p.name.lower().startswith("cam_"))
                    and "desktop" not in p.name.lower()
                    and "screen" not in p.name.lower()
                    and "snap" not in p.name.lower()
                    and "tmp" not in p.name.lower()
                    and p.suffix.lower() in [".jpg", ".png", ".jpeg"]
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            if camera_snaps:
                newest = camera_snaps[0]
                if time.time() - newest.stat().st_mtime < 60.0:
                    try:
                        with open(newest, "rb") as f:
                            return f.read()
                    except Exception:
                        pass

        # 3. Informative, instant dark SVG/JPEG card if camera is idle or initializing
        try:
            from PIL import ImageDraw
            img = Image.new("RGB", (640, 480), color=(10, 15, 26))
            draw = ImageDraw.Draw(img)
            draw.rectangle([10, 10, 630, 470], outline=(30, 41, 59), width=2)
            draw.text((190, 200), "📷 VEB-KAMERA JONLI REJIMDA", fill=(6, 182, 212))
            draw.text((150, 230), "Kamera harakat datchigi bilan faol ishlamoqda.", fill=(148, 163, 184))
            draw.text((210, 260), "Kadr olish uchun qayta bosing.", fill=(236, 72, 153))

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception:
            img = Image.new("RGB", (640, 480), color=(11, 15, 25))
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            return buf.getvalue()

    try:
        jpeg_bytes = await loop.run_in_executor(None, _get_camera_jpeg)
        return web.Response(
            body=jpeg_bytes,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
        )
    except Exception as e:
        logger.error(f"Error handling camera frame: {e}")
        return web.Response(text=f"Camera error: {e}", status=500)


async def handle_mouse_move_relative(request: web.Request) -> web.Response:
    """Move mouse cursor relatively (trackpad dragging)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    dx = int(data.get("dx", 0))
    dy = int(data.get("dy", 0))

    loop = asyncio.get_running_loop()

    def _move():
        cx, cy = desktop.get_cursor_pos()
        nx = cx + dx
        ny = cy + dy
        return desktop.mouse_move(nx, ny, smooth=False)

    res = await loop.run_in_executor(None, _move)
    return web.json_response(res)


async def handle_mouse_click(request: web.Request) -> web.Response:
    """Trigger mouse click (left, right, double)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    btn = str(data.get("button", "left")).lower()

    loop = asyncio.get_running_loop()

    def _click():
        if btn == "double":
            return desktop.mouse_double_click()
        elif btn == "right":
            return desktop.mouse_right_click()
        else:
            return desktop.mouse_click(button="left")

    res = await loop.run_in_executor(None, _click)
    return web.json_response(res)


async def handle_mouse_scroll(request: web.Request) -> web.Response:
    """Scroll mouse wheel (positive = up, negative = down)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    amount = int(data.get("amount", 0))
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: desktop.mouse_scroll(amount))
    return web.json_response(res)


async def handle_keyboard_type(request: web.Request) -> web.Response:
    """Type text into active foreground window."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    text = str(data.get("text", ""))
    press_enter = bool(data.get("enter", False))
    if not text:
        return web.json_response({"status": "error", "message": "Text is empty"}, status=400)

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: desktop.keyboard_type(text, press_enter=press_enter))
    return web.json_response(res)


async def handle_keyboard_key(request: web.Request) -> web.Response:
    """Press keyboard key or shortcut combination (e.g. 'enter', 'ctrl+c', 'win+r')."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    key = str(data.get("key", "")).strip()
    if not key:
        return web.json_response({"status": "error", "message": "Key is empty"}, status=400)

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: desktop.key_press(key))
    return web.json_response(res)


async def handle_get_projects(request: web.Request) -> web.Response:
    """List Antigravity project workspaces."""
    loop = asyncio.get_running_loop()
    projects = await loop.run_in_executor(None, antigravity.list_projects)
    return web.json_response({"status": "success", "projects": projects})


async def handle_post_projects(request: web.Request) -> web.Response:
    """Create a new Antigravity project workspace."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    if not name:
        return web.json_response({"status": "error", "message": "Project name is required"}, status=400)

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: antigravity.create_project(name, description))
    return web.json_response({"status": "success", "project": res, "projects": antigravity.list_projects()})


async def handle_get_tasks(request: web.Request) -> web.Response:
    """List Antigravity delegated tasks."""
    loop = asyncio.get_running_loop()
    tasks = await loop.run_in_executor(None, antigravity.get_delegation_history)
    return web.json_response({"status": "success", "tasks": tasks})


async def handle_get_memory(request: web.Request) -> web.Response:
    """Return user profile, plans, and persistent memory context."""
    loop = asyncio.get_running_loop()

    def _read_mem():
        return {
            "status": "success",
            "owner": memory.get_owner(),
            "location": memory.get_location(),
            "plans": memory.get_plans(),
            "profile": memory.data
        }

    res = await loop.run_in_executor(None, _read_mem)
    return web.json_response(res)


async def handle_get_unread(request: web.Request) -> web.Response:
    """Return smart 3-tier Telegram triage unread digest."""
    try:
        # Run triage with a safe 20s timeout
        summary = await asyncio.wait_for(
            JarvisUserTelegram.generate_unread_summary_async(limit_dialogs=15),
            timeout=20.0
        )
        return web.json_response({
            "status": "success",
            "markdown": summary.get("markdown", "Xabarlar yo'q"),
            "voice_summary": summary.get("voice_summary", ""),
            "stats": summary.get("stats", {})
        })
    except asyncio.TimeoutError:
        logger.warning("Unread summary timed out (20s). Returning fallback.")
        return web.json_response({
            "status": "partial",
            "markdown": "⏳ Telegram sessiyasi band yoki server javobi kechikdi. Iltimos, qayta urinib ko'ring.",
            "voice_summary": "Telegram xabarlarini tahlil qilish vaqti tugadi."
        })
    except Exception as e:
        logger.error(f"Error fetching unread summary: {e}")
        return web.json_response({
            "status": "error",
            "markdown": f"⚠️ Telegram tahlilida xatolik yuz berdi: {e}",
            "voice_summary": "Xabarlarni yuklab bo'lmadi."
        })


# =============================================================================
# Monika AI Concierge (Kotiba Rejimi) Handlers
# =============================================================================

async def handle_concierge_status(request: web.Request) -> web.Response:
    """Return AI Concierge (Kotiba) status, away state, and inbox count."""
    loop = asyncio.get_running_loop()
    status_data = await loop.run_in_executor(None, concierge_manager.get_status)
    return web.json_response(status_data)


async def handle_concierge_toggle(request: web.Request) -> web.Response:
    """Toggle AI Concierge mode on or off (accepts optional {"enabled": true/false})."""
    target_state = None
    try:
        body = await request.json()
        if isinstance(body, dict) and "enabled" in body:
            target_state = bool(body["enabled"])
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    new_state = await loop.run_in_executor(None, lambda: concierge_manager.toggle(target_state))
    msg = "Kotiba rejimi yoqildi (Faol)" if new_state else "Kotiba rejimi to'xtatildi (Pauzada)"
    return web.json_response({
        "status": "success",
        "enabled": new_state,
        "message": msg
    })


async def handle_concierge_inbox(request: web.Request) -> web.Response:
    """Return recorded notes and incoming messages captured by AI Concierge."""
    loop = asyncio.get_running_loop()
    inbox = await loop.run_in_executor(None, concierge_manager.get_inbox)
    return web.json_response({
        "status": "success",
        "inbox": inbox,
        "count": len(inbox)
    })


async def handle_concierge_inbox_clear(request: web.Request) -> web.Response:
    """Clear recorded notes in AI Concierge inbox."""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, concierge_manager.clear_inbox)
    return web.json_response({
        "status": "success" if ok else "error",
        "message": "Kotiba qutisi muvaffaqiyatli tozalandi." if ok else "Qutini tozalashda xatolik yuz berdi.",
        "count": 0
    })


# =============================================================================
# Application Factory
# =============================================================================

def create_app() -> web.Application:
    """Construct and configure the aiohttp WebApp."""
    app = web.Application(middlewares=[cors_middleware, auth_middleware])

    # Static / HTML & PWA
    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/sw.js", handle_service_worker)
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_get("/icons/{filename}", handle_pwa_icon)
    app.router.add_get("/monika_avatar.jpg", handle_avatar)
    app.router.add_get("/avatar", handle_avatar)

    # Core System & Status APIs
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/command", handle_command)
    app.router.add_post("/api/security", handle_security)
    app.router.add_post("/api/say", handle_say)
    app.router.add_post("/api/lock", handle_lock)
    app.router.add_post("/api/mute", handle_mute)

    # Windows Workstation Screen Lock & Unlock APIs (Milestone 1)
    app.router.add_post("/api/screen/lock", handle_screen_lock)
    app.router.add_post("/api/screen/unlock", handle_screen_unlock)
    app.router.add_get("/api/screen/status", handle_screen_status)

    # Monika AI Concierge (Kotiba Rejimi) APIs
    app.router.add_get("/api/concierge/status", handle_concierge_status)
    app.router.add_post("/api/concierge/toggle", handle_concierge_toggle)
    app.router.add_get("/api/concierge/inbox", handle_concierge_inbox)
    app.router.add_post("/api/concierge/inbox/clear", handle_concierge_inbox_clear)

    # v3.0 Chat & Two-Way Synced Conversation History APIs
    app.router.add_post("/api/chat/send", handle_chat_send)
    app.router.add_get("/api/chat/history", handle_chat_history)
    app.router.add_post("/api/chat/clear", handle_chat_clear)

    # v3.0 State-Aware Dynamic Action APIs
    app.router.add_post("/api/action/lock", handle_action_lock)
    app.router.add_post("/api/action/unlock", handle_action_unlock)
    app.router.add_post("/api/action/mute", handle_action_mute)
    app.router.add_post("/api/action/key", handle_action_key)

    # v3.0 Omni-Knowledge Desktop & Telegram Search API
    app.router.add_get("/api/omni/search", handle_omni_search)

    # Vision & Visual Feeds
    app.router.add_get("/api/screenshot", handle_screenshot)
    app.router.add_get("/api/camera", handle_camera)

    # Remote Touchpad & Keyboard Control
    app.router.add_post("/api/mouse/move_relative", handle_mouse_move_relative)
    app.router.add_post("/api/mouse/scroll", handle_mouse_scroll)
    app.router.add_post("/api/mouse/click", handle_mouse_click)
    app.router.add_post("/api/keyboard/type", handle_keyboard_type)
    app.router.add_post("/api/keyboard/key", handle_keyboard_key)

    # Antigravity Projects & Tasks
    app.router.add_get("/api/projects", handle_get_projects)
    app.router.add_post("/api/projects", handle_post_projects)
    app.router.add_get("/api/tasks", handle_get_tasks)

    # Personal Memory & Telegram Triage
    app.router.add_get("/api/memory", handle_get_memory)
    app.router.add_get("/api/unread", handle_get_unread)

    # OpenJarvis-Style OpenAI API & Audit Traces
    app.router.add_get("/v1/models", handle_openai_models)
    app.router.add_post("/v1/chat/completions", handle_openai_chat_completions)
    app.router.add_get("/api/traces", handle_traces)

    return app


def main():
    """Run webapp server."""
    _enforce_single_webapp_instance()
    port = int(os.environ.get("PORT", 8088))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"🚀 Starting Monika TMA WebApp Server on http://{host}:{port} ...")
    app = create_app()
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
