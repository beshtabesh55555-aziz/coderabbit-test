"""
JARVIS Desktop Controller Module
================================
Provides full GUI automation, mouse cursor control, keyboard typing,
window management, and visual screen automation on Windows OS.
Built with native Windows API (ctypes / user32 / gdi32) and PIL.
"""

import sys
import os
import re
import time
import math
import logging
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from PIL import ImageGrab

logger = logging.getLogger("JarvisDesktop")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-DESKTOP] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

SCREENSHOT_DIR = Path(r"C:\Users\User\.gemini\antigravity\scratch\jarvis-system\snapshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD)
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT)
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


VK_MAP = {
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "space": 0x20, "backspace": 0x08, "back": 0x08,
    "win": 0x5B, "cmd": 0x5B, "super": 0x5B,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "shift": 0x10,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B
}


class FailSafeException(Exception):
    """Raised when mouse hits (0, 0) fail-safe corner."""
    pass


class JarvisDesktop:
    """Complete desktop and GUI controller for Monika / JARVIS."""

    def __init__(self, failsafe: bool = True):
        self.failsafe = failsafe
        self._hdesk = None
        self._attach_desktop()

    def _attach_desktop(self):
        """Ensure thread is attached to the active input desktop without thrashing handles."""
        if getattr(self, "_hdesk", None):
            return
        try:
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            if hdesk:
                if user32.SetThreadDesktop(hdesk):
                    self._hdesk = hdesk
                else:
                    user32.CloseDesktop(hdesk)
        except Exception:
            pass

    def close(self):
        """Release attached desktop handle."""
        if getattr(self, "_hdesk", None):
            try:
                user32.CloseDesktop(self._hdesk)
            except Exception:
                pass
            self._hdesk = None

    def __del__(self):
        self.close()

    def check_failsafe(self, target_x: Optional[int] = None, target_y: Optional[int] = None):
        """Verify cursor is not in the fail-safe abort position (0, 0).
        If user explicitly commands a movement away from (0, 0), allow it."""
        if not self.failsafe:
            return
        if target_x is not None and target_y is not None and (target_x > 20 or target_y > 20):
            return
        try:
            x, y = self.get_cursor_pos()
        except RuntimeError:
            return

        if x == 0 and y == 0 and target_x is None:
            # Check if deliberate: only raise if cursor stayed at (0, 0)
            time.sleep(0.05)
            try:
                x2, y2 = self.get_cursor_pos()
            except RuntimeError:
                return
            if x2 == 0 and y2 == 0:
                logger.warning("FAIL-SAFE TRIGGERED: Mouse is parked at (0, 0).")
                raise FailSafeException("Mouse hit top-left corner (0, 0). Fail-safe triggered.")

    def get_screen_size(self) -> Tuple[int, int]:
        """Return (width, height) of primary monitor."""
        self._attach_desktop()
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        return w, h

    def get_cursor_pos(self) -> Tuple[int, int]:
        """Return current mouse cursor (x, y) coordinates.
        Raises RuntimeError if Win32 GetCursorPos fails, preventing false-positive (0, 0) failsafes."""
        self._attach_desktop()
        pt = POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            # Retry once after refreshing desktop attachment (e.g. desktop switch / UAC)
            self._hdesk = None
            self._attach_desktop()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                raise RuntimeError("Failed to read cursor position via GetCursorPos.")
        return pt.x, pt.y

    def mouse_move(self, x: int, y: int, smooth: bool = True, steps: int = 20) -> Dict[str, Any]:
        """Move cursor to (x, y) with optional smooth trajectory."""
        self._attach_desktop()
        self.check_failsafe()

        x, y = int(x), int(y)
        w, h = self.get_screen_size()
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))

        if smooth:
            curr_x, curr_y = self.get_cursor_pos()
            for i in range(1, steps + 1):
                # Cosine easing for natural human-like movement
                t = i / steps
                ease = (1.0 - math.cos(t * math.pi)) / 2.0
                step_x = int(curr_x + (x - curr_x) * ease)
                step_y = int(curr_y + (y - curr_y) * ease)
                user32.SetCursorPos(step_x, step_y)
                time.sleep(0.005)
        else:
            user32.SetCursorPos(x, y)

        final_x, final_y = self.get_cursor_pos()
        success = (final_x == x and final_y == y)
        logger.info(f"Cursor moved to ({final_x}, {final_y}) (target: {x}, {y})")
        return {
            "success": success,
            "x": final_x,
            "y": final_y,
            "target_x": x,
            "target_y": y,
            "status": "success" if success else "partial",
        }

    def mouse_click(self, x: Optional[int] = None, y: Optional[int] = None,
                    button: str = "left", clicks: int = 1) -> Dict[str, Any]:
        """Perform mouse click at given or current coordinates."""
        self._attach_desktop()
        self.check_failsafe()

        if x is not None and y is not None:
            self.mouse_move(x, y, smooth=True)
            time.sleep(0.05)

        btn = button.lower()
        if btn == "right":
            down_flag, up_flag = 0x0008, 0x0010
        elif btn == "middle":
            down_flag, up_flag = 0x0020, 0x0040
        else:
            down_flag, up_flag = 0x0002, 0x0004

        for i in range(clicks):
            user32.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(up_flag, 0, 0, 0, 0)
            if i < clicks - 1:
                time.sleep(0.1)

        cur_x, cur_y = self.get_cursor_pos()
        logger.info(f"Mouse clicked {btn} ({clicks}x) at ({cur_x}, {cur_y})")
        return {"button": btn, "clicks": clicks, "x": cur_x, "y": cur_y, "status": "success"}

    def mouse_double_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Perform double click."""
        return self.mouse_click(x=x, y=y, button="left", clicks=2)

    def mouse_right_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Perform right click."""
        return self.mouse_click(x=x, y=y, button="right", clicks=1)

    def mouse_drag(self, start_x: int, start_y: int, end_x: int, end_y: int,
                   button: str = "left") -> Dict[str, Any]:
        """Drag mouse from start to end coordinates."""
        self._attach_desktop()
        self.check_failsafe()

        self.mouse_move(start_x, start_y, smooth=True)
        time.sleep(0.08)

        down_flag = 0x0008 if button.lower() == "right" else 0x0002
        up_flag = 0x0010 if button.lower() == "right" else 0x0004

        user32.mouse_event(down_flag, 0, 0, 0, 0)
        time.sleep(0.08)

        steps = 25
        for i in range(1, steps + 1):
            curr_x = int(start_x + (end_x - start_x) * (i / steps))
            curr_y = int(start_y + (end_y - start_y) * (i / steps))
            user32.SetCursorPos(curr_x, curr_y)
            time.sleep(0.01)

        time.sleep(0.05)
        user32.mouse_event(up_flag, 0, 0, 0, 0)
        logger.info(f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y})")
        return {"from": (start_x, start_y), "to": (end_x, end_y), "status": "success"}

    def mouse_scroll(self, amount: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Scroll mouse wheel (positive = up, negative = down)."""
        self._attach_desktop()
        self.check_failsafe()
        if x is not None and y is not None:
            self.mouse_move(x, y, smooth=False)
            time.sleep(0.03)

        user32.mouse_event(0x0800, 0, 0, int(amount), 0)
        logger.info(f"Scrolled mouse wheel: {amount}")
        return {"amount": amount, "status": "success"}

    KEY_ALIASES = {
        "command": "win",
        "cmd": "win",
        "super": "win",
        "windows": "win",
        "control": "ctrl",
        "alternate": "alt",
        "escape": "esc",
        "delete": "del",
        "return": "enter",
    }

    RESTRICTED_COMBOS = [
        {"win", "r"},
        {"win", "x"},
        {"ctrl", "alt", "del"},
        {"ctrl", "shift", "esc"},
        {"alt", "f4"},
    ]
    RESTRICTED_TYPE_REGEX = re.compile(
        r"\b(format\s+[a-z]:|rmdir\s+/[sq]|del\s+/[fq]|powershell(\.exe)?\s+(-[ce]|-[eE]nc)|cmd(\.exe)?\s+/[ck]|shutdown\s+/[st])",
        re.IGNORECASE
    )

    def key_press(self, key_or_combo: str) -> Dict[str, Any]:
        """Press single key or combination (e.g. 'ctrl+c', 'alt+tab')."""
        self._attach_desktop()
        raw_parts = [k.strip().lower() for k in str(key_or_combo).split("+")]
        normalized_parts = {self.KEY_ALIASES.get(p, p) for p in raw_parts if p}

        for restricted in self.RESTRICTED_COMBOS:
            if restricted == normalized_parts:
                logger.warning(f"Restricted key combination rejected: {key_or_combo} (normalized: {normalized_parts})")
                return {"error": f"Shortcut '{key_or_combo}' is restricted by desktop safety policy.", "status": "blocked"}

        vk_codes = []
        for p in raw_parts:
            canonical = self.KEY_ALIASES.get(p, p)
            if canonical in VK_MAP:
                vk_codes.append(VK_MAP[canonical])
            elif p in VK_MAP:
                vk_codes.append(VK_MAP[p])
            elif len(p) == 1 and p.isalnum():
                vk_codes.append(ord(p.upper()))
            else:
                return {"error": f"Unknown key code: '{p}'"}

        # Press keys down in forward order with failsafe protection and clean release
        pressed_vks = []
        try:
            self.check_failsafe()
            for vk in vk_codes:
                self.check_failsafe()
                user32.keybd_event(vk, 0, 0, 0)
                pressed_vks.append(vk)
                time.sleep(0.02)
        finally:
            # Always ensure any pressed keys are released even if aborted
            for vk in reversed(pressed_vks):
                user32.keybd_event(vk, 0, 2, 0)
                time.sleep(0.01)

        logger.info(f"Key combination pressed: {key_or_combo}")
        return {"key": key_or_combo, "status": "success"}

    def keyboard_type(self, text: str, press_enter: bool = False, delay: float = 0.01) -> Dict[str, Any]:
        """Type Unicode text reliably via SendInput."""
        self._attach_desktop()
        self.check_failsafe()
        raw_text = str(text)
        if self.RESTRICTED_TYPE_REGEX.search(raw_text):
            logger.warning(f"Restricted text typing rejected by regex: {text[:50]}")
            return {"error": "Command injection detected: Text contains restricted shell commands.", "status": "blocked"}

        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002

        for i, char in enumerate(str(text)):
            if i % 10 == 0:
                self.check_failsafe()
            scan_code = ord(char)
            # Key down
            inp_down = INPUT()
            inp_down.type = 1
            inp_down.u.ki.wVk = 0
            inp_down.u.ki.wScan = scan_code
            inp_down.u.ki.dwFlags = KEYEVENTF_UNICODE
            inp_down.u.ki.time = 0
            inp_down.u.ki.dwExtraInfo = 0

            # Key up
            inp_up = INPUT()
            inp_up.type = 1
            inp_up.u.ki.wVk = 0
            inp_up.u.ki.wScan = scan_code
            inp_up.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            inp_up.u.ki.time = 0
            inp_up.u.ki.dwExtraInfo = 0

            user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
            time.sleep(delay)
            user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
            time.sleep(delay)

        if press_enter:
            time.sleep(0.05)
            self.key_press("enter")

        logger.info(f"Typed {len(text)} characters: '{text[:30]}...'")
        return {"text": text, "length": len(text), "status": "success"}

    def take_screenshot(self, filename: Optional[str] = None) -> Dict[str, Any]:
        """Capture screenshot of desktop safely in clean context and return path & screen info."""
        if not filename:
            filename = f"desktop_{int(time.time() * 1000)}.png"
        if not filename.endswith((".png", ".jpg")):
            filename += ".png"

        save_path = SCREENSHOT_DIR / filename

        # 1. First try in-process
        img = None
        try:
            self._attach_desktop()
            img = ImageGrab.grab()
        except Exception:
            img = None

        # 2. If in-process failed (e.g. OpenCV / COM / DirectShow thread desktop conflict), run isolated worker
        if img is None:
            import subprocess
            py_code = (
                "import ctypes\n"
                "user32 = ctypes.windll.user32\n"
                "hdesk = user32.OpenInputDesktop(0, False, 0x01FF)\n"
                "if hdesk: user32.SetThreadDesktop(hdesk)\n"
                "from PIL import ImageGrab\n"
                f"img = ImageGrab.grab()\n"
                f"img.save(r'{save_path}')\n"
                "print('OK')\n"
            )
            try:
                proc = subprocess.run([sys.executable, "-c", py_code], capture_output=True, text=True, timeout=5)
                if proc.returncode != 0 or not save_path.exists():
                    logger.error(f"Isolated screenshot failed: {proc.stderr}")
                    return {"status": "error", "message": proc.stderr or "Screenshot worker failed"}
            except Exception as e:
                logger.error(f"Subprocess grab error: {e}")
                return {"status": "error", "message": str(e)}
        else:
            img.save(str(save_path))

        cur_x, cur_y = self.get_cursor_pos()
        w, h = self.get_screen_size()
        logger.info(f"Desktop screenshot saved: {save_path} ({w}x{h})")
        return {
            "path": str(save_path),
            "width": w,
            "height": h,
            "cursor": {"x": cur_x, "y": cur_y},
            "status": "success"
        }

    def list_windows(self) -> List[Dict[str, Any]]:
        """Enumerate all open visible desktop application windows."""
        self._attach_desktop()
        windows = []

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
                            "hwnd": hwnd,
                            "title": title,
                            "rect": {"left": rect.left, "top": rect.top, "width": w, "height": h}
                        })
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        return windows

    def focus_window(self, title_query: str) -> Dict[str, Any]:
        """Bring window matching title_query to foreground."""
        self._attach_desktop()
        q = title_query.lower()
        windows = self.list_windows()
        for win in windows:
            if q in win["title"].lower():
                hwnd = win["hwnd"]
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                logger.info(f"Focused window: '{win['title']}' (HWND: {hwnd})")
                return {"title": win["title"], "hwnd": hwnd, "status": "success"}

        return {"error": f"No active window matched '{title_query}'", "status": "not_found"}


if __name__ == "__main__":
    dt = JarvisDesktop()
    w, h = dt.get_screen_size()
    cx, cy = dt.get_cursor_pos()
    print(f"Screen size: {w}x{h}, Cursor position: ({cx}, {cy})")
    wins = dt.list_windows()
    print(f"Found {len(wins)} windows. Top 3:")
    for win in wins[:3]:
        print(f" - {win['title']}")
