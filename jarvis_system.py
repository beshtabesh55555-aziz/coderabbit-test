"""JARVIS System Controller
=========================
Windows administrative automation, system monitoring, volume control,
application management, power management, native speech synthesis,
and secure command execution.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import psutil

try:
    import win32com.client
    HAS_WIN32COM = True
except ImportError:
    win32com = None  # type: ignore
    HAS_WIN32COM = False

try:
    import config
except ImportError:
    config = None  # type: ignore


class JarvisSystem:
    """Core System Controller for JARVIS on Windows."""

    # Virtual key codes
    VK_SPACE = 0x20
    VK_RETURN = 0x0D
    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    # Screen / Power Win32 constants
    HWND_BROADCAST = 0xFFFF
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    ES_DISPLAY_REQUIRED = 0x00000002
    ES_SYSTEM_REQUIRED = 0x00000001

    # Protected system processes blacklist (strictly cannot be closed)
    PROTECTED_PROCESSES: Set[str] = {
        "explorer.exe",
        "python.exe",
        "pythonw.exe",
        "system",
        "registry",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "winlogon.exe",
        "dwm.exe",
        "taskhostw.exe",
        "sihost.exe",
        "conhost.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "taskmgr.exe",
    }

    # Common application aliases
    APP_ALIASES: Dict[str, str] = {
        "notepad": "notepad.exe",
        "chrome": "chrome.exe",
        "google chrome": "chrome.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "vscode": "code.cmd",
        "code": "code.cmd",
        "edge": "msedge.exe",
        "microsoft edge": "msedge.exe",
        "explorer": "explorer.exe",
        "file explorer": "explorer.exe",
        "taskmgr": "taskmgr.exe",
        "task manager": "taskmgr.exe",
        "cmd": "cmd.exe",
        "command prompt": "cmd.exe",
        "powershell": "powershell.exe",
        "terminal": "wt.exe",
        "settings": "ms-settings:",
        "mspaint": "mspaint.exe",
        "paint": "mspaint.exe",
    }

    def __init__(self) -> None:
        self._current_volume: int = 50
        self._muted: bool = False
        self._tts_voice = None
        self._init_tts()

    def _init_tts(self) -> None:
        """Initialize native Windows SAPI TTS engine if available."""
        if HAS_WIN32COM:
            try:
                self._tts_voice = win32com.client.Dispatch("SAPI.SpVoice")
            except Exception:
                self._tts_voice = None

    # =========================================================================
    # 1. System Metrics
    # =========================================================================

    def get_system_stats(self) -> Dict[str, Any]:
        """Collect real-time CPU, RAM, Battery, Disk, and security status.

        Returns:
            Dict containing detailed system metrics.
        """
        # CPU
        cpu_pct = psutil.cpu_percent(interval=0.1)

        # RAM
        vm = psutil.virtual_memory()
        ram_pct = vm.percent
        ram_used_gb = round(vm.used / (1024**3), 2)
        ram_total_gb = round(vm.total / (1024**3), 2)

        # Battery
        battery = psutil.sensors_battery()
        if battery is not None:
            battery_pct: Optional[float] = float(battery.percent)
            battery_plugged: Optional[bool] = bool(battery.power_plugged)
            battery_secsleft: Optional[int] = battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else None
        else:
            battery_pct = None
            battery_plugged = None
            battery_secsleft = None

        # Disk (Primary drive C:)
        try:
            disk = psutil.disk_usage("C:\\")
            disk_pct = disk.percent
            disk_free_gb = round(disk.free / (1024**3), 2)
            disk_total_gb = round(disk.total / (1024**3), 2)
        except Exception:
            disk_pct = 0.0
            disk_free_gb = 0.0
            disk_total_gb = 0.0

        # Uptime
        boot_time = psutil.boot_time()
        uptime_seconds = max(0.0, time.time() - boot_time)
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        uptime_str = f"{hours}h {minutes}m"

        # Security Status (detect if workstation desktop is locked)
        security_status = self.get_security_status()

        return {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "cpu_percent": cpu_pct,
            "ram_percent": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "battery_percent": battery_pct,
            "battery_plugged": battery_plugged,
            "battery_secsleft": battery_secsleft,
            "disk_percent": disk_pct,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "uptime_seconds": round(uptime_seconds, 1),
            "uptime_formatted": uptime_str,
            "security_status": security_status,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_lock_status(self) -> Dict[str, Any]:
        """Check whether the Windows workstation is currently locked or active.

        Uses Win32 OpenInputDesktop with DESKTOP_SWITCHDESKTOP (0x0100).
        When the session is locked, Windows switches input to the Winlogon desktop,
        causing OpenInputDesktop to return a NULL handle (access denied).

        Returns:
            Dict containing is_locked (bool), security_status (str), and timestamp (float).
        """
        now = time.time()
        try:
            user32 = ctypes.windll.user32
            # 0x0100 is DESKTOP_SWITCHDESKTOP permission
            h_desktop = user32.OpenInputDesktop(0, False, 0x0100)
            if h_desktop:
                user32.CloseDesktop(h_desktop)
                return {
                    "is_locked": False,
                    "security_status": "Active (Unlocked)",
                    "timestamp": now,
                }
            else:
                return {
                    "is_locked": True,
                    "security_status": "Locked",
                    "timestamp": now,
                }
        except Exception as err:
            return {
                "is_locked": False,
                "security_status": "Active (Normal)",
                "timestamp": now,
                "error": str(err),
            }

    def get_security_status(self) -> str:
        """Check whether the Windows workstation is currently locked or active."""
        status = self.get_lock_status()
        return status.get("security_status", "Active (Unlocked)")

    def format_stats_report(self) -> str:
        """Produce a clean human-friendly markdown summary of system metrics."""
        stats = self.get_system_stats()
        batt_str = (
            f"{stats['battery_percent']}% ({'Charging/AC' if stats['battery_plugged'] else 'On Battery'})"
            if stats["battery_percent"] is not None
            else "N/A (Desktop/AC)"
        )
        return (
            f"💻 *JARVIS System Diagnostics*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Host:* `{stats['hostname']}` ({stats['os']})\n"
            f"• *Security:* `{stats['security_status']}`\n"
            f"• *CPU Usage:* `{stats['cpu_percent']}%`\n"
            f"• *RAM Usage:* `{stats['ram_percent']}%` ({stats['ram_used_gb']}GB / {stats['ram_total_gb']}GB)\n"
            f"• *Disk (C:):* `{stats['disk_percent']}%` ({stats['disk_free_gb']}GB free of {stats['disk_total_gb']}GB)\n"
            f"• *Battery:* `{batt_str}`\n"
            f"• *Uptime:* `{stats['uptime_formatted']}`\n"
            f"• *Time:* `{stats['timestamp']}`"
        )

    # =========================================================================
    # 2. Workstation Security
    # =========================================================================

    def lock_workstation(self) -> Dict[str, Any]:
        """Immediately lock the Windows workstation."""
        try:
            res = ctypes.windll.user32.LockWorkStation()
            if res != 0:
                return {"success": True, "message": "Workstation locked successfully via Win32 API."}
        except Exception:
            # Fallback to rundll32
            pass

        try:
            subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
            return {"success": True, "message": "Workstation locked via rundll32 fallback."}
        except Exception as err:
            return {"success": False, "message": f"Failed to lock workstation: {err}", "error": str(err)}

    def wake_display(self) -> Dict[str, Any]:
        """Wake up physical display monitor backlight and prevent sleep.

        1. Broadcasts SC_MONITORPOWER command (-1 = display power on) via SendMessageW.
        2. Sets thread execution state: ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED.
        3. Generates subtle synthetic mouse movement to stimulate the Windows display driver.

        Returns:
            Dict containing success (bool) and descriptive message.
        """
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # 1. Broadcast monitor power on command
            user32.SendMessageW(
                self.HWND_BROADCAST,
                self.WM_SYSCOMMAND,
                self.SC_MONITORPOWER,
                -1,
            )

            # 2. Prevent sleep & enforce display power state
            kernel32.SetThreadExecutionState(
                self.ES_DISPLAY_REQUIRED | self.ES_SYSTEM_REQUIRED
            )

            # 3. Synthetic mouse wiggle
            user32.mouse_event(0x0001, 2, 2, 0, 0)
            time.sleep(0.03)
            user32.mouse_event(0x0001, -2, -2, 0, 0)

            return {
                "success": True,
                "message": "Physical display wake sequence triggered successfully.",
            }
        except Exception as err:
            return {
                "success": False,
                "message": f"Failed to wake display: {err}",
                "error": str(err),
            }

    def unlock_workstation(self, pin: Optional[str] = None) -> Dict[str, Any]:
        """Wake display, dismiss lock screen curtain, inject PIN, and verify unlock.

        Args:
            pin: Optional PIN string. If omitted, falls back to config.ADMIN_PIN.

        Returns:
            Dict containing success (bool), is_locked (bool), and message (str).
        """
        try:
            user32 = ctypes.windll.user32

            # 1. Wake physical display
            self.wake_display()

            # 2. Dismiss lock screen wallpaper curtain
            user32.keybd_event(self.VK_SPACE, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(self.VK_SPACE, 0, self.KEYEVENTF_KEYUP, 0)

            # 3. Allow lock screen slide-up animation to complete and focus PIN input
            time.sleep(0.45)

            # 4. Resolve PIN
            target_pin = pin
            if target_pin is None or str(target_pin).strip() == "":
                if config and hasattr(config, "ADMIN_PIN"):
                    target_pin = getattr(config, "ADMIN_PIN", "")
                else:
                    target_pin = os.getenv("ADMIN_PIN", "")

            # 5. Type PIN if provided
            if target_pin:
                clean_pin = str(target_pin).strip()
                for ch in clean_pin:
                    if ch.isdigit():
                        vk = 0x30 + int(ch)
                        user32.keybd_event(vk, 0, 0, 0)
                        time.sleep(0.025)
                        user32.keybd_event(vk, 0, self.KEYEVENTF_KEYUP, 0)
                        time.sleep(0.025)
                    else:
                        user32.keybd_event(0, ord(ch), self.KEYEVENTF_UNICODE, 0)
                        time.sleep(0.025)
                        user32.keybd_event(0, ord(ch), self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP, 0)
                        time.sleep(0.025)

                time.sleep(0.05)
                # Press Enter to submit credentials
                user32.keybd_event(self.VK_RETURN, 0, 0, 0)
                time.sleep(0.03)
                user32.keybd_event(self.VK_RETURN, 0, self.KEYEVENTF_KEYUP, 0)

            # 6. Wait for logon processing
            time.sleep(0.8)

            # 7. Verify actual lock state
            status = self.get_lock_status()
            is_locked = status.get("is_locked", False)
            success = not is_locked

            if success:
                message = "Workstation unlocked successfully."
            else:
                if not target_pin:
                    message = "Display woken and lock screen dismissed (no PIN configured/provided)."
                else:
                    message = "Unlock keystrokes injected; workstation remains locked."

            return {
                "success": success,
                "is_locked": is_locked,
                "message": message,
            }
        except Exception as err:
            return {
                "success": False,
                "is_locked": True,
                "message": f"Failed to unlock workstation: {err}",
                "error": str(err),
            }

    # =========================================================================
    # 3. Volume Control
    # =========================================================================

    def _send_vk(self, vk_code: int) -> None:
        """Send virtual key press and release event."""
        ctypes.windll.user32.keybd_event(vk_code, 0, self.KEYEVENTF_EXTENDEDKEY, 0)
        ctypes.windll.user32.keybd_event(vk_code, 0, self.KEYEVENTF_EXTENDEDKEY | self.KEYEVENTF_KEYUP, 0)

    def set_volume(self, level: int) -> Dict[str, Any]:
        """Set master volume percentage (0 - 100).

        Calibration approach:
        Windows adjusts volume in 2% steps.
        Sending 50 VK_VOLUME_DOWN presses reduces volume to 0%,
        followed by (level / 2) VK_VOLUME_UP presses to reach exact target.
        """
        level = max(0, min(100, int(level)))

        try:
            # Reset volume to 0
            for _ in range(50):
                self._send_vk(self.VK_VOLUME_DOWN)

            # Step up to target level
            steps_up = level // 2
            for _ in range(steps_up):
                self._send_vk(self.VK_VOLUME_UP)

            self._current_volume = level
            self._muted = False
            return {
                "success": True,
                "volume": level,
                "message": f"Volume set to {level}%.",
            }
        except Exception as err:
            return {"success": False, "error": f"Failed to set volume: {err}"}

    def mute_volume(self, mute: Optional[bool] = None) -> Dict[str, Any]:
        """Toggle or set mute state for master volume."""
        try:
            self._send_vk(self.VK_VOLUME_MUTE)
            if mute is not None:
                self._muted = mute
            else:
                self._muted = not self._muted

            return {
                "success": True,
                "muted": self._muted,
                "message": "Volume mute toggled.",
            }
        except Exception as err:
            return {"success": False, "error": f"Failed to toggle mute: {err}"}

    def get_volume(self) -> Dict[str, Any]:
        """Return the tracked volume and mute status."""
        return {
            "volume": self._current_volume,
            "muted": self._muted,
        }

    # =========================================================================
    # 4. Application Management
    # =========================================================================

    def _resolve_app_target(self, app_name: str) -> str:
        """Resolve friendly application name to executable or command."""
        key = app_name.strip().lower()
        return self.APP_ALIASES.get(key, app_name.strip())

    def open_application(self, app_name: str) -> Dict[str, Any]:
        """Launch an application by friendly name or executable path."""
        target = self._resolve_app_target(app_name)

        try:
            if target.startswith("ms-settings:"):
                os.startfile(target)  # type: ignore[attr-defined]
                return {
                    "success": True,
                    "app": app_name,
                    "target": target,
                    "message": f"Opened Windows URI '{target}'.",
                }

            # If executable is on PATH or absolute
            subprocess.Popen(
                target,
                shell=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0,
            )
            return {
                "success": True,
                "app": app_name,
                "target": target,
                "message": f"Application '{app_name}' launched successfully.",
            }
        except Exception as err:
            # Fallback to os.startfile
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                return {
                    "success": True,
                    "app": app_name,
                    "target": target,
                    "message": f"Application '{app_name}' launched via startfile.",
                }
            except Exception as inner_err:
                return {
                    "success": False,
                    "app": app_name,
                    "error": f"Failed to open '{app_name}': {inner_err}",
                }

    def close_application(self, app_name: str) -> Dict[str, Any]:
        """Gracefully terminate or kill processes matching app name using exact matching and protected process blacklist."""
        target = self._resolve_app_target(app_name).strip().lower()
        if not target.endswith(".exe") and not target.endswith(".cmd") and not target.endswith(".bat"):
            target_name = f"{target}.exe"
        else:
            target_name = os.path.basename(target)

        # Blacklist check: strictly forbid terminating critical system or interpreter processes
        if target_name in self.PROTECTED_PROCESSES or target in self.PROTECTED_PROCESSES:
            return {
                "success": False,
                "app": app_name,
                "target": target_name,
                "killed_count": 0,
                "error": f"Process '{target_name}' is a protected system process and cannot be terminated.",
            }

        killed_pids: List[int] = []
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    # Strict exact matching (prevents unintended prefix or substring termination)
                    if pname == target_name:
                        proc.terminate()
                        killed_pids.append(proc.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Wait briefly for termination
            if killed_pids:
                gone, alive = psutil.wait_procs(
                    [psutil.Process(pid) for pid in killed_pids if psutil.pid_exists(pid)],
                    timeout=2,
                )
                for p in alive:
                    try:
                        p.kill()
                    except Exception:
                        pass

                return {
                    "success": True,
                    "app": app_name,
                    "target": target_name,
                    "killed_count": len(killed_pids),
                    "message": f"Terminated {len(killed_pids)} process instance(s) of '{app_name}'.",
                }

            # If psutil process search didn't match, attempt taskkill with exact name
            tk = subprocess.run(
                ["taskkill", "/F", "/IM", target_name],
                capture_output=True,
                text=True,
            )
            if tk.returncode == 0:
                return {
                    "success": True,
                    "app": app_name,
                    "target": target_name,
                    "killed_count": 1,
                    "message": f"Closed '{app_name}' via taskkill.",
                }

            return {
                "success": False,
                "app": app_name,
                "target": target_name,
                "killed_count": 0,
                "message": f"No active process found for '{app_name}'.",
            }

        except Exception as err:
            return {
                "success": False,
                "app": app_name,
                "target": target_name,
                "killed_count": 0,
                "error": f"Error closing '{app_name}': {err}",
            }

    # =========================================================================
    # 5. Sleep / Shutdown Controls
    # =========================================================================

    def shutdown_pc(self, delay_seconds: int = 60, restart: bool = False) -> Dict[str, Any]:
        """Initiate scheduled shutdown or reboot."""
        delay = max(0, int(delay_seconds))
        flag = "/r" if restart else "/s"
        action = "Reboot" if restart else "Shutdown"

        try:
            cmd = ["shutdown.exe", flag, "/t", str(delay)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                return {
                    "success": True,
                    "action": action,
                    "delay_seconds": delay,
                    "message": f"{action} scheduled in {delay} seconds.",
                }
            return {
                "success": False,
                "error": res.stderr.strip() or "Shutdown command returned non-zero code.",
            }
        except Exception as err:
            return {"success": False, "error": f"Failed to execute shutdown: {err}"}

    def cancel_shutdown(self) -> Dict[str, Any]:
        """Cancel any scheduled shutdown or reboot."""
        try:
            cmd = ["shutdown.exe", "/a"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                return {"success": True, "message": "Scheduled shutdown cancelled successfully."}
            return {
                "success": False,
                "message": res.stderr.strip() or "No scheduled shutdown to cancel.",
            }
        except Exception as err:
            return {"success": False, "error": f"Failed to cancel shutdown: {err}"}

    def sleep_pc(self) -> Dict[str, Any]:
        """Put PC to sleep."""
        try:
            cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
            subprocess.run(cmd, check=False)
            return {"success": True, "message": "PC entering sleep state."}
        except Exception as err:
            return {"success": False, "error": f"Failed to enter sleep state: {err}"}

    # =========================================================================
    # 6. File & Directory Management (Safety Gated)
    # =========================================================================

    def delete_file(self, filepath: str) -> Dict[str, Any]:
        """Safely delete a single file with system path guards."""
        try:
            target = Path(filepath).resolve()
            if not target.exists():
                return {"success": False, "error": f"Fayl topilmadi: {filepath}"}
            if not target.is_file():
                return {"success": False, "error": f"Ko'rsatilgan yo'l fayl emas: {filepath}"}

            # Protected system extensions check
            protected_exts = {".sys", ".dll", ".drv", ".efi"}
            if target.suffix.lower() in protected_exts:
                return {
                    "success": False,
                    "error": f"Tizim fayllarini ({target.suffix}) o'chirish taqiqlangan: '{target.name}'.",
                }

            target.unlink()
            return {
                "success": True,
                "path": str(target),
                "message": f"'{target.name}' fayli muvaffaqiyatli o'chirildi.",
            }
        except Exception as err:
            return {"success": False, "error": f"Faylni o'chirishda xatolik: {err}"}

    def delete_folder(self, folderpath: str) -> Dict[str, Any]:
        """Safely delete a directory recursively with critical path protection."""
        try:
            target = Path(folderpath).resolve()
            if not target.exists():
                return {"success": False, "error": f"Papka topilmadi: {folderpath}"}
            if not target.is_dir():
                return {"success": False, "error": f"Ko'rsatilgan yo'l papka emas: {folderpath}"}

            # Blacklist critical Windows roots
            normalized = str(target).lower().rstrip("\\/")
            forbidden = {
                "c:", "c:\\windows", "c:\\program files",
                "c:\\program files (x86)", "c:\\users",
                "c:\\users\\user", "c:\\users\\user\\desktop"
            }
            if normalized in forbidden or len(normalized) <= 3:
                return {
                    "success": False,
                    "error": f"Tizim yoki asosiy profil papkasini o'chirish qat'iyan taqiqlangan: '{target}'.",
                }

            shutil.rmtree(target)
            return {
                "success": True,
                "path": str(target),
                "message": f"'{target.name}' papkasi muvaffaqiyatli o'chirildi.",
            }
        except Exception as err:
            return {"success": False, "error": f"Papkani o'chirishda xatolik: {err}"}

    def is_destructive_command(self, command: str) -> bool:
        """Inspect command string for potentially destructive administrative patterns."""
        if not command:
            return False
        patterns = [
            r'\brmdir\b', r'\bdel\b', r'\bremove-item\b', r'\bformat\b',
            r'\bstop-process\b', r'\btaskkill\b', r'\bshutdown\b',
            r'\bclear-disk\b', r'\bdiskpart\b', r'\bdrop\b', r'\bwipe\b'
        ]
        cmd_lower = command.lower()
        return any(re.search(pat, cmd_lower) for pat in patterns)

    # =========================================================================
    # 7. Administrative / PowerShell Execution
    # =========================================================================

    def execute_command(
        self,
        command: str,
        timeout: int = 30,
        powershell: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Safely execute administrative or PowerShell commands with timeouts and error capture.

        Args:
            command: The command line string to execute.
            timeout: Maximum execution time in seconds.
            powershell: If True, executes via PowerShell -NoProfile. Otherwise via cmd.exe.
            force: If True, bypasses destructive command safety guard.

        Returns:
            Dict containing exit_code, stdout, stderr, execution_time, and success flag.
        """
        if not command or not command.strip():
            return {"success": False, "error": "Empty command provided."}

        # Safety gate: block destructive commands if not explicitly forced
        if not force and self.is_destructive_command(command):
            return {
                "success": False,
                "gated": True,
                "error": "Xavfsizlik qoidasi: Destruktiv tizim buyrug'i aniqlandi. Tasdiqlash talab etiladi (force=True).",
            }

        start_time = time.time()
        try:
            if powershell:
                cmd = [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ]
            else:
                cmd = ["cmd.exe", "/c", command]

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = round(time.time() - start_time, 2)

            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "duration_seconds": elapsed,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Command timed out after {timeout} seconds.",
                "duration_seconds": timeout,
            }
        except Exception as err:
            return {
                "success": False,
                "error": f"Execution error: {err}",
                "duration_seconds": round(time.time() - start_time, 2),
            }

    # =========================================================================
    # 8. Native Text-to-Speech (TTS)
    # =========================================================================

    def speak(self, text: str, async_mode: bool = True) -> Dict[str, Any]:
        """Speak the given text on PC speakers using native Windows SAPI.

        Args:
            text: Text to speak.
            async_mode: If True, speaks asynchronously without blocking the caller.
        """
        if not text or not text.strip():
            return {"success": False, "error": "Empty text provided."}

        # 1. Try win32com SAPI.SpVoice
        if self._tts_voice is not None:
            try:
                # SVSFlagsAsync = 1, SVSFDefault = 0
                flags = 1 if async_mode else 0
                self._tts_voice.Speak(text, flags)
                return {"success": True, "engine": "win32com.SAPI.SpVoice", "async": async_mode}
            except Exception:
                pass

        # 2. Fallback via PowerShell SAPI
        try:
            clean_text = text.replace("'", "''")
            ps_cmd = f"(New-Object -ComObject SAPI.SpVoice).Speak('{clean_text}')"
            if async_mode:
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
            else:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                    timeout=15,
                    check=False,
                )
            return {"success": True, "engine": "powershell.SAPI.SpVoice", "async": async_mode}
        except Exception as err:
            return {"success": False, "error": f"TTS synthesis failed: {err}"}
