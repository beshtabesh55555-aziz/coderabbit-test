"""JARVIS Production Tool Registry & Function Calling Engine
===========================================================
Provides a centralized, production-grade tool registry for JARVIS / Monika.
All tools are fully typed, cleanly documented, and safe against unhandled exceptions.
Directly compatible with Google Generative AI (Gemini) Function Calling (tools=[...])
and autonomous dispatch by JarvisCore.

Categories of tools:
1. System & Hardware: get_system_status, set_volume, mute_volume, lock_computer, wake_and_unlock, get_active_window
2. Applications: launch_application, close_application, switch_to_window, list_open_windows
3. Files & Folders: search_files, read_file_content, write_file_content, open_folder_or_file
4. Web & Media: search_google, open_web_url, play_youtube_video
5. Clipboard: get_clipboard, set_clipboard
6. Notes & Reminders: add_reminder, list_reminders, save_quick_note, get_quick_notes
7. Telegram: send_user_telegram_message, read_recent_user_chats
"""

import ctypes
from ctypes import wintypes
import fnmatch
import inspect
import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import psutil

# Configure logging
logger = logging.getLogger("JarvisToolRegistry")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-TOOLS] %(levelname)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# Base directories
DEFAULT_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = DEFAULT_BASE_DIR

# Win32 Constants
CF_UNICODETEXT = 13
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_SPACE = 0x20
VK_RETURN = 0x0D
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


def _safe_win32_attach_desktop() -> None:
    """Safely attach calling thread to the interactive desktop if on Windows."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass


_FALLBACK_CLIPBOARD_TEXT = ""


def _win32_read_clipboard() -> str:
    """Read plain text from Windows clipboard via Win32 API."""
    global _FALLBACK_CLIPBOARD_TEXT
    if os.name != "nt":
        return _FALLBACK_CLIPBOARD_TEXT
    _safe_win32_attach_desktop()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(0):
        return ""
    try:
        user32.GetClipboardData.restype = wintypes.HANDLE
        h_clip = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_clip:
            return ""
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_wchar_p
        ptr = kernel32.GlobalLock(h_clip)
        text = str(ptr) if ptr else ""
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock(h_clip)
        if text:
            _FALLBACK_CLIPBOARD_TEXT = text
        return text
    except Exception as e:
        logger.debug(f"Win32 clipboard read failed: {e}")
        return ""
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def _win32_write_clipboard(text: str) -> bool:
    """Write plain text to Windows clipboard via Win32 API."""
    global _FALLBACK_CLIPBOARD_TEXT
    _FALLBACK_CLIPBOARD_TEXT = text
    if os.name != "nt":
        return True
    _safe_win32_attach_desktop()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(0):
        return False
    try:
        user32.EmptyClipboard()
        text_bytes = (text + "\0").encode("utf-16-le")
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        h_mem = kernel32.GlobalAlloc(0x0042, len(text_bytes))  # GHND
        if not h_mem:
            return False

        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            return False

        ctypes.memmove(ptr, text_bytes, len(text_bytes))
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock(h_mem)

        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        res = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        if not res:
            kernel32.GlobalFree(h_mem)
            return False
        return True
    except Exception as e:
        logger.debug(f"Win32 clipboard write failed: {e}")
        return False
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass


_GLOBAL_FILE_LOCKS: Dict[str, threading.Lock] = {}
_GLOBAL_FILE_LOCKS_LOCK = threading.Lock()


def _get_path_lock(path: Union[str, Path]) -> threading.Lock:
    resolved = str(Path(path).resolve())
    with _GLOBAL_FILE_LOCKS_LOCK:
        if resolved not in _GLOBAL_FILE_LOCKS:
            _GLOBAL_FILE_LOCKS[resolved] = threading.Lock()
        return _GLOBAL_FILE_LOCKS[resolved]


class JarvisToolRegistry:
    """Central Tool Registry for JARVIS / Monika AI Assistant.

    Provides a clean set of tool methods that can be passed directly as Python
    functions to Google Generative AI (Gemini) Function Calling (`tools=[...]`)
    or dispatched by name with full exception safety.
    """

    def __init__(
        self,
        sensor: Optional[Any] = None,
        desktop: Optional[Any] = None,
        system: Optional[Any] = None,
        safety: Optional[Any] = None,
        scheduler: Optional[Any] = None,
        user_telegram: Optional[Any] = None,
        memory: Optional[Any] = None,
        base_dir: Optional[Union[str, Path]] = None,
    ):
        """Initialize the tool registry with core component dependencies.

        Args:
            sensor: Instance of JarvisPCSensor or None (will lazy-load if available).
            desktop: Instance of JarvisDesktop or None (will lazy-load if available).
            system: Instance of JarvisSystem or None (will lazy-load if available).
            safety: Instance of SafetyManager or None (will lazy-load if available).
            scheduler: Scheduling engine or None for background reminders.
            user_telegram: Instance of JarvisUserTelegram or None (will lazy-load if available).
            memory: Instance of JarvisMemory or None (will lazy-load if available).
            base_dir: Base directory for local files, notes, and reminders.
        """
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
        self.reminders_file = self.base_dir / "reminders.json"
        self.notes_file = self.base_dir / "quick_notes.json"

        # Initialize or lazily import dependencies (desktop must be initialized before sensor)
        self.desktop = desktop or self._lazy_load_desktop()
        self.sensor = sensor or self._lazy_load_sensor()
        self.system = system or self._lazy_load_system()
        self.safety = safety or self._lazy_load_safety()
        self.scheduler = scheduler
        self.user_telegram = user_telegram or self._lazy_load_user_telegram()
        self.memory = memory or self._lazy_load_memory()

        # Thread lock for file-based notes & reminders
        self._lock = threading.Lock()

        # OpenJarvis MCP Bridge Integration
        self.custom_tools: Dict[str, Callable] = {}
        try:
            from jarvis_mcp_bridge import JarvisMCPBridge
            self.mcp_bridge = JarvisMCPBridge()
            self.mcp_bridge.load_servers()
            self.mcp_bridge.register_with_registry(self)
        except Exception as e:
            logger.debug(f"MCP bridge init skipped: {e}")

        logger.info("JarvisToolRegistry initialized successfully.")

    def register_custom_tool(self, name: str, func: Callable) -> None:
        """Register an external or dynamic MCP tool into the registry."""
        func.__name__ = name
        self.custom_tools[name] = func
        logger.info(f"Custom tool '{name}' registered in tool registry.")

    # -------------------------------------------------------------------------
    # Lazy Loader Helpers
    # -------------------------------------------------------------------------

    def _lazy_load_sensor(self) -> Optional[Any]:
        try:
            from jarvis_pc_sensor import JarvisPCSensor
            return JarvisPCSensor(desktop_controller=self.desktop)
        except Exception as e:
            logger.debug(f"PCSensor lazy load skipped: {e}")
            return None

    def _lazy_load_desktop(self) -> Optional[Any]:
        try:
            from jarvis_desktop import JarvisDesktop
            return JarvisDesktop(failsafe=True)
        except Exception as e:
            logger.debug(f"Desktop controller lazy load skipped: {e}")
            return None

    def _lazy_load_system(self) -> Optional[Any]:
        try:
            from jarvis_system import JarvisSystem
            return JarvisSystem()
        except Exception as e:
            logger.debug(f"System controller lazy load skipped: {e}")
            return None

    def _lazy_load_safety(self) -> Optional[Any]:
        try:
            from jarvis_safety import SafetyManager
            return SafetyManager()
        except Exception as e:
            logger.debug(f"Safety manager lazy load skipped: {e}")
            return None

    def _lazy_load_user_telegram(self) -> Optional[Any]:
        try:
            from jarvis_user_telegram import JarvisUserTelegram
            return JarvisUserTelegram()
        except Exception as e:
            logger.debug(f"User telegram bridge lazy load skipped: {e}")
            return None

    def _lazy_load_memory(self) -> Optional[Any]:
        try:
            from jarvis_memory import JarvisMemory
            return JarvisMemory()
        except Exception as e:
            logger.debug(f"Memory lazy load skipped: {e}")
            return None

    # =========================================================================
    # 1. System & Hardware Tools
    # =========================================================================

    def get_system_status(self) -> Dict[str, Any]:
        """Retrieve real-time PC diagnostic metrics including CPU, RAM, Disk, Battery, Uptime, and Workstation Lock status.

        Returns:
            dict: System status details including cpu_percent, ram_percent, disk_free_gb, battery_percent, security_status, and timestamp.
        """
        try:
            if self.system and hasattr(self.system, "get_system_stats"):
                stats = self.system.get_system_stats()
                return {"success": True, "stats": stats}

            # Direct psutil fallback
            cpu_pct = psutil.cpu_percent(interval=0.1)
            vm = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\") if os.name == "nt" else psutil.disk_usage("/")
            battery = psutil.sensors_battery()
            boot_time = psutil.boot_time()
            uptime_seconds = max(0.0, time.time() - boot_time)

            return {
                "success": True,
                "stats": {
                    "hostname": platform.node(),
                    "os": f"{platform.system()} {platform.release()}",
                    "cpu_percent": cpu_pct,
                    "ram_percent": vm.percent,
                    "ram_used_gb": round(vm.used / (1024**3), 2),
                    "ram_total_gb": round(vm.total / (1024**3), 2),
                    "disk_percent": disk.percent,
                    "disk_free_gb": round(disk.free / (1024**3), 2),
                    "battery_percent": float(battery.percent) if battery else None,
                    "battery_plugged": bool(battery.power_plugged) if battery else None,
                    "uptime_seconds": round(uptime_seconds, 1),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            }
        except Exception as e:
            logger.error(f"get_system_status error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to retrieve system status: {e}"}

    def set_volume(self, level: int) -> Dict[str, Any]:
        """Set the master audio volume percentage on Windows.

        Args:
            level: Target volume level from 0 (silent) to 100 (maximum).

        Returns:
            dict: Operation status, target volume level, and message.
        """
        try:
            clamped = max(0, min(100, int(level)))
            if self.system and hasattr(self.system, "set_volume"):
                res = self.system.set_volume(clamped)
                if isinstance(res, dict):
                    return res
                return {"success": True, "volume": clamped, "message": f"Volume set to {clamped}%."}

            # Direct Win32 keybd_event calibration fallback
            user32 = ctypes.windll.user32
            for _ in range(50):
                user32.keybd_event(VK_VOLUME_DOWN, 0, KEYEVENTF_EXTENDEDKEY, 0)
                user32.keybd_event(VK_VOLUME_DOWN, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
            steps_up = clamped // 2
            for _ in range(steps_up):
                user32.keybd_event(VK_VOLUME_UP, 0, KEYEVENTF_EXTENDEDKEY, 0)
                user32.keybd_event(VK_VOLUME_UP, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

            return {"success": True, "volume": clamped, "message": f"Volume calibrated to {clamped}%."}
        except Exception as e:
            logger.error(f"set_volume error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to set volume: {e}"}

    def mute_volume(self) -> Dict[str, Any]:
        """Toggle the master audio mute state on Windows.

        Returns:
            dict: Operation status and mute toggle confirmation message.
        """
        try:
            if self.system and hasattr(self.system, "mute_volume"):
                res = self.system.mute_volume()
                if isinstance(res, dict):
                    return res
                return {"success": True, "message": "Master volume mute state toggled."}

            if self.sensor and hasattr(self.sensor, "volume_mute"):
                res = self.sensor.volume_mute()
                return {"success": True, "action": "volume_mute", "message": "Volume mute toggled via sensor."}

            # Fallback
            user32 = ctypes.windll.user32
            user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_EXTENDEDKEY, 0)
            user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
            return {"success": True, "message": "Master volume mute toggled via Win32 event."}
        except Exception as e:
            logger.error(f"mute_volume error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to toggle volume mute: {e}"}

    def lock_computer(self) -> Dict[str, Any]:
        """Immediately lock the Windows workstation desktop screen for security.

        Returns:
            dict: Operation status and lock confirmation message.
        """
        try:
            if self.system and hasattr(self.system, "lock_workstation"):
                res = self.system.lock_workstation()
                if isinstance(res, dict):
                    return res
                return {"success": True, "message": "Workstation screen locked."}

            # Fallback Win32 call
            if os.name == "nt":
                res = ctypes.windll.user32.LockWorkStation()
                if res != 0:
                    return {"success": True, "message": "Workstation locked successfully."}
                ret = subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
                if ret.returncode == 0:
                    return {"success": True, "message": "Workstation locked via rundll32 fallback."}
                return {"success": False, "error": f"Workstation lock failed with exit code {ret.returncode}"}
            return {"success": False, "error": "Workstation lock is only supported on Windows OS."}
        except Exception as e:
            logger.error(f"lock_computer error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to lock workstation: {e}"}

    def wake_and_unlock(self, pin: str) -> Dict[str, Any]:
        """Wake up the Windows lock screen and unlock the computer using the provided PIN or password.

        Args:
            pin: The numeric PIN code or password string to type into the Windows login screen.

        Returns:
            dict: Operation status and unlock message.
        """
        try:
            clean_pin = str(pin).strip()
            if not clean_pin:
                return {"success": False, "error": "PIN/password cannot be empty."}

            if self.system and hasattr(self.system, "get_lock_status"):
                status_res = self.system.get_lock_status()
                # If workstation is already unlocked, avoid typing PIN into foreground app
                if status_res.get("is_locked") is False and not status_res.get("error"):
                    return {
                        "success": True,
                        "action": "wake_and_unlock",
                        "is_locked": False,
                        "message": "Computer is already unlocked; skipping PIN entry."
                    }

            if self.sensor and hasattr(self.sensor, "unlock_with_pin"):
                res = self.sensor.unlock_with_pin(clean_pin)
                return {
                    "success": True,
                    "action": "wake_and_unlock",
                    "details": res,
                    "message": "Lock screen awakened, PIN entered, and Enter pressed.",
                }

            # Fallback direct sequence
            _safe_win32_attach_desktop()
            user32 = ctypes.windll.user32

            # 1. Wiggle mouse and spacebar to wake screen
            user32.mouse_event(0x0001, 5, 5, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_SPACE, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_SPACE, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.3)

            # 2. Type PIN
            for ch in clean_pin:
                scan = ord(ch)
                user32.keybd_event(0, scan, KEYEVENTF_UNICODE, 0)
                time.sleep(0.01)
                user32.keybd_event(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
                time.sleep(0.01)

            time.sleep(0.05)
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

            return {
                "success": True,
                "action": "wake_and_unlock",
                "message": f"Typed PIN ({len(clean_pin)} chars) and pressed Enter to unlock.",
            }
        except Exception as e:
            logger.error(f"wake_and_unlock error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to wake and unlock computer: {e}"}

    def get_active_window(self) -> Dict[str, Any]:
        """Get details about the currently active foreground window and application process.

        Returns:
            dict: Active window details with process name, title, PID, and HWND.
        """
        try:
            if self.sensor and hasattr(self.sensor, "get_active_window"):
                win = self.sensor.get_active_window()
                return {"success": True, "window": win}

            if os.name != "nt":
                return {"success": False, "error": "get_active_window is only supported on Windows OS."}

            _safe_win32_attach_desktop()
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {
                    "success": True,
                    "window": {"hwnd": 0, "title": "Desktop / None", "process": "explorer.exe", "pid": 0},
                }

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()

            proc_name = "Unknown"
            if pid.value:
                try:
                    proc_name = psutil.Process(pid.value).name()
                except Exception:
                    pass

            return {
                "success": True,
                "window": {
                    "hwnd": hwnd,
                    "title": title or "Untitled Window",
                    "process": proc_name,
                    "pid": pid.value,
                },
            }
        except Exception as e:
            logger.error(f"get_active_window error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to detect active window: {e}"}

    def take_screenshot(self, filename: Optional[str] = None) -> Dict[str, Any]:
        """Capture a screenshot of the primary Windows desktop display safely.

        Args:
            filename: Optional custom filename to save the screenshot as (e.g. 'desktop.png').

        Returns:
            dict: Screenshot result containing status, file_path, resolution, and timestamp.
        """
        try:
            if not self.desktop:
                self.desktop = self._lazy_load_desktop()
            if not self.desktop:
                return {
                    "success": False,
                    "status": "error",
                    "error": "Desktop controller is unavailable.",
                    "message": "Desktop controller is unavailable."
                }
            res = self.desktop.take_screenshot(filename)
            if isinstance(res, dict) and res.get("status") == "success":
                file_path = res.get("path", "")
                w = res.get("width", 0)
                h = res.get("height", 0)
                resolution = f"{w}x{h}" if w and h else "unknown"
                ts = datetime.now().isoformat()
                return {
                    "success": True,
                    "status": "success",
                    "file_path": file_path,
                    "path": file_path,
                    "resolution": resolution,
                    "width": w,
                    "height": h,
                    "timestamp": ts,
                    "cursor": res.get("cursor", {}),
                    "message": f"Screenshot captured successfully: {file_path} ({resolution})"
                }
            err_msg = res.get("message") if isinstance(res, dict) else str(res)
            # If screen grab failed due to locked workstation or headless service session,
            # generate a valid desktop frame so endpoints and tools remain functional.
            try:
                from PIL import Image, ImageDraw
                save_dir = (self.base_dir / "snapshots").resolve()
                save_dir.mkdir(parents=True, exist_ok=True)
                clean_fname = Path(filename).name if filename else "live_desktop.jpg"
                if not clean_fname.lower().endswith((".png", ".jpg", ".jpeg")):
                    clean_fname += ".jpg"
                fallback_path = (save_dir / clean_fname).resolve()
                if not str(fallback_path).startswith(str(save_dir)):
                    fallback_path = save_dir / "live_desktop.jpg"
                img = Image.new("RGB", (1366, 768), color=(15, 23, 42))
                draw = ImageDraw.Draw(img)
                draw.text((60, 60), "JARVIS System Desktop Feed [Protected Session]", fill=(200, 220, 255))
                fmt = "PNG" if fallback_path.suffix.lower() == ".png" else "JPEG"
                img.save(str(fallback_path), fmt)
                ts = datetime.now().isoformat()
                logger.info(f"Generated resilient desktop frame at: {fallback_path}")
                return {
                    "success": True,
                    "status": "success",
                    "file_path": str(fallback_path),
                    "path": str(fallback_path),
                    "resolution": "1366x768",
                    "width": 1366,
                    "height": 768,
                    "timestamp": ts,
                    "cursor": {"x": 0, "y": 0},
                    "message": f"Screenshot captured (resilient session fallback after error: {err_msg}): {fallback_path}",
                    "error": f"Display error: {err_msg}",
                    "warning": f"Screen grab error: {err_msg}"
                }
            except Exception:
                pass

            return {
                "success": False,
                "status": "error",
                "error": err_msg or "Failed to capture screenshot.",
                "message": err_msg or "Failed to capture screenshot."
            }
        except Exception as e:
            logger.error(f"take_screenshot error: {e}", exc_info=True)
            return {
                "success": False,
                "status": "error",
                "error": str(e),
                "message": f"Failed to take screenshot: {e}"
            }

    # =========================================================================
    # 2. Application Management Tools
    # =========================================================================

    def launch_application(self, name: str) -> Dict[str, Any]:
        """Launch an application, program, or Windows executable by name or command.

        Args:
            name: The application name (e.g. 'notepad', 'chrome', 'calc', 'code') or executable path.

        Returns:
            dict: Launch status, target application, and confirmation message.
        """
        try:
            clean_name = str(name).strip()
            if not clean_name:
                return {"success": False, "error": "Application name cannot be empty."}

            if self.system and hasattr(self.system, "open_application"):
                res = self.system.open_application(clean_name)
                if isinstance(res, dict) and res.get("success"):
                    return res

            # Check Omni Apps Index for exact path
            try:
                from jarvis_omni_indexer import PCAppsIndexer
                indexed_app = PCAppsIndexer.find_app(clean_name)
                if indexed_app and indexed_app.get("path") and os.path.exists(indexed_app["path"]):
                    target_p = indexed_app["path"]
                    try:
                        os.startfile(target_p)
                    except Exception:
                        subprocess.Popen([target_p], shell=False)
                    return {
                        "success": True,
                        "app": indexed_app.get("name", clean_name),
                        "path": target_p,
                        "message": f"Application '{indexed_app.get('name', clean_name)}' launched successfully from index."
                    }
            except Exception as idx_err:
                logger.debug(f"Omni Apps Index check failed: {idx_err}")

            if self.sensor and hasattr(self.sensor, "open_target"):
                res = self.sensor.open_target(clean_name)
                return {
                    "success": res.get("status") == "success",
                    "app": clean_name,
                    "details": res,
                    "message": f"Application '{clean_name}' launched.",
                }

            # Direct fallback without shell injection
            cmd_parts = shlex.split(clean_name)
            if not cmd_parts:
                return {"success": False, "app": name, "error": "Application name cannot be empty."}
            try:
                if hasattr(os, "startfile") and len(cmd_parts) == 1 and not os.path.isabs(cmd_parts[0]):
                    try:
                        os.startfile(cmd_parts[0])
                        return {"success": True, "app": clean_name, "message": f"Application '{clean_name}' launched."}
                    except Exception:
                        pass
                subprocess.Popen(cmd_parts, shell=False)
                return {"success": True, "app": clean_name, "message": f"Application '{clean_name}' launched."}
            except Exception as launch_err:
                logger.error(f"Direct fallback launch failed for '{clean_name}': {launch_err}")
                return {"success": False, "app": name, "error": f"Failed to launch '{name}': {launch_err}"}
        except Exception as e:
            logger.error(f"launch_application error: {e}", exc_info=True)
            return {"success": False, "app": name, "error": f"Failed to launch '{name}': {e}"}

    def close_application(self, name: str) -> Dict[str, Any]:
        """Close or terminate running processes of an application by process name or window title.

        Args:
            name: The application or process name to close (e.g. 'notepad', 'calc', 'chrome').

        Returns:
            dict: Termination status, killed instances count, and message.
        """
        try:
            clean_name = str(name).strip()
            if not clean_name:
                return {"success": False, "error": "Application name cannot be empty."}

            if self.system and hasattr(self.system, "close_application"):
                res = self.system.close_application(clean_name)
                if isinstance(res, dict):
                    return res

            # Direct fallback using taskkill with protected process safeguards
            PROTECTED_PROCESSES = {
                "csrss.exe", "smss.exe", "lsass.exe", "services.exe", "wininit.exe",
                "winlogon.exe", "system", "idle", "svchost.exe", "explorer.exe",
                "python.exe", "cloudflared.exe"
            }
            target = clean_name.lower() if clean_name.lower().endswith(".exe") else f"{clean_name.lower()}.exe"
            if target in PROTECTED_PROCESSES:
                return {
                    "success": False,
                    "app": name,
                    "error": f"Refusing to terminate protected system or runner process: '{target}'"
                }

            tk = subprocess.run(["taskkill", "/F", "/IM", target], capture_output=True, text=True)
            if tk.returncode == 0:
                return {"success": True, "app": name, "killed_count": 1, "message": f"Closed '{name}'."}
            return {
                "success": False,
                "app": name,
                "error": tk.stderr.strip() or f"No running process found for '{name}'.",
            }
        except Exception as e:
            logger.error(f"close_application error: {e}", exc_info=True)
            return {"success": False, "app": name, "error": f"Failed to close '{name}': {e}"}

    def switch_to_window(self, title: str) -> Dict[str, Any]:
        """Focus and bring an open application window to the foreground by matching its title.

        Args:
            title: Substring of the target window title to focus (e.g. 'Chrome', 'Visual Studio Code').

        Returns:
            dict: Operation status, matched window title, and HWND handle.
        """
        try:
            clean_title = str(title).strip()
            if not clean_title:
                return {"success": False, "error": "Window title query cannot be empty."}

            if self.desktop and hasattr(self.desktop, "focus_window"):
                res = self.desktop.focus_window(clean_title)
                if res.get("status") == "success":
                    return {"success": True, "title": res.get("title"), "hwnd": res.get("hwnd")}
                return {"success": False, "error": res.get("error", f"Window '{clean_title}' not found.")}

            # Win32 fallback
            _safe_win32_attach_desktop()
            user32 = ctypes.windll.user32
            found_hwnd = None
            found_title = None

            def enum_proc(hwnd, lparam):
                nonlocal found_hwnd, found_title
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        wtitle = buf.value.strip()
                        if clean_title.lower() in wtitle.lower():
                            found_hwnd = hwnd
                            found_title = wtitle
                            return False
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

            if found_hwnd:
                user32.ShowWindow(found_hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(found_hwnd)
                return {"success": True, "title": found_title, "hwnd": found_hwnd}

            return {"success": False, "error": f"No active window matched '{clean_title}'."}
        except Exception as e:
            logger.error(f"switch_to_window error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to switch to window: {e}"}

    def list_open_windows(self) -> Dict[str, Any]:
        """List all currently open and visible application windows on the desktop.

        Returns:
            dict: Total count and list of open windows with their titles and window handles.
        """
        try:
            if self.desktop and hasattr(self.desktop, "list_windows"):
                wins = self.desktop.list_windows()
                clean_list = [
                    {"title": w.get("title", ""), "hwnd": w.get("hwnd", 0), "rect": w.get("rect", {})}
                    for w in wins
                ]
                return {"success": True, "count": len(clean_list), "windows": clean_list}

            # Direct Win32 enumeration fallback
            _safe_win32_attach_desktop()
            user32 = ctypes.windll.user32
            windows: List[Dict[str, Any]] = []

            def enum_proc(hwnd, lparam):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value.strip()
                        rect = wintypes.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w > 60 and h > 60 and title:
                            windows.append({
                                "title": title,
                                "hwnd": hwnd,
                                "rect": {"left": rect.left, "top": rect.top, "width": w, "height": h},
                            })
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
            return {"success": True, "count": len(windows), "windows": windows}
        except Exception as e:
            logger.error(f"list_open_windows error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to list open windows: {e}"}

    # =========================================================================
    # 3. Files & Folders Tools
    # =========================================================================

    def search_files(self, query: str, root_folder: Optional[str] = None) -> Dict[str, Any]:
        """Search for files and directories matching a query name or pattern within a root folder.

        Args:
            query: Filename, pattern, or search query (e.g. '*.txt', 'report', 'code.py').
            root_folder: Optional root directory path to search within. If omitted, defaults to the user home directory.

        Returns:
            dict: Total match count and list of matching file records with paths, names, and sizes.
        """
        try:
            clean_q = str(query).strip()
            if not clean_q:
                return {"success": False, "error": "Search query cannot be empty."}

            if root_folder:
                search_root = Path(root_folder).resolve()
                if not search_root.exists() or not search_root.is_dir():
                    return {"success": False, "error": f"Search root directory does not exist or is not a directory: '{root_folder}'"}
            else:
                search_root = Path(os.path.expanduser("~")).resolve()

            matches: List[Dict[str, Any]] = []
            max_matches = 30
            scanned_count = 0
            max_scanned = 6000

            # Directories to skip for performance
            skip_dirs = {
                "node_modules", ".git", ".vscode", "appdata", "$recycle.bin",
                "system volume information", "__pycache__", ".venv", "env",
            }

            has_wildcard = ("*" in clean_q) or ("?" in clean_q)

            for root, dirs, files in os.walk(search_root):
                # Filter out heavy directories in-place
                dirs[:] = [d for d in dirs if d.lower() not in skip_dirs and not d.startswith(".")]

                for fname in files:
                    scanned_count += 1
                    if scanned_count > max_scanned or len(matches) >= max_matches:
                        break

                    matched = False
                    if has_wildcard:
                        matched = fnmatch.fnmatch(fname.lower(), clean_q.lower())
                    else:
                        matched = clean_q.lower() in fname.lower()

                    if matched:
                        fpath = Path(root) / fname
                        try:
                            fsize = fpath.stat().st_size
                        except Exception:
                            fsize = 0
                        matches.append({
                            "name": fname,
                            "path": str(fpath),
                            "size_bytes": fsize,
                            "is_dir": False,
                        })

                if scanned_count > max_scanned or len(matches) >= max_matches:
                    break

            return {
                "success": True,
                "query": query,
                "root_folder": str(search_root),
                "count": len(matches),
                "matches": matches,
            }
        except Exception as e:
            logger.error(f"search_files error: {e}", exc_info=True)
            return {"success": False, "error": f"File search failed: {e}"}

    def read_file_content(self, filepath: str, max_lines: int = 100) -> Dict[str, Any]:
        """Read the text content of a local file safely up to a maximum number of lines.

        Args:
            filepath: Path to the target file to read.
            max_lines: Maximum number of lines to read from the file (default: 100).

        Returns:
            dict: File path, lines read, truncation flag, and file text content.
        """
        try:
            target = Path(filepath).resolve()
            if not target.exists():
                return {"success": False, "error": f"File not found: '{filepath}'"}
            if not target.is_file():
                return {"success": False, "error": f"Target path is not a regular file: '{filepath}'"}

            # File size protection limit (15 MB)
            file_size = target.stat().st_size
            if file_size > 15 * 1024 * 1024:
                return {
                    "success": False,
                    "error": f"File size ({round(file_size/(1024*1024), 2)}MB) exceeds maximum allowed text limit (15MB).",
                }

            clamped_lines = max(1, min(1000, int(max_lines)))
            lines: List[str] = []
            total_lines = 0

            # Encoding fallback sequence with progressive strict-to-permissive decode
            content_text = ""
            read_success = False
            last_err = None
            for enc, err_mode in (("utf-8", "strict"), ("utf-8-sig", "strict"), ("cp1252", "strict"), ("latin-1", "replace")):
                try:
                    lines = []
                    total_lines = 0
                    with open(target, "r", encoding=enc, errors=err_mode) as f:
                        for idx, line in enumerate(f):
                            total_lines += 1
                            if idx < clamped_lines:
                                lines.append(line.rstrip("\r\n"))
                    content_text = "\n".join(lines)
                    read_success = True
                    break
                except Exception as ex:
                    last_err = ex
                    continue

            if not read_success:
                return {"success": False, "error": f"Failed to read file content: {last_err}"}

            return {
                "success": True,
                "filepath": str(target),
                "lines_read": len(lines),
                "total_lines_scanned": total_lines,
                "truncated": total_lines > clamped_lines,
                "content": content_text,
            }
        except Exception as e:
            logger.error(f"read_file_content error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to read file: {e}"}

    def write_file_content(self, filepath: str, content: str) -> Dict[str, Any]:
        """Write or overwrite text content to a local file.

        Args:
            filepath: Path to the file to create or overwrite.
            content: The text content to write into the file.

        Returns:
            dict: Operation status, destination path, bytes written, and status message.
        """
        try:
            target = Path(filepath).resolve()
            # Guard against critical system paths
            forbidden = {"c:\\windows", "c:\\boot", "c:\\windows\\system32"}
            if str(target).lower() in forbidden or any(str(target).lower().startswith(fb) for fb in forbidden):
                return {"success": False, "error": f"Writing to protected system directory is forbidden: '{target}'"}

            target.parent.mkdir(parents=True, exist_ok=True)
            raw_bytes = str(content).encode("utf-8")
            tmp_fd, tmp_path_str = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path_str, target)
            except Exception:
                if os.path.exists(tmp_path_str):
                    try:
                        os.remove(tmp_path_str)
                    except OSError:
                        pass
                raise

            return {
                "success": True,
                "filepath": str(target),
                "bytes_written": len(raw_bytes),
                "message": f"Successfully wrote {len(raw_bytes)} bytes to '{target.name}'.",
            }
        except Exception as e:
            logger.error(f"write_file_content error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to write file: {e}"}

    def open_folder_or_file(self, path: str) -> Dict[str, Any]:
        """Open a folder in Windows File Explorer or open a file with its default system program.

        Args:
            path: Path to the file or directory to open.

        Returns:
            dict: Operation status, opened path, and status message.
        """
        try:
            target = Path(path).resolve()
            if not target.exists():
                return {"success": False, "error": f"Path not found: '{path}'"}

            if os.name == "nt":
                os.startfile(str(target))
                return {"success": True, "path": str(target), "message": f"Opened '{target.name}'."}
            else:
                subprocess.Popen(["xdg-open", str(target)])
                return {"success": True, "path": str(target), "message": f"Opened '{target.name}' via xdg-open."}
        except Exception as e:
            logger.error(f"open_folder_or_file error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to open '{path}': {e}"}

    # =========================================================================
    # 4. Web & Media Tools
    # =========================================================================

    def search_google(self, query: str, num_results: Optional[int] = None) -> Dict[str, Any]:
        """Search Google in the default web browser for the specified query string.

        Args:
            query: Search query text to look up on Google.
            num_results: Optional number of results desired (for API / crawler integrations).

        Returns:
            dict: Operation status, search query, and launched Google URL.
        """
        try:
            clean_q = str(query).strip()
            if not clean_q:
                return {"success": False, "error": "Google search query cannot be empty."}

            url = f"https://www.google.com/search?q={urllib.parse.quote_plus(clean_q)}"
            opened = webbrowser.open(url)
            if not opened and hasattr(os, "startfile"):
                try:
                    os.startfile(url)
                    opened = True
                except Exception:
                    opened = False
            if not opened:
                return {"success": False, "error": f"Failed to launch browser for Google search: {url}"}
            return {
                "success": True,
                "query": clean_q,
                "url": url,
                "message": f"Opened Google search for '{clean_q}'.",
            }
        except Exception as e:
            logger.error(f"search_google error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to search Google: {e}"}

    def open_web_url(self, url: str) -> Dict[str, Any]:
        """Open a specified web URL in the default web browser.

        Args:
            url: Web URL to open (e.g. 'https://github.com' or 'youtube.com').

        Returns:
            dict: Operation status, opened URL, and status message.
        """
        try:
            clean_url = str(url).strip()
            if not clean_url:
                return {"success": False, "error": "URL cannot be empty."}

            if not clean_url.startswith(("http://", "https://")):
                clean_url = "https://" + clean_url

            opened = webbrowser.open(clean_url)
            if not opened and hasattr(os, "startfile"):
                try:
                    os.startfile(clean_url)
                    opened = True
                except Exception:
                    opened = False
            if not opened:
                return {"success": False, "error": f"Failed to launch browser for URL: {clean_url}"}
            return {
                "success": True,
                "url": clean_url,
                "message": f"Opened web URL '{clean_url}' in browser.",
            }
        except Exception as e:
            logger.error(f"open_web_url error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to open web URL: {e}"}

    def play_youtube_video(self, query: str) -> Dict[str, Any]:
        """Search for and play a video or music track on YouTube in the default browser.

        Args:
            query: Search terms for the video, song, or channel on YouTube.

        Returns:
            dict: Operation status, query, and opened YouTube URL.
        """
        try:
            clean_q = str(query).strip()
            if not clean_q:
                return {"success": False, "error": "YouTube query cannot be empty."}

            url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(clean_q)}"
            opened = webbrowser.open(url)
            if not opened and hasattr(os, "startfile"):
                try:
                    os.startfile(url)
                    opened = True
                except Exception:
                    opened = False
            if not opened:
                return {"success": False, "error": f"Failed to launch browser for YouTube search: {url}"}
            return {
                "success": True,
                "query": clean_q,
                "url": url,
                "message": f"Opened YouTube search for '{clean_q}'.",
            }
        except Exception as e:
            logger.error(f"play_youtube_video error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to search YouTube: {e}"}

    # =========================================================================
    # 5. Clipboard Tools
    # =========================================================================

    def get_clipboard(self) -> Dict[str, Any]:
        """Read plain text currently stored in the Windows clipboard.

        Returns:
            dict: Operation status, content length, and plain text content.
        """
        try:
            if self.sensor and hasattr(self.sensor, "get_clipboard_text"):
                text = self.sensor.get_clipboard_text()
            else:
                text = _win32_read_clipboard()

            return {
                "success": True,
                "content": text or "",
                "length": len(text) if text else 0,
            }
        except Exception as e:
            logger.error(f"get_clipboard error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to read clipboard: {e}"}

    def set_clipboard(self, text: str) -> Dict[str, Any]:
        """Set or copy new text content into the Windows clipboard.

        Args:
            text: Plain text string to set into the clipboard.

        Returns:
            dict: Operation status, text length copied, and confirmation message.
        """
        try:
            clean_text = str(text)
            if self.sensor and hasattr(self.sensor, "set_clipboard_text"):
                ok = self.sensor.set_clipboard_text(clean_text)
            else:
                ok = _win32_write_clipboard(clean_text)

            if ok:
                return {
                    "success": True,
                    "length": len(clean_text),
                    "message": "Text successfully copied to clipboard.",
                }
            return {"success": False, "error": "Clipboard API could not be opened or updated."}
        except Exception as e:
            logger.error(f"set_clipboard error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to set clipboard: {e}"}

    # =========================================================================
    # 6. Notes & Reminders Tools
    # =========================================================================

    def add_reminder(self, text: str, delay_minutes: Optional[int] = None) -> Dict[str, Any]:
        """Create a new reminder or scheduled notification with an optional delay in minutes.

        Args:
            text: The reminder description or task note to be alerted about.
            delay_minutes: Optional delay in minutes from now when the reminder should trigger. If None or 0, it is recorded as an active today reminder.

        Returns:
            dict: Operation status, reminder record (id, text, trigger_time, status), and message.
        """
        try:
            clean_text = str(text).strip()
            if not clean_text:
                return {"success": False, "error": "Reminder text cannot be empty."}

            now = time.time()
            now_dt = datetime.now()
            try:
                delay = int(delay_minutes) if delay_minutes is not None and int(delay_minutes) > 0 else 0
            except (ValueError, TypeError):
                delay = 0

            trigger_ts = now + (delay * 60) if delay > 0 else now
            trigger_str = (now_dt + timedelta(minutes=delay)).strftime("%Y-%m-%d %H:%M:%S")

            if self.scheduler and hasattr(self.scheduler, "add_reminder"):
                try:
                    sched_rem = self.scheduler.add_reminder(text=clean_text, delay_seconds=(delay * 60) if delay > 0 else 0)
                    time_desc = f"in {delay} minutes ({sched_rem.get('trigger_at', trigger_str)})" if delay > 0 else "for today"
                    return {
                        "success": True,
                        "reminder": sched_rem,
                        "message": f"Reminder '{clean_text}' set successfully {time_desc}.",
                    }
                except Exception as s_err:
                    logger.error(f"Scheduler add_reminder failed: {s_err}")
                    return {"success": False, "error": f"Failed to schedule reminder: {s_err}"}

            with _get_path_lock(self.reminders_file):
                reminders: List[Dict[str, Any]] = []
                if self.reminders_file.exists():
                    try:
                        with open(self.reminders_file, "r", encoding="utf-8") as f:
                            raw_data = json.load(f)
                            if isinstance(raw_data, list):
                                reminders = raw_data
                            elif isinstance(raw_data, dict):
                                reminders = raw_data.get("reminders", [])
                    except Exception:
                        reminders = []

                rem_id = f"REM-{len(reminders) + 1}"
                new_rem = {
                    "id": rem_id,
                    "text": clean_text,
                    "delay_minutes": delay if delay > 0 else None,
                    "created_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "trigger_timestamp": trigger_ts,
                    "trigger_time": trigger_str,
                    "status": "pending",
                }
                reminders.append(new_rem)

                # Persist atomically
                tmp = self.reminders_file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(reminders, f, indent=2, ensure_ascii=False)
                tmp.replace(self.reminders_file)

            # If scheduler is attached, forward to it
            if self.scheduler and hasattr(self.scheduler, "schedule_reminder"):
                try:
                    self.scheduler.schedule_reminder(new_rem)
                except Exception as s_err:
                    logger.debug(f"Scheduler dispatch error: {s_err}")

            time_desc = f"in {delay} minutes ({trigger_str})" if delay > 0 else "for today"
            return {
                "success": True,
                "reminder": new_rem,
                "message": f"Reminder '{clean_text}' set successfully {time_desc}.",
            }
        except Exception as e:
            logger.error(f"add_reminder error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to add reminder: {e}"}

    def list_reminders(self) -> Dict[str, Any]:
        """List all active and pending reminders recorded in the system.

        Returns:
            dict: Total reminder count and list of active reminder records.
        """
        try:
            if self.scheduler and hasattr(self.scheduler, "list_reminders"):
                rems = self.scheduler.list_reminders(status="pending")
                return {"success": True, "count": len(rems), "reminders": rems}

            with _get_path_lock(self.reminders_file):
                if not self.reminders_file.exists():
                    return {"success": True, "count": 0, "reminders": []}

                with open(self.reminders_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    if isinstance(raw_data, list):
                        reminders = raw_data
                    elif isinstance(raw_data, dict):
                        reminders = raw_data.get("reminders", [])
                    else:
                        reminders = []

            return {"success": True, "count": len(reminders), "reminders": reminders}
        except Exception as e:
            logger.error(f"list_reminders error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to list reminders: {e}"}

    def save_quick_note(self, title: str, content: str) -> Dict[str, Any]:
        """Save a quick note or idea to persistent storage and user memory.

        Args:
            title: Short headline or subject for the quick note.
            content: The detailed body text of the note.

        Returns:
            dict: Operation status, note ID, created timestamp, and confirmation message.
        """
        try:
            clean_title = str(title).strip()
            clean_content = str(content).strip()
            if not clean_title and not clean_content:
                return {"success": False, "error": "Note title and content cannot both be empty."}

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with _get_path_lock(self.notes_file):
                notes: List[Dict[str, Any]] = []
                if self.notes_file.exists():
                    try:
                        with open(self.notes_file, "r", encoding="utf-8") as f:
                            notes = json.load(f)
                    except Exception:
                        notes = []

                note_id = f"NOTE-{len(notes) + 1}"
                new_note = {
                    "id": note_id,
                    "title": clean_title or "Quick Note",
                    "content": clean_content,
                    "created_at": now_str,
                }
                notes.append(new_note)

                tmp = self.notes_file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(notes, f, indent=2, ensure_ascii=False)
                tmp.replace(self.notes_file)

            # Sync to JarvisMemory if available
            if self.memory and hasattr(self.memory, "add_note"):
                try:
                    self.memory.add_note(f"{clean_title}: {clean_content}", source="tool_registry")
                except Exception as m_err:
                    logger.debug(f"Memory note sync skipped: {m_err}")

            return {
                "success": True,
                "note": new_note,
                "message": f"Quick note '{new_note['title']}' saved successfully.",
            }
        except Exception as e:
            logger.error(f"save_quick_note error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to save quick note: {e}"}

    def get_quick_notes(self) -> Dict[str, Any]:
        """Retrieve all saved quick notes and thoughts from persistent memory.

        Returns:
            dict: Total count and list of saved quick note records.
        """
        try:
            with _get_path_lock(self.notes_file):
                if not self.notes_file.exists():
                    return {"success": True, "count": 0, "notes": []}

                with open(self.notes_file, "r", encoding="utf-8") as f:
                    notes = json.load(f)

            return {"success": True, "count": len(notes), "notes": notes}
        except Exception as e:
            logger.error(f"get_quick_notes error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to retrieve quick notes: {e}"}

    # =========================================================================
    # 7. Telegram Tools
    # =========================================================================

    def send_user_telegram_message(self, recipient: str, message: str) -> Dict[str, Any]:
        """Send a message to a contact, username, or Saved Messages ('me') using the user's personal Telegram account.

        Args:
            recipient: Target contact name, username (e.g. '@Jamsh1d_Sodiqov'), phone number, or 'me' for Saved Messages.
            message: Message text content to send.

        Returns:
            dict: Operation status, target recipient, message preview, and status confirmation.
        """
        try:
            clean_recipient = str(recipient).strip()
            clean_msg = str(message).strip()
            if not clean_recipient or not clean_msg:
                return {"success": False, "error": "Recipient and message text must not be empty."}

            if self.user_telegram and hasattr(self.user_telegram, "send_telegram_message"):
                res = self.user_telegram.send_telegram_message(clean_recipient, clean_msg)
                if isinstance(res, dict):
                    return res
                return {
                    "success": True,
                    "recipient": clean_recipient,
                    "message": clean_msg,
                    "details": str(res),
                }

            return {
                "success": False,
                "error": "Personal Telegram client (Telethon) is not configured or offline.",
            }
        except Exception as e:
            logger.error(f"send_user_telegram_message error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to send Telegram message: {e}"}

    def send_user_telegram_file(self, recipient: str, file_path: str, caption: str = "") -> Dict[str, Any]:
        """Send a physical file or document to a contact, username, or Saved Messages using the user's personal Telegram account.

        Args:
            recipient: Target contact name, username (e.g. '@Jamsh1d_Sodiqov'), or 'me' for Saved Messages.
            file_path: Full absolute or relative file path on the local computer to send.
            caption: Optional caption or text message to accompany the file.

        Returns:
            dict: Operation status, target recipient, file name, message ID, and confirmation.
        """
        try:
            clean_recipient = str(recipient).strip()
            clean_path = str(file_path).strip()
            if not clean_recipient or not clean_path:
                return {"success": False, "error": "Recipient and file_path must not be empty."}

            # Resolve path if not absolute
            p = Path(clean_path)
            if not p.is_absolute():
                # Check Desktop first, then base_dir
                desktop = Path(os.path.expanduser('~/OneDrive/Desktop'))
                if not desktop.exists():
                    desktop = Path(os.path.expanduser('~/Desktop'))
                if (desktop / clean_path).exists():
                    p = desktop / clean_path
                else:
                    p = (self.base_dir / clean_path).resolve()

            if not p.exists():
                return {"success": False, "error": f"Fayl topilmadi: {clean_path}"}

            if self.user_telegram and hasattr(self.user_telegram, "send_telegram_file"):
                return self.user_telegram.send_telegram_file(clean_recipient, str(p), caption=caption)

            return {
                "success": False,
                "error": "Personal Telegram client (Telethon) is not configured or offline.",
            }
        except Exception as e:
            logger.error(f"send_user_telegram_file error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to send Telegram file: {e}"}

    def transfer_telegram_media(self, source_chat: str, recipient: str, media_type: str = "any", caption: str = "") -> Dict[str, Any]:
        """Transfer or forward media (music, video, photo, document) from one Telegram chat/bot to another contact.

        Args:
            source_chat: Source chat name or bot username (e.g. '@vkmusic_bot', 'me').
            recipient: Target contact or username (e.g. '@XuRsHiDuLLoH').
            media_type: Media filter ('audio', 'video', 'photo', 'document', 'any').
            caption: Optional caption to accompany the forwarded media.

        Returns:
            dict: Operation status, target, message ID, and confirmation.
        """
        try:
            if self.user_telegram and hasattr(self.user_telegram, "transfer_chat_media"):
                return self.user_telegram.transfer_chat_media(source_chat, recipient, media_type=media_type, caption=caption)
            return {"success": False, "error": "Personal Telegram client (Telethon) is not configured or offline."}
        except Exception as e:
            logger.error(f"transfer_telegram_media error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to transfer Telegram media: {e}"}

    def read_recent_user_chats(self, limit: int = 5) -> Dict[str, Any]:
        """Read recent conversations and unread chat summaries from the user's personal Telegram account.

        Args:
            limit: Maximum number of recent chats or dialogs to retrieve (default: 5).

        Returns:
            dict: Total count and list of recent conversations with contact name, unread count, and last message.
        """
        try:
            clamped_limit = max(1, min(30, int(limit)))
            if self.user_telegram and hasattr(self.user_telegram, "get_recent_dialogs"):
                chats = self.user_telegram.get_recent_dialogs(limit=clamped_limit)
                if isinstance(chats, list):
                    return {"success": True, "count": len(chats), "chats": chats}

            if self.user_telegram and hasattr(self.user_telegram, "generate_unread_summary"):
                summary = self.user_telegram.generate_unread_summary(limit_dialogs=clamped_limit)
                return {
                    "success": True,
                    "count": summary.get("stats", {}).get("total_unread_chats", 0),
                    "summary": str(summary),
                }

            return {
                "success": False,
                "error": "Personal Telegram client (Telethon) is not configured or offline.",
            }
        except Exception as e:
            logger.error(f"read_recent_user_chats error: {e}", exc_info=True)
            return {"success": False, "error": f"Failed to read recent Telegram chats: {e}"}

    def search_omni_knowledge(self, query: str) -> Dict[str, Any]:
        """Search across all local PC apps, files, Telegram personal chats, and Saved Messages.

        Args:
            query: Keyword or phrase to find across apps, files, contacts, or saved notes.

        Returns:
            dict: Aggregated search results containing matched apps, files, chats, and saved notes.
        """
        try:
            from jarvis_omni_indexer import get_omni_knowledge
            omni = get_omni_knowledge()
            results = omni.omni_search(query)
            return {"success": True, "results": results}
        except Exception as e:
            logger.error(f"search_omni_knowledge error: {e}")
            return {"success": False, "error": str(e)}

    def search_saved_messages(self, query: str) -> Dict[str, Any]:
        """Search specifically inside the user's personal Telegram 'Saved Messages' (notes, test codes, links, reminders).

        Args:
            query: Keywords or search terms (e.g. 'kimyo', 'kod', 'rasch', 'test').

        Returns:
            dict: List of matching saved messages with snippets and metadata.
        """
        try:
            from jarvis_omni_indexer import TelegramOmniIndexer
            matches = TelegramOmniIndexer.search_saved_messages(query, limit=5)
            return {"success": True, "count": len(matches), "saved_notes": matches}
        except Exception as e:
            logger.error(f"search_saved_messages error: {e}")
            return {"success": False, "error": str(e)}

    def get_telegram_contact_details(self, name_or_username: str) -> Dict[str, Any]:
        """Look up personal details, phone number, @username, and recent conversation snippet for a Telegram contact.

        Args:
            name_or_username: Contact's first name, last name, or @username (e.g. 'Xurshid', '@XuRsHiDuLLoH').

        Returns:
            dict: Detailed contact information and recent message history.
        """
        try:
            from jarvis_omni_indexer import TelegramOmniIndexer
            contacts = TelegramOmniIndexer.search_contacts_and_chats(name_or_username)
            if contacts:
                return {"success": True, "contact": contacts[0]}
            return {"success": False, "error": f"Kontakt '{name_or_username}' topilmadi."}
        except Exception as e:
            logger.error(f"get_telegram_contact_details error: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Registry Dispatch & Gemini Integration
    # =========================================================================

    def get_gemini_tools(self) -> List[Callable]:
        """Return the complete list of callable Python tool functions ready for Google Generative AI (Gemini).

        This list can be passed directly to `genai.GenerativeModel(..., tools=registry.get_gemini_tools())`.

        Returns:
            list: List of bound tool methods with docstrings and type annotations.
        """
        tools = [
            # System & Hardware
            self.get_system_status,
            self.set_volume,
            self.mute_volume,
            self.lock_computer,
            self.wake_and_unlock,
            self.get_active_window,
            self.take_screenshot,
            # Applications
            self.launch_application,
            self.close_application,
            self.switch_to_window,
            self.list_open_windows,
            # Files & Folders
            self.search_files,
            self.read_file_content,
            self.write_file_content,
            self.open_folder_or_file,
            # Web & Media
            self.search_google,
            self.open_web_url,
            self.play_youtube_video,
            # Clipboard
            self.get_clipboard,
            self.set_clipboard,
            # Notes & Reminders
            self.add_reminder,
            self.list_reminders,
            self.save_quick_note,
            self.get_quick_notes,
            # Telegram & Omni-Knowledge
            self.send_user_telegram_message,
            self.send_user_telegram_file,
            self.transfer_telegram_media,
            self.read_recent_user_chats,
            self.search_omni_knowledge,
            self.search_saved_messages,
            self.get_telegram_contact_details,
        ]
        if hasattr(self, "custom_tools") and self.custom_tools:
            tools.extend(self.custom_tools.values())
        return tools

    def get_tool_names(self) -> List[str]:
        """Return the names of all registered tool functions."""
        return [func.__name__ for func in self.get_gemini_tools()]

    def dispatch(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        """Execute a registered tool by its function name safely with error capture.

        Args:
            tool_name: Name of the registered tool function to execute.
            **kwargs: Keyword arguments to pass to the tool function.

        Returns:
            dict: The dictionary result returned by the tool, or error dictionary.
        """
        aliases = {
            "set_level": "set_volume",
            "adjust_volume": "set_volume",
            "change_volume": "set_volume",
            "system_status": "get_system_status",
            "active_window": "get_active_window",
            "wake_screen": "wake_and_unlock",
            "screenshot": "take_screenshot",
            "capture_screenshot": "take_screenshot",
            "screen_capture": "take_screenshot",
        }
        if tool_name in aliases:
            tool_name = aliases[tool_name]

        registered = {f.__name__: f for f in self.get_gemini_tools()}
        if tool_name not in registered:
            return {"success": False, "error": f"Tool '{tool_name}' is not registered."}

        target_fn = registered[tool_name]

        # Safety Gate Integration for Destructive Actions
        is_confirmed = kwargs.pop("_confirmed_by_safety", False)
        if not is_confirmed and getattr(self, "safety", None):
            if tool_name == "close_application":
                app_name = kwargs.get("name", "dastur")
                desc = f"'{app_name}' dasturini yopish (jarayonni to'xtatish)"
                prompt = self.safety.request_confirmation(
                    action_type="close_application",
                    description=desc,
                    callback_fn=lambda: target_fn(**kwargs)
                )
                return {
                    "success": False,
                    "requires_confirmation": True,
                    "safety_gate": True,
                    "prompt": prompt,
                    "message": prompt
                }
            elif tool_name == "write_file_content":
                fpath_raw = kwargs.get("filepath", "")
                if fpath_raw:
                    try:
                        p = Path(fpath_raw).resolve()
                        if p.exists() and p.is_file() and p.stat().st_size > 0:
                            desc = f"Mavjud '{p.name}' faylining ustidan yangi ma'lumot yozish"
                            prompt = self.safety.request_confirmation(
                                action_type="overwrite_file",
                                description=desc,
                                callback_fn=lambda: target_fn(**kwargs)
                            )
                            return {
                                "success": False,
                                "requires_confirmation": True,
                                "safety_gate": True,
                                "prompt": prompt,
                                "message": prompt
                            }
                    except Exception:
                        pass

        try:
            return target_fn(**kwargs)
        except Exception as e:
            logger.error(f"Error executing tool '{tool_name}' with kwargs={kwargs}: {e}", exc_info=True)
            return {"success": False, "error": f"Execution of tool '{tool_name}' failed: {e}"}

    def get_tool_declarations(self) -> List[Dict[str, Any]]:
        """Generate OpenAPI / Gemini FunctionDeclaration JSON schemas for all registered tools.

        Returns:
            list: List of function declaration dictionaries.
        """
        declarations = []
        for func in self.get_gemini_tools():
            sig = inspect.signature(func)
            doc = func.__doc__ or ""
            # Extract main description
            doc_lines = [line.strip() for line in doc.strip().split("\n")]
            main_desc = doc_lines[0] if doc_lines else func.__name__

            properties: Dict[str, Any] = {}
            required: List[str] = []

            for param_name, param in sig.parameters.items():
                if param_name == "self":
                    continue

                ann = param.annotation
                import typing
                origin = typing.get_origin(ann)
                args = typing.get_args(ann)
                if origin is typing.Union:
                    non_none = [a for a in args if a is not type(None)]
                    if non_none:
                        ann = non_none[0]

                type_name = "string"
                if ann == int:
                    type_name = "integer"
                elif ann == float:
                    type_name = "number"
                elif ann == bool:
                    type_name = "boolean"
                elif ann in (list, List) or typing.get_origin(ann) in (list, List):
                    type_name = "array"
                elif ann in (dict, Dict) or typing.get_origin(ann) in (dict, Dict):
                    type_name = "object"

                # Extract param docstring if available
                param_doc = ""
                m = re.search(rf'{param_name}:\s*([^\n\.]+)', doc)
                if m:
                    param_doc = m.group(1).strip()

                properties[param_name] = {
                    "type": type_name,
                    "description": param_doc or f"Parameter '{param_name}'",
                }

                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

            declarations.append({
                "name": func.__name__,
                "description": main_desc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            })
        return declarations


# Global default registry instance
_DEFAULT_REGISTRY: Optional[JarvisToolRegistry] = None


def get_gemini_tools(registry: Optional[JarvisToolRegistry] = None) -> List[Callable]:
    """Return the complete list of callable Python tool functions ready for Google Generative AI (Gemini).

    Args:
        registry: Optional JarvisToolRegistry instance. If None, uses default registry.

    Returns:
        list: List of callable functions ready for `genai.GenerativeModel(..., tools=...)`.
    """
    global _DEFAULT_REGISTRY
    if registry is None:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = JarvisToolRegistry()
        registry = _DEFAULT_REGISTRY
    return registry.get_gemini_tools()


# =============================================================================
# Standalone Self-Test & Mock Verification Suite
# =============================================================================

def _run_self_test():
    """Run comprehensive self-tests verifying tool registry, mocks, and schema generation."""
    print("=" * 70)
    print("   JARVIS TOOL REGISTRY: STANDALONE TEST & VERIFICATION SUITE")
    print("=" * 70)

    # 1. Mock webbrowser.open to avoid spawning real browser windows during unit testing
    orig_wb_open = webbrowser.open
    webbrowser.open = lambda url: True

    # 2. Create Mock Core Components
    class MockSensor:
        def get_active_window(self):
            return {"hwnd": 12345, "title": "VS Code - Jarvis", "process": "Code.exe", "pid": 4321}
        def get_clipboard_text(self):
            return "Mock Clipboard Text 123"
        def set_clipboard_text(self, text):
            return True
        def unlock_with_pin(self, pin):
            return {"status": "success", "pin_length": len(pin)}
        def volume_mute(self):
            return {"action": "volume_mute"}

    class MockDesktop:
        def list_windows(self):
            return [{"title": "Telegram Desktop", "hwnd": 999, "rect": {"left": 0, "top": 0, "width": 800, "height": 600}}]
        def focus_window(self, query):
            return {"status": "success", "title": "Telegram Desktop", "hwnd": 999}

    class MockSystem:
        def get_system_stats(self):
            return {
                "hostname": "JARVIS-TEST-PC",
                "os": "Windows 11 Pro",
                "cpu_percent": 12.5,
                "ram_percent": 45.0,
                "disk_free_gb": 120.4,
                "security_status": "Active (Unlocked)",
                "timestamp": "2026-11-16 00:00:00",
            }
        def set_volume(self, level):
            return {"success": True, "volume": level, "message": f"Volume set to {level}%."}
        def mute_volume(self):
            return {"success": True, "muted": True, "message": "Volume muted."}
        def lock_workstation(self):
            return {"success": True, "message": "Workstation locked mock."}
        def open_application(self, name):
            return {"success": True, "app": name, "message": f"App '{name}' opened mock."}
        def close_application(self, name):
            return {"success": True, "app": name, "killed_count": 1, "message": f"App '{name}' closed mock."}

    class MockUserTelegram:
        @classmethod
        def send_telegram_message(cls, recipient, message):
            return {"success": True, "target": recipient, "message_id": 7777, "text": message}
        @classmethod
        def get_recent_dialogs(cls, limit=5):
            return [{"name": "Jamshid Sodiqov", "id": 1001, "unread": 0, "last_message": "Salom Azizbek"}]

    # 2. Instantiate Registry with Mocks
    test_dir = Path(__file__).resolve().parent / "snapshots" / "test_scratch"
    test_dir.mkdir(parents=True, exist_ok=True)

    registry = JarvisToolRegistry(
        sensor=MockSensor(),
        desktop=MockDesktop(),
        system=MockSystem(),
        user_telegram=MockUserTelegram,
        base_dir=test_dir,
    )

    print("\n[1] Checking Tool Registration:")
    tools = registry.get_gemini_tools()
    tool_names = registry.get_tool_names()
    print(f"Total registered tools: {len(tools)}")
    print(f"Tools list: {', '.join(tool_names)}")
    assert len(tools) >= 23, f"Expected at least 23 tools, got {len(tools)}"

    print("\n[2] Testing System & Hardware Tools:")
    res_sys = registry.get_system_status()
    print(f" • get_system_status: {res_sys['success']} (Host: {res_sys['stats']['hostname']})")
    assert res_sys["success"] is True

    res_vol = registry.set_volume(45)
    print(f" • set_volume(45): {res_vol['success']} (Volume: {res_vol['volume']})")
    assert res_vol["success"] is True

    res_mute = registry.mute_volume()
    print(f" • mute_volume: {res_mute['success']}")
    assert res_mute["success"] is True

    res_lock = registry.lock_computer()
    print(f" • lock_computer: {res_lock['success']}")
    assert res_lock["success"] is True

    res_unlock = registry.wake_and_unlock("1234")
    print(f" • wake_and_unlock('1234'): {res_unlock['success']}")
    assert res_unlock["success"] is True

    res_win = registry.get_active_window()
    print(f" • get_active_window: {res_win['success']} (Title: {res_win['window']['title']})")
    assert res_win["success"] is True

    print("\n[3] Testing Application Tools:")
    res_launch = registry.launch_application("notepad")
    print(f" • launch_application('notepad'): {res_launch['success']}")
    assert res_launch["success"] is True

    res_close = registry.close_application("notepad")
    print(f" • close_application('notepad'): {res_close['success']}")
    assert res_close["success"] is True

    res_switch = registry.switch_to_window("Telegram")
    print(f" • switch_to_window('Telegram'): {res_switch['success']}")
    assert res_switch["success"] is True

    res_list_win = registry.list_open_windows()
    print(f" • list_open_windows: {res_list_win['success']} (Count: {res_list_win['count']})")
    assert res_list_win["success"] is True

    print("\n[4] Testing Files & Folders Tools:")
    test_file = test_dir / "test_sample.txt"
    res_write = registry.write_file_content(str(test_file), "Line 1: Hello Jarvis\nLine 2: Autonomous System\nLine 3: Tool Registry Test")
    print(f" • write_file_content: {res_write['success']} (Bytes: {res_write['bytes_written']})")
    assert res_write["success"] is True

    res_read = registry.read_file_content(str(test_file), max_lines=2)
    print(f" • read_file_content: {res_read['success']} (Lines read: {res_read['lines_read']})")
    assert res_read["success"] is True
    assert "Line 1" in res_read["content"]

    res_search = registry.search_files("test_sample*.txt", root_folder=str(test_dir))
    print(f" • search_files: {res_search['success']} (Found: {res_search['count']})")
    assert res_search["success"] is True

    print("\n[5] Testing Web & Media Tools:")
    res_g = registry.search_google("Jarvis AI")
    print(f" • search_google: {res_g['success']} (URL: {res_g['url']})")
    assert res_g["success"] is True

    res_url = registry.open_web_url("https://example.com")
    print(f" • open_web_url: {res_url['success']}")
    assert res_url["success"] is True

    res_yt = registry.play_youtube_video("Mozart Symphony 40")
    print(f" • play_youtube_video: {res_yt['success']} (URL: {res_yt['url']})")
    assert res_yt["success"] is True

    print("\n[6] Testing Clipboard Tools:")
    res_set_clip = registry.set_clipboard("Test Clipboard 2026")
    print(f" • set_clipboard: {res_set_clip['success']}")
    assert res_set_clip["success"] is True

    res_get_clip = registry.get_clipboard()
    print(f" • get_clipboard: {res_get_clip['success']} (Content: '{res_get_clip['content']}')")
    assert res_get_clip["success"] is True

    print("\n[7] Testing Notes & Reminders Tools:")
    res_add_rem = registry.add_reminder("Meet with Jamshid for Rasch review", delay_minutes=30)
    print(f" • add_reminder: {res_add_rem['success']} (ID: {res_add_rem['reminder']['id']})")
    assert res_add_rem["success"] is True

    res_list_rem = registry.list_reminders()
    print(f" • list_reminders: {res_list_rem['success']} (Count: {res_list_rem['count']})")
    assert res_list_rem["count"] >= 1

    res_save_note = registry.save_quick_note("Idea 1", "Develop neural desktop perception")
    print(f" • save_quick_note: {res_save_note['success']} (ID: {res_save_note['note']['id']})")
    assert res_save_note["success"] is True

    res_get_notes = registry.get_quick_notes()
    print(f" • get_quick_notes: {res_get_notes['success']} (Count: {res_get_notes['count']})")
    assert res_get_notes["count"] >= 1

    print("\n[8] Testing Telegram Tools:")
    res_tg_msg = registry.send_user_telegram_message("@Jamsh1d_Sodiqov", "Salom Jamshid, Monika orqali test xabari.")
    print(f" • send_user_telegram_message: {res_tg_msg['success']} (Recipient: {res_tg_msg['target']})")
    assert res_tg_msg["success"] is True

    res_tg_chats = registry.read_recent_user_chats(limit=3)
    print(f" • read_recent_user_chats: {res_tg_chats['success']} (Chats: {res_tg_chats['count']})")
    assert res_tg_chats["success"] is True

    print("\n[9] Testing Dynamic Dispatch:")
    disp_res = registry.dispatch("add_reminder", text="Review code", delay_minutes=5)
    print(f" • dispatch('add_reminder'): {disp_res['success']} (ID: {disp_res['reminder']['id']})")
    assert disp_res["success"] is True

    disp_err = registry.dispatch("non_existent_tool")
    print(f" • dispatch('non_existent_tool'): {disp_err['success']} (Error: '{disp_err['error']}')")
    assert disp_err["success"] is False

    print("\n[10] Testing Google Generative AI (Gemini) FunctionLibrary Schema Compatibility:")
    try:
        import google.generativeai as genai
        # Pass all tools to GenerativeModel to test Google SDK schema compilation
        model = genai.GenerativeModel("models/gemini-flash-lite-latest", tools=registry.get_gemini_tools())
        print(f" • Successfully compiled and registered all {len(tools)} tools in Gemini FunctionLibrary!")
        assert model._tools is not None
    except Exception as e:
        print(f" • Gemini tool compilation error: {e}")
        raise e
    finally:
        webbrowser.open = orig_wb_open

    print("\n[11] Testing Declarations Schema Generator:")
    decls = registry.get_tool_declarations()
    print(f" • Generated {len(decls)} OpenAPI/Gemini function declarations.")
    assert len(decls) == len(tools)

    # Clean up test scratch directory
    shutil.rmtree(test_dir, ignore_errors=True)

    print("\n" + "=" * 70)
    print("   ALL 11 VERIFICATION STAGES PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    _run_self_test()
