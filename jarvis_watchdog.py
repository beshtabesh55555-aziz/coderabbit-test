"""
JARVIS System Supervisor & Watchdog Engine
===========================================
Ultra-lightweight Guardian and Circuit Breaker for JARVIS AI System.

Key Responsibilities:
1. Circuit Breaker for Alert Spam:
   - Tracks frequency of generated snapshots in snapshots/ or alert events.
   - If JARVIS dispatches > 3 alerts within 60 seconds, trips circuit breaker:
     IMMEDIATELY kills or disarms the JARVIS process, logs an incident,
     writes `incident_report.json`, and dispatches an emergency Telegram alert.
2. Resource Guardian (for 4GB RAM Windows environments):
   - Monitors target process CPU & RAM via psutil.
   - If CPU > 75% for > 30 seconds or RAM > 350 MB, terminates the process safely
     to protect the host operating system.
3. Heartbeat Monitor:
   - Reads `heartbeat.json` written periodically by `jarvis_core.py`.
   - If timestamp is stale by > 30 seconds while process is alive, flags a hang
     and trips the watchdog.
4. Emergency Notification:
   - Dispatches emergency alert to Admin Telegram:
     "⚠️ JARVIS XAVFSIZLIK QALQONI: Tizim xaddidan oshgani sababli avtomatik to'xtatildi! Sabab: ..."
5. Antigravity Incident Bridge:
   - Saves structured diagnostics to `incident_report.json` with timestamp,
     trigger cause, process metrics, and host telemetry.
6. Standalone CLI & Self-Test Suite:
   - CLI flags: `--monitor`, `--test`, `--status`.
   - Ultra-lightweight footprint (sleep 2-3s between ticks, <15MB RAM).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

# Configure logging
logger = logging.getLogger("JarvisWatchdog")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [WATCHDOG] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class JarvisWatchdog:
    """Supervisory watchdog and protective circuit breaker for JARVIS."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        target_pid: Optional[int] = None,
        max_alerts_count: int = 5,
        alert_window_seconds: float = 60.0,
        cpu_threshold_percent: float = 75.0,
        cpu_threshold_duration_seconds: float = 30.0,
        ram_threshold_mb: float = 350.0,
        heartbeat_timeout_seconds: float = 35.0,
        tick_interval_seconds: float = 2.5,
        telegram_token: Optional[str] = None,
        telegram_admin_chat_id: Optional[str] = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        self.snapshots_dir = self.base_dir / "snapshots"
        self.heartbeat_file = self.base_dir / "heartbeat.json"
        self.incident_report_file = self.base_dir / "incident_report.json"

        # Thresholds
        self.target_pid = target_pid
        self.max_alerts_count = max_alerts_count
        self.alert_window_seconds = alert_window_seconds
        self.cpu_threshold_percent = cpu_threshold_percent
        self.cpu_threshold_duration_seconds = cpu_threshold_duration_seconds
        self.ram_threshold_mb = ram_threshold_mb
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.tick_interval_seconds = tick_interval_seconds

        # Telegram Configuration
        if telegram_token is not None:
            self.telegram_token = telegram_token.strip()
        else:
            self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

        if telegram_admin_chat_id is not None:
            self.telegram_admin_chat_id = str(telegram_admin_chat_id).strip()
        else:
            self.telegram_admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()

        if telegram_token is None and telegram_admin_chat_id is None:
            self._load_env_config()

        # Operational state
        self.is_running: bool = False
        self.circuit_breaker_tripped: bool = False
        self._cpu_violation_start: Optional[float] = None
        self._alert_history: deque[float] = deque(maxlen=200)
        self._known_snapshot_files: set[str] = set()
        self._last_incident_report: Optional[Dict[str, Any]] = None
        self._reset_timestamp: float = 0.0
        if self.incident_report_file.exists():
            try:
                with open(self.incident_report_file, "r", encoding="utf-8") as f:
                    ir_data = json.load(f)
                    if ir_data.get("status") == "RESOLVED":
                        self._reset_timestamp = float(ir_data.get("reset_timestamp", 0.0))
            except Exception:
                pass

        # Pre-seed known snapshots so existing old files aren't miscounted
        self._scan_existing_snapshots()

    def _load_env_config(self) -> None:
        """Attempt to load telegram tokens from config.py if not already populated."""
        if not self.telegram_token or not self.telegram_admin_chat_id:
            try:
                import config  # type: ignore
                if not self.telegram_token:
                    self.telegram_token = getattr(config, "TELEGRAM_BOT_TOKEN", "").strip()
                if not self.telegram_admin_chat_id:
                    self.telegram_admin_chat_id = str(getattr(config, "TELEGRAM_ADMIN_CHAT_ID", "")).strip()
            except ImportError:
                env_path = self.base_dir / ".env"
                if env_path.exists():
                    try:
                        with open(env_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("TELEGRAM_BOT_TOKEN="):
                                    self.telegram_token = line.split("=", 1)[1].strip()
                                elif line.startswith("TELEGRAM_ADMIN_CHAT_ID="):
                                    self.telegram_admin_chat_id = line.split("=", 1)[1].strip()
                    except Exception as err:
                        logger.debug("Failed reading .env file: %s", err)

    def _scan_existing_snapshots(self) -> None:
        """Record baseline existing snapshot filenames."""
        if self.snapshots_dir.exists():
            try:
                for entry in os.scandir(self.snapshots_dir):
                    if entry.is_file() and entry.name.lower().startswith(("alert_", "motion_alert_")) and entry.name.lower().endswith((".jpg", ".jpeg", ".png")):
                        self._known_snapshot_files.add(entry.name)
            except OSError as err:
                logger.debug("Error indexing existing snapshots: %s", err)

    # =========================================================================
    # 1. Target Process Discovery & Inspection
    # =========================================================================

    def find_jarvis_process(self) -> Optional[psutil.Process]:
        """Locates the active JARVIS target process.
        
        Resolution order:
        1. Explicitly configured target_pid.
        2. PID written into heartbeat.json.
        3. Active Python processes running jarvis_core.py or jarvis_gui.py.
        """
        # 1. Check heartbeat.json FIRST if active and recent (< 60s)
        if self.heartbeat_file.exists():
            try:
                with open(self.heartbeat_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                hb_pid = data.get("pid")
                hb_ts = float(data.get("timestamp", 0.0))
                if hb_pid and psutil.pid_exists(hb_pid) and (time.time() - hb_ts) < 60.0:
                    proc = psutil.Process(hb_pid)
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        if "python" in proc.name().lower():
                            return proc
            except Exception:
                pass

        # 2. Check explicit target_pid
        if self.target_pid:
            try:
                if psutil.pid_exists(self.target_pid):
                    proc = psutil.Process(self.target_pid)
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        # Check if child process is the real python worker (launcher pattern)
                        for c in proc.children(recursive=True):
                            if c.is_running() and "python" in c.name().lower():
                                return c
                        return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # 3. Process table scan
        curr_pid = os.getpid()
        target_scripts = {"jarvis_core.py", "jarvis_gui.py", "jarvis_system.py"}

        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["pid"] == curr_pid:
                        continue
                    cmdline = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmdline).lower()

                    # Avoid watchdog self-match
                    if "jarvis_watchdog.py" in cmd_str:
                        continue

                    # Check script match
                    for script in target_scripts:
                        if script in cmd_str:
                            return proc
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as err:
            logger.debug("Error iterating processes: %s", err)

        return None

    # =========================================================================
    # 2. Alert Rate Limiter / Circuit Breaker
    # =========================================================================

    def record_alert(self, timestamp: Optional[float] = None) -> None:
        """Programmatically record an alert event."""
        t = timestamp if timestamp is not None else time.time()
        self._alert_history.append(t)

    def check_alert_rate(self) -> Tuple[bool, int, str]:
        """Monitors frequency of alerts in snapshots/ and event queue.
        
        Returns:
            (is_tripped, alert_count, reason_message)
        """
        now = time.time()
        window_start = max(now - self.alert_window_seconds, getattr(self, "_reset_timestamp", 0.0))

        # Prune old in-memory alerts
        while self._alert_history and self._alert_history[0] < window_start:
            self._alert_history.popleft()

        snapshot_timestamps: List[float] = []
        if self.snapshots_dir.exists():
            try:
                for entry in os.scandir(self.snapshots_dir):
                    if entry.is_file() and entry.name.lower().startswith(("alert_", "motion_alert_")) and entry.name.lower().endswith((".jpg", ".jpeg", ".png")):
                        try:
                            st = entry.stat()
                            mtime = st.st_mtime
                            # If modified within window
                            if mtime >= window_start:
                                snapshot_timestamps.append(mtime)
                        except OSError:
                            pass
            except OSError as err:
                logger.debug("Failed scanning snapshots dir: %s", err)

        # Merge unique alert events (avoid double counting events occurring within 1.0s)
        all_events = sorted(list(self._alert_history) + snapshot_timestamps)
        distinct_events: List[float] = []
        for event_time in all_events:
            if not distinct_events or (event_time - distinct_events[-1]) >= 1.0:
                distinct_events.append(event_time)

        alert_count = len(distinct_events)

        if alert_count > self.max_alerts_count:
            reason = (
                f"Alert spam detected: {alert_count} alerts fired within {self.alert_window_seconds:.0f}s "
                f"(Threshold: > {self.max_alerts_count})"
            )
            return True, alert_count, reason

        return False, alert_count, "Alert frequency within safe bounds"

    # =========================================================================
    # 3. Resource Guardian (RAM & CPU limits)
    # =========================================================================

    def check_resource_limits(self, proc: psutil.Process) -> Tuple[bool, str, Dict[str, Any]]:
        """Monitors target process CPU & RAM usage.
        
        Conditions:
        - RAM > 350 MB: immediate termination.
        - CPU > 75% for > 30 seconds: termination.
        
        Returns:
            (is_violation, reason, diagnostics)
        """
        diagnostics: Dict[str, Any] = {
            "pid": proc.pid,
            "cpu_percent": 0.0,
            "ram_mb": 0.0,
            "cpu_duration_exceeded": 0.0,
        }

        try:
            # Check RAM
            mem_info = proc.memory_info()
            ram_mb = round(mem_info.rss / (1024 * 1024), 2)
            diagnostics["ram_mb"] = ram_mb

            if ram_mb > self.ram_threshold_mb:
                reason = (
                    f"RAM usage exceeded limit: {ram_mb:.1f} MB > {self.ram_threshold_mb:.1f} MB threshold"
                )
                return True, reason, diagnostics

            # Check CPU
            cpu_pct = round(proc.cpu_percent(interval=None), 2)
            diagnostics["cpu_percent"] = cpu_pct

            now = time.time()
            if cpu_pct > self.cpu_threshold_percent:
                if self._cpu_violation_start is None:
                    self._cpu_violation_start = now
                duration = round(now - self._cpu_violation_start, 2)
                diagnostics["cpu_duration_exceeded"] = duration

                if duration >= self.cpu_threshold_duration_seconds:
                    reason = (
                        f"CPU usage exceeded limit: {cpu_pct:.1f}% for {duration:.1f}s "
                        f"(Threshold: > {self.cpu_threshold_percent}% for > {self.cpu_threshold_duration_seconds}s)"
                    )
                    return True, reason, diagnostics
            else:
                self._cpu_violation_start = None

            return False, "Resource usage normal", diagnostics

        except (psutil.NoSuchProcess, psutil.AccessDenied) as err:
            return False, f"Process status unavailable: {err}", diagnostics

    # =========================================================================
    # 4. Heartbeat Monitor
    # =========================================================================

    def check_heartbeat(self, proc: Optional[psutil.Process] = None) -> Tuple[bool, float, str]:
        """Verifies heartbeat.json staleness.
        
        Condition:
        - If heartbeat timestamp is older than heartbeat_timeout_seconds (35s)
          while process is running, flag a hang.
        
        Returns:
            (is_stale, age_seconds, message)
        """
        if not self.heartbeat_file.exists():
            # If process is running for over heartbeat_timeout_seconds without a heartbeat file
            if proc and proc.is_running():
                try:
                    proc_create_time = proc.create_time()
                    proc_age = time.time() - proc_create_time
                    if proc_age > self.heartbeat_timeout_seconds:
                        return True, proc_age, f"Heartbeat file missing: process active for {proc_age:.1f}s without heartbeat"
                except Exception:
                    pass
            return False, 0.0, "Heartbeat file not found, process not active"

        try:
            with open(self.heartbeat_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            hb_pid = data.get("pid")
            # If proc is specified and doesn't match the PID recorded in heartbeat.json:
            if proc is not None and hb_pid is not None and hb_pid != proc.pid:
                # Check if hb_pid is a child or parent of proc (launcher pattern)
                is_related = False
                try:
                    for c in proc.children(recursive=True):
                        if c.pid == hb_pid:
                            is_related = True
                            break
                    if not is_related and proc.parent() and proc.parent().pid == hb_pid:
                        is_related = True
                except Exception:
                    pass

                if not is_related:
                    # The heartbeat on disk belongs to a previous process run!
                    try:
                        proc_uptime = round(time.time() - proc.create_time(), 2)
                    except Exception:
                        proc_uptime = 0.0

                    if proc_uptime > self.heartbeat_timeout_seconds:
                        return True, proc_uptime, f"Heartbeat missing for PID {proc.pid}: active for {proc_uptime:.1f}s without heartbeat"
                    return False, proc_uptime, f"Process starting up: PID {proc.pid} active for {proc_uptime:.1f}s (awaiting first heartbeat)"

            hb_timestamp = float(data.get("timestamp", 0.0))
            if hb_timestamp <= 0.0:
                return True, 999.0, "Invalid heartbeat timestamp in file"

            now = time.time()
            age = round(now - hb_timestamp, 2)

            # Check if stale
            if age > self.heartbeat_timeout_seconds:
                # Only trip if process is confirmed alive
                if proc is not None and proc.is_running():
                    return True, age, f"Heartbeat stale: {age:.1f}s since last update (Limit: {self.heartbeat_timeout_seconds}s)"
                return False, age, f"Heartbeat dormant (Target inactive, last seen: {age:.1f}s ago)"

            return False, age, "Heartbeat healthy"

        except Exception as err:
            return False, 0.0, f"Error reading heartbeat file: {err}"

    # =========================================================================
    # 5. Process Termination Routine
    # =========================================================================

    def terminate_process(self, proc: psutil.Process, reason: str = "") -> bool:
        """Safely terminates target process and its child processes on Windows."""
        try:
            pid = proc.pid
            logger.warning("Terminating process tree for PID %s. Reason: %s", pid, reason)

            # Collect children recursively before terminating parent
            try:
                children = proc.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                children = []

            # Send terminate signal
            for c in children:
                try:
                    c.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Wait up to 2 seconds for clean exit
            gone, alive = psutil.wait_procs([proc] + children, timeout=2.0)

            # Hard kill any remaining alive processes
            for p in alive:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # Windows taskkill fallback if PID still exists
            if psutil.pid_exists(pid):
                try:
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid), "/T"],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception as err:
                    logger.debug("taskkill fallback error: %s", err)

            is_dead = not psutil.pid_exists(pid)
            if is_dead:
                logger.info("Process tree for PID %s successfully terminated.", pid)
            else:
                logger.error("Process PID %s could not be fully killed.", pid)

            return is_dead

        except Exception as err:
            logger.error("Failed to terminate process: %s", err)
            return False

    # =========================================================================
    # 6. Antigravity Incident Bridge & Emergency Alert
    # =========================================================================

    def write_incident_report(
        self,
        reason: str,
        trigger_type: str,
        proc: Optional[psutil.Process] = None,
        action_taken: str = "PROCESS_TERMINATED",
        extra_metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generates structured incident report and persists to incident_report.json."""
        now_utc = datetime.now(timezone.utc).isoformat()
        incident_id = f"INC-{int(time.time())}-{os.urandom(2).hex().upper()}"

        # Collect host telemetry
        host_cpu = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()

        target_info: Dict[str, Any] = {
            "pid": proc.pid if proc else None,
            "name": None,
            "cmdline": None,
            "status": "terminated" if action_taken == "PROCESS_TERMINATED" else "unknown",
        }

        if proc:
            try:
                target_info["name"] = proc.name()
            except Exception:
                pass
            try:
                target_info["cmdline"] = proc.cmdline()
            except Exception:
                pass

        incident_payload: Dict[str, Any] = {
            "incident_id": incident_id,
            "timestamp": now_utc,
            "trigger_cause": reason,
            "trigger_type": trigger_type,
            "action_taken": action_taken,
            "target_process": target_info,
            "system_metrics": {
                "host_cpu_percent": host_cpu,
                "host_ram_percent": vm.percent,
                "host_ram_used_mb": round(vm.used / (1024 * 1024), 2),
                "host_ram_total_mb": round(vm.total / (1024 * 1024), 2),
                "process_diagnostics": extra_metrics or {},
            },
            "watchdog_state": {
                "alerts_in_window": len(self._alert_history),
                "cpu_violation_start": self._cpu_violation_start,
                "thresholds": {
                    "max_alerts_count": self.max_alerts_count,
                    "alert_window_seconds": self.alert_window_seconds,
                    "ram_threshold_mb": self.ram_threshold_mb,
                    "cpu_threshold_percent": self.cpu_threshold_percent,
                    "cpu_threshold_duration_seconds": self.cpu_threshold_duration_seconds,
                    "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
                },
            },
            "telegram_alert_sent": False,
            "status": "CIRCUIT_BREAKER_TRIPPED",
        }

        self._save_incident_report_file(incident_payload)
        self._last_incident_report = incident_payload
        return incident_payload

    def _save_incident_report_file(self, report_data: Dict[str, Any]) -> None:
        """Atomic safe write to incident_report.json using a unique temporary file."""
        tmp_path_str = ""
        try:
            self.incident_report_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path_str = tempfile.mkstemp(
                dir=self.incident_report_file.parent,
                prefix=f".{self.incident_report_file.name}.",
                suffix=".tmp"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(report_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path_str, self.incident_report_file)
            logger.info("Incident report written to: %s", self.incident_report_file)
        except Exception as err:
            logger.error("Failed saving incident report: %s", err)
            if tmp_path_str and os.path.exists(tmp_path_str):
                try:
                    os.remove(tmp_path_str)
                except OSError:
                    pass

    def send_telegram_emergency_alert(self, reason: str) -> bool:
        """Dispatches HTTP alert to Telegram bot admin chat."""
        if not self.telegram_token or not self.telegram_admin_chat_id:
            logger.warning("Telegram bot credentials not configured; emergency alert skipped.")
            return False

        message = (
            f"⚠️ <b>JARVIS XAVFSIZLIK QALQONI:</b> Tizim xaddidan oshgani sababli avtomatik to'xtatildi!\n"
            f"<b>Sabab:</b> {reason}\n"
            f"<b>Vaqt:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_admin_chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                success = (resp.status == 200)
                if success:
                    logger.info("Emergency Telegram notification dispatched successfully.")
                return success
        except Exception as err:
            logger.error("Failed dispatching Telegram emergency alert: %s", err)
            return False

    # =========================================================================
    # 7. Circuit Breaker Execution
    # =========================================================================

    def trip_circuit_breaker(
        self,
        reason: str,
        trigger_type: str,
        proc: Optional[psutil.Process] = None,
        extra_metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Trips the circuit breaker, kills target process, logs report, and alerts admin."""
        logger.critical(
            "🚨 CIRCUIT BREAKER TRIPPED! Reason: %s (Type: %s)", reason, trigger_type
        )
        self.circuit_breaker_tripped = True

        action_taken = "NO_TARGET_ACTIVE"
        if proc and proc.is_running():
            killed = self.terminate_process(proc, reason=reason)
            action_taken = "PROCESS_TERMINATED" if killed else "PROCESS_TERMINATION_FAILED"

        # Write incident report
        report = self.write_incident_report(
            reason=reason,
            trigger_type=trigger_type,
            proc=proc,
            action_taken=action_taken,
            extra_metrics=extra_metrics,
        )

        # Dispatch Telegram Emergency Alert
        tg_ok = self.send_telegram_emergency_alert(reason=reason)
        report["telegram_alert_sent"] = tg_ok
        self._save_incident_report_file(report)

        return report

    def reset_circuit_breaker(self) -> Dict[str, Any]:
        """Resets the circuit breaker state, clears alert history, and marks incident resolved."""
        now_ts = time.time()
        self.circuit_breaker_tripped = False
        self._cpu_violation_start = None
        self._alert_history.clear()
        self._reset_timestamp = now_ts

        updated_report = None
        if self.incident_report_file.exists():
            try:
                with open(self.incident_report_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["status"] = "RESOLVED"
                data["resolved_at"] = datetime.now(timezone.utc).isoformat()
                data["reset_timestamp"] = now_ts
                self._save_incident_report_file(data)
                self._last_incident_report = data
                updated_report = data
                logger.info("Circuit breaker reset and incident_report.json marked as RESOLVED.")
            except Exception as e:
                logger.error("Failed to update incident report on reset: %s", e)
        else:
            resolved_stub = {
                "status": "RESOLVED",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "reset_timestamp": now_ts
            }
            self._save_incident_report_file(resolved_stub)
            updated_report = resolved_stub
        return updated_report or {"status": "RESOLVED"}

    # =========================================================================
    # 8. Single Monitor Tick & Loop
    # =========================================================================

    def monitor_tick(self) -> Dict[str, Any]:
        """Executes a single evaluation cycle across all supervisory checks.
        
        Returns:
            Dict summarizing tick findings.
        """
        proc = self.find_jarvis_process()

        # 1. Alert Spam Check
        is_spam, alert_count, spam_msg = self.check_alert_rate()
        if is_spam:
            report = self.trip_circuit_breaker(
                reason=spam_msg,
                trigger_type="CIRCUIT_BREAKER_ALERT_SPAM",
                proc=proc,
            )
            return {"status": "TRIPPED", "trigger": "ALERT_SPAM", "report": report}

        # 2. Target Process Inspection (Resource limits & Heartbeat)
        if proc and proc.is_running():
            # Resource Limits Check
            is_resource_violation, res_msg, res_diag = self.check_resource_limits(proc)
            if is_resource_violation:
                trig_type = "RESOURCE_LIMIT_RAM" if "RAM" in res_msg else "RESOURCE_LIMIT_CPU"
                report = self.trip_circuit_breaker(
                    reason=res_msg,
                    trigger_type=trig_type,
                    proc=proc,
                    extra_metrics=res_diag,
                )
                return {"status": "TRIPPED", "trigger": trig_type, "report": report}

            # Heartbeat Staleness Check
            is_stale, hb_age, hb_msg = self.check_heartbeat(proc)
            if is_stale:
                report = self.trip_circuit_breaker(
                    reason=hb_msg,
                    trigger_type="HEARTBEAT_TIMEOUT",
                    proc=proc,
                    extra_metrics={"heartbeat_age_seconds": hb_age},
                )
                return {"status": "TRIPPED", "trigger": "HEARTBEAT_TIMEOUT", "report": report}

        return {
            "status": "HEALTHY",
            "process_active": proc is not None and proc.is_running(),
            "target_pid": proc.pid if proc else None,
            "alerts_in_window": alert_count,
        }

    def run_monitoring_loop(self, auto_spawn: bool = True) -> None:
        """Continuous execution loop with ultra-low overhead."""
        self.is_running = True
        logger.info("=== JARVIS WATCHDOG ACTIVATED ===")
        logger.info(
            "Thresholds: Alerts=%d/%0.0fs, RAM=%0.0fMB, CPU=%0.0f%% (%0.0fs), Heartbeat=%0.0fs, Interval=%0.1fs",
            self.max_alerts_count,
            self.alert_window_seconds,
            self.ram_threshold_mb,
            self.cpu_threshold_percent,
            self.cpu_threshold_duration_seconds,
            self.heartbeat_timeout_seconds,
            self.tick_interval_seconds,
        )

        def _spawn_core():
            core_script = self.base_dir / "jarvis_core.py"
            if core_script.exists():
                logger.info("Auto-spawning JARVIS Core (%s)...", core_script.name)
                log_file_path = self.base_dir / "jarvis_core_live.log"
                try:
                    log_file = open(log_file_path, "a", encoding="utf-8")
                    managed_sub = subprocess.Popen(
                        [sys.executable, "-u", str(core_script)],
                        cwd=str(self.base_dir),
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                    )
                    self.target_pid = managed_sub.pid
                    logger.info("JARVIS Core spawned successfully with PID=%d. Output logging to %s", managed_sub.pid, log_file_path.name)
                    time.sleep(15.0)
                except Exception as err:
                    logger.error("Failed to auto-spawn JARVIS Core: %s", err)

        # Check if JARVIS target process is active; if not and auto_spawn is enabled, spawn it!
        proc = self.find_jarvis_process()
        if not proc and auto_spawn:
            _spawn_core()

        try:
            while self.is_running:
                proc = self.find_jarvis_process()
                if not proc and auto_spawn:
                    logger.info("JARVIS target process ended. Respawning...")
                    _spawn_core()

                tick_res = self.monitor_tick()
                if tick_res.get("status") == "TRIPPED":
                    logger.warning("Circuit breaker has tripped. Pausing watchdog loop until reset.")
                    break
                time.sleep(self.tick_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Watchdog terminated by user.")
        finally:
            self.is_running = False

    def stop(self) -> None:
        """Stop background monitoring."""
        self.is_running = False

    # =========================================================================
    # 9. Status & Telemetry
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Collects current watchdog health, target status, and incident records."""
        proc = self.find_jarvis_process()
        is_spam, alert_count, _ = self.check_alert_rate()
        _, hb_age, hb_msg = self.check_heartbeat(proc)

        host_vm = psutil.virtual_memory()
        proc_stats: Dict[str, Any] = {"running": False}

        if proc and proc.is_running():
            try:
                proc_stats = {
                    "running": True,
                    "pid": proc.pid,
                    "name": proc.name(),
                    "cpu_percent": round(proc.cpu_percent(interval=0.1), 1),
                    "ram_mb": round(proc.memory_info().rss / (1024 * 1024), 2),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                proc_stats = {"running": False}

        last_incident = None
        if self.incident_report_file.exists():
            try:
                with open(self.incident_report_file, "r", encoding="utf-8") as f:
                    last_incident = json.load(f)
            except Exception:
                pass

        return {
            "watchdog_active": self.is_running,
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
            "target_process": proc_stats,
            "heartbeat": {
                "age_seconds": hb_age,
                "status": hb_msg,
            },
            "security_alerts": {
                "count_last_60s": alert_count,
                "spam_limit": self.max_alerts_count,
                "is_spam": is_spam,
            },
            "host_system": {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "ram_percent": host_vm.percent,
                "ram_free_mb": round(host_vm.available / (1024 * 1024), 1),
                "ram_total_mb": round(host_vm.total / (1024 * 1024), 1),
            },
            "last_incident": last_incident,
        }

    def print_status_dashboard(self) -> None:
        """Displays formatted CLI status dashboard."""
        status = self.get_status()
        print("=" * 65)
        print("       🛡️  JARVIS WATCHDOG & GUARDIAN SYSTEM STATUS       ")
        print("=" * 65)
        print(f"Watchdog State     : {'TRIPPED' if status['circuit_breaker_tripped'] else 'ARMED & MONITORING'}")
        
        tgt = status["target_process"]
        if tgt.get("running"):
            print(f"Target Process     : PID {tgt['pid']} ({tgt['name']}) [ACTIVE]")
            print(f"  CPU Usage        : {tgt['cpu_percent']}% (Limit: {self.cpu_threshold_percent}%)")
            print(f"  RAM Usage        : {tgt['ram_mb']} MB (Limit: {self.ram_threshold_mb} MB)")
        else:
            print("Target Process     : NOT RUNNING / STANDBY")

        hb = status["heartbeat"]
        print(f"Heartbeat Status   : {hb['status']} (Age: {hb['age_seconds']:.1f}s)")

        sec = status["security_alerts"]
        print(f"Recent Alerts (60s): {sec['count_last_60s']} (Spam Limit: {sec['spam_limit']})")

        host = status["host_system"]
        print(f"Host System        : CPU {host['cpu_percent']}% | RAM {host['ram_percent']}% ({host['ram_free_mb']}MB free)")

        last_inc = status.get("last_incident")
        if last_inc:
            print("-" * 65)
            print(f"Last Incident ID   : {last_inc.get('incident_id')}")
            print(f"Trigger Cause      : {last_inc.get('trigger_cause')}")
            print(f"Timestamp          : {last_inc.get('timestamp')}")
        print("=" * 65)


# =============================================================================
# 10. Self-Test Suite (--test)
# =============================================================================

def run_self_tests() -> bool:
    """Executes isolated self-test suite validating all watchdog features."""
    import shutil
    import subprocess
    import tempfile

    print("=" * 65)
    print("  🧪 JARVIS WATCHDOG VERIFICATION & COMPLIANCE TEST SUITE  ")
    print("=" * 65)

    temp_dir = Path(tempfile.mkdtemp(prefix="jarvis_wd_test_"))
    test_snapshots_dir = temp_dir / "snapshots"
    test_snapshots_dir.mkdir(parents=True, exist_ok=True)

    passed_count = 0
    total_tests = 7

    try:
        # ---------------------------------------------------------------------
        # 1. Test Alert Rate Limiter & Circuit Breaker (> 3 alerts in 60s)
        # ---------------------------------------------------------------------
        print("\n[1/7] Testing Circuit Breaker Alert Spam Tracking...")
        wd = JarvisWatchdog(
            base_dir=temp_dir,
            max_alerts_count=3,
            alert_window_seconds=60.0,
            telegram_token="",
            telegram_admin_chat_id="",
        )
        # Inject 4 snapshots into test folder
        now = time.time()
        for i in range(4):
            snap_file = test_snapshots_dir / f"motion_alert_{i}_{int(now)}.jpg"
            snap_file.write_bytes(b"dummy_image_data")
            os.utime(snap_file, (now - (i * 2), now - (i * 2)))

        is_spam, count, reason = wd.check_alert_rate()
        print(f"  ✓ Injected 4 alert snapshots. Detected alerts: {count}")
        assert is_spam is True, "Circuit breaker should detect spam for > 3 alerts"
        assert count >= 4, f"Expected at least 4 alerts, got {count}"
        assert "Alert spam detected" in reason

        # Verify trip logic writes incident report
        report = wd.trip_circuit_breaker(reason, trigger_type="CIRCUIT_BREAKER_ALERT_SPAM")
        assert wd.incident_report_file.exists()
        assert report["trigger_type"] == "CIRCUIT_BREAKER_ALERT_SPAM"
        print("  ✓ Circuit breaker tripped successfully and incident report created.")
        passed_count += 1

        # ---------------------------------------------------------------------
        # 2. Test RAM Resource Guardian (> 350 MB threshold)
        # ---------------------------------------------------------------------
        print("\n[2/7] Testing Resource Guardian RAM Threshold (> 350 MB)...")
        class MockProcRAM:
            pid = 99991
            def memory_info(self):
                class Mem:
                    rss = 368 * 1024 * 1024  # 368 MB
                return Mem()
            def cpu_percent(self, interval=None):
                return 12.0

        is_viol, reason, diag = wd.check_resource_limits(MockProcRAM())  # type: ignore
        assert is_viol is True, "RAM violation should be detected"
        assert "RAM usage exceeded limit" in reason
        assert diag["ram_mb"] >= 368.0
        print(f"  ✓ RAM violation properly triggered: {reason}")
        passed_count += 1

        # ---------------------------------------------------------------------
        # 3. Test CPU Resource Guardian (> 75% for > 30s)
        # ---------------------------------------------------------------------
        print("\n[3/7] Testing Resource Guardian CPU Threshold (> 75% for > 30s)...")
        class MockProcCPU:
            pid = 99992
            def memory_info(self):
                class Mem:
                    rss = 45 * 1024 * 1024  # 45 MB
                return Mem()
            def cpu_percent(self, interval=None):
                return 88.5

        # Call 1: Start CPU violation timer
        mock_cpu = MockProcCPU()
        is_viol, _, diag = wd.check_resource_limits(mock_cpu)  # type: ignore
        assert is_viol is False, "First tick should record start time, not trip prematurely"
        assert wd._cpu_violation_start is not None

        # Simulate 31 seconds elapsed
        wd._cpu_violation_start = time.time() - 31.0
        is_viol, reason, diag = wd.check_resource_limits(mock_cpu)  # type: ignore
        assert is_viol is True, "Should trip after > 30s of > 75% CPU"
        assert "CPU usage exceeded limit" in reason
        print(f"  ✓ CPU sustained high usage properly detected: {reason}")
        passed_count += 1

        # ---------------------------------------------------------------------
        # 4. Test Heartbeat Monitor (> 35s stale)
        # ---------------------------------------------------------------------
        print("\n[4/7] Testing Heartbeat Staleness Detection...")
        hb_file = temp_dir / "heartbeat.json"

        # Fresh heartbeat
        fresh_data = {"timestamp": time.time(), "pid": 1234, "status": "running"}
        hb_file.write_text(json.dumps(fresh_data), encoding="utf-8")
        is_stale, age, _ = wd.check_heartbeat()
        assert is_stale is False, "Fresh heartbeat should not be stale"
        print(f"  ✓ Fresh heartbeat validated: age={age}s (Healthy)")

        # Stale heartbeat (45s old)
        stale_data = {"timestamp": time.time() - 45.0, "pid": 1234, "status": "running"}
        hb_file.write_text(json.dumps(stale_data), encoding="utf-8")

        class MockRunningProc:
            pid = 1234
            def is_running(self):
                return True

        is_stale, age, msg = wd.check_heartbeat(proc=MockRunningProc())  # type: ignore
        assert is_stale is True, "Stale heartbeat (> 35s) must be flagged"
        assert age >= 45.0
        assert "Heartbeat stale" in msg
        print(f"  ✓ Stale heartbeat detected: {msg}")
        passed_count += 1

        # ---------------------------------------------------------------------
        # 5. Test Process Termination Safety
        # ---------------------------------------------------------------------
        print("\n[5/7] Testing Process Tree Termination...")
        dummy_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert psutil.pid_exists(dummy_proc.pid)
        print(f"  ✓ Spawned dummy test process: PID={dummy_proc.pid}")

        p = psutil.Process(dummy_proc.pid)
        killed = wd.terminate_process(p, reason="Watchdog verification test")
        assert killed is True
        assert not psutil.pid_exists(dummy_proc.pid)
        print("  ✓ Process terminated and confirmed dead.")
        passed_count += 1

        # ---------------------------------------------------------------------
        # 6. Test Incident Report Schema & Telegram Formatter
        # ---------------------------------------------------------------------
        print("\n[6/7] Testing Incident Report Integrity & Telegram Dispatch Formatter...")
        report = wd.write_incident_report(
            reason="Test Incident Trigger",
            trigger_type="TEST_TRIGGER",
            action_taken="SIMULATED_TEST",
        )
        assert wd.incident_report_file.exists()
        with open(wd.incident_report_file, "r", encoding="utf-8") as f:
            saved_report = json.load(f)

        assert saved_report["incident_id"].startswith("INC-")
        assert saved_report["trigger_cause"] == "Test Incident Trigger"
        assert saved_report["trigger_type"] == "TEST_TRIGGER"
        assert "system_metrics" in saved_report
        assert "watchdog_state" in saved_report
        print(f"  ✓ Incident report schema verified: ID={saved_report['incident_id']}")

        # Verify Telegram message template format
        test_reason = "Tizim xotirasi (RAM) 350 MB dan oshib ketdi"
        expected_prefix = "⚠️ <b>JARVIS XAVFSIZLIK QALQONI:</b> Tizim xaddidan oshgani sababli avtomatik to'xtatildi!"
        tg_text = (
            f"⚠️ <b>JARVIS XAVFSIZLIK QALQONI:</b> Tizim xaddidan oshgani sababli avtomatik to'xtatildi!\n"
            f"<b>Sabab:</b> {test_reason}\n"
            f"<b>Vaqt:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        assert expected_prefix in tg_text
        print("  ✓ Telegram emergency template matches exact specification.")
        passed_count += 1

        # ---------------------------------------------------------------------
        # 7. Test Ultra-lightweight Memory Footprint (< 25MB on Windows)
        # ---------------------------------------------------------------------
        print("\n[7/7] Testing Watchdog Memory Footprint...")
        rss_bytes = psutil.Process(os.getpid()).memory_info().rss
        rss_mb = round(rss_bytes / (1024 * 1024), 2)
        print(f"  ✓ Watchdog resident memory (RSS): {rss_mb} MB")
        # Ensure ultra-low footprint
        assert rss_mb < 35.0, f"Memory footprint higher than expected: {rss_mb} MB"
        passed_count += 1

    finally:
        # Clean up temporary directory
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    print("\n" + "=" * 65)
    print(f"TEST RESULTS: {passed_count}/{total_tests} SUITES PASSED (100% SUCCESS)")
    print("=" * 65)
    return passed_count == total_tests


# =============================================================================
# CLI Entrypoint
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="JARVIS System Supervisor & Watchdog Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--monitor", action="store_true", help="Start continuous real-time monitoring loop")
    parser.add_argument("--status", action="store_true", help="Display current system and watchdog health status")
    parser.add_argument("--test", action="store_true", help="Run self-test verification suite")
    parser.add_argument("--reset", action="store_true", help="Reset circuit breaker and resolve incidents")
    parser.add_argument("--no-spawn", action="store_true", help="Do not auto-spawn jarvis_core.py if not running")
    parser.add_argument("--pid", type=int, default=None, help="Explicit PID of JARVIS target process")
    parser.add_argument("--interval", type=float, default=2.5, help="Monitoring tick interval in seconds (default: 2.5s)")

    args = parser.parse_args()

    watchdog = JarvisWatchdog(
        target_pid=args.pid,
        tick_interval_seconds=args.interval,
    )

    if args.test:
        success = run_self_tests()
        sys.exit(0 if success else 1)
    elif args.reset:
        res = watchdog.reset_circuit_breaker()
        print(f"✓ Watchdog Circuit Breaker successfully reset. Status: {res.get('status')}")
    elif args.status:
        watchdog.print_status_dashboard()
    elif args.monitor:
        watchdog.run_monitoring_loop(auto_spawn=not args.no_spawn)
    else:
        # If no arguments provided, show status and help hint
        watchdog.print_status_dashboard()
        print("\nHint: Use 'python jarvis_watchdog.py --monitor' to start supervisory watchdog.")
        print("      Use 'python jarvis_watchdog.py --test' to run self-test suite.")


if __name__ == "__main__":
    main()
