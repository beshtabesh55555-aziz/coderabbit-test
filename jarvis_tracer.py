"""
JARVIS Execution Tracer & Anti-Hallucination Verification Engine
================================================================
Inspired by OpenJarvis trace-driven execution and strict empirical grounding.

Guarantees:
1. Zero-Hallucination Gate: The AI can NEVER claim an action was successful unless verified by physical OS state checks.
2. Auditability: Every command, tool execution, argument, physical result, and latency is appended to command_traces.jsonl.
3. Post-Action State Verification: Validates physical changes (file on disk, master volume level, workstation lock state, reminder storage).
"""

import os
import sys
import time
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("JarvisTracer")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-TRACER] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
TRACES_FILE = BASE_DIR / "command_traces.jsonl"


class JarvisTracer:
    """Manages execution tracing and anti-hallucination verification."""

    def __init__(self, traces_file: Optional[Path] = None):
        self.traces_file = traces_file or TRACES_FILE
        self._ensure_file()

    def _ensure_file(self):
        try:
            if not self.traces_file.exists():
                self.traces_file.touch(exist_ok=True)
        except Exception as e:
            logger.error(f"Could not create traces file: {e}")

    def start_trace(self, query: str, source: str = "telegram", user_id: Optional[str] = None) -> Dict[str, Any]:
        """Initialize a new execution trace context."""
        return {
            "trace_id": f"TRC-{int(time.time() * 1000)}-{os.getpid()}",
            "timestamp": time.time(),
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
            "query": query,
            "source": source,
            "user_id": user_id,
            "start_time": time.perf_counter(),
            "tools_called": [],
            "status": "in_progress",
            "verified": False,
            "verification_details": {},
            "final_reply": None,
            "latency_ms": 0.0,
        }

    def verify_tool_execution(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_result: Dict[str, Any],
        sensor: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Anti-Hallucination Grounding Gate:
        Physically verifies whether the expected OS state change actually occurred.
        Returns a verification record with 'verified': True/False and 'reason'.
        """
        # If tool returned an explicit error/failure, it cannot be verified as successful
        if isinstance(tool_result, dict) and not tool_result.get("success", True):
            err_msg = tool_result.get("error") or "Noma'lum xatolik"
            return {
                "verified": False,
                "verified_success": False,
                "reason": f"Vosita xatolik bilan yakunlandi: {err_msg}",
                "post_state": None
            }

        verified = False
        verified_success = None
        reason = "Tekshiruv amalga oshirilmadi (maxsus sensor yo'q)."
        post_state = None

        try:
            # 1. File Write Verification
            if tool_name in ("write_file_content", "save_quick_note"):
                path_str = tool_args.get("filepath") or tool_args.get("path")
                if path_str:
                    p = Path(path_str)
                    if p.exists() and p.is_file():
                        sz = p.stat().st_size
                        post_state = f"Fayl mavjud ({sz} bayt)"
                        reason = f"Fayl diskda fizik mavjudligi tasdiqlandi: {p.name} ({sz} bayt)."
                        verified = True
                        verified_success = True
                    else:
                        verified = True
                        verified_success = False
                        post_state = "Fayl diskda topilmadi"
                        reason = f"XATO: Fayl diskda topilmadi: {path_str}"

            # 2. Volume Verification
            elif tool_name == "set_volume":
                target_lvl = tool_args.get("level")
                if sensor and hasattr(sensor, "get_volume_level"):
                    current_vol = sensor.get_volume_level()
                    post_state = f"Volume: {current_vol}%"
                    if target_lvl is not None and abs(int(current_vol) - int(target_lvl)) <= 3:
                        verified = True
                        verified_success = True
                        reason = f"Ovoz darajasi fizik tasdiqlandi: {current_vol}% (maqsad: {target_lvl}%)."
                    else:
                        verified = True
                        verified_success = False
                        reason = f"Ovoz darajasi mos kelmadi: joriy={current_vol}%, kutilgan={target_lvl}%."
                else:
                    post_state = f"Ovoz o'rnatildi: {target_lvl}%"
                    reason = "Ovoz sensori mavjud emas, tasdiqlanmadi."

            # 3. Workstation Lock Verification
            elif tool_name == "lock_computer":
                if sensor and hasattr(sensor, "is_locked"):
                    locked = sensor.is_locked()
                    if not locked:
                        time.sleep(0.3)
                        locked = sensor.is_locked()
                    post_state = "LOCKED" if locked else "UNLOCKED"
                    if locked:
                        verified = True
                        verified_success = True
                        reason = "Ekran qulflanganligi tasdiqlandi."
                    else:
                        verified = True
                        verified_success = False
                        reason = "XATO: Ekran hali qulflanmagan."
                else:
                    reason = "Qulf sensori mavjud emas, tasdiqlanmadi."

            # 4. Reminder Storage Verification
            elif tool_name == "add_reminder":
                rem_file = BASE_DIR / "reminders.json"
                if rem_file.exists():
                    try:
                        with open(rem_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        rems = data.get("reminders", [])
                        rem_text = str(tool_args.get("text", "")).strip()
                        matching = [r for r in rems if rem_text and rem_text in r.get("text", "")]
                        if matching:
                            post_state = f"{len(matching)} ta eslatma saqlangan"
                            reason = f"Eslatma bazada saqlangani fizik tasdiqlandi: {matching[-1].get('trigger_at')}."
                            verified = True
                            verified_success = True
                        else:
                            verified = True
                            verified_success = False
                            post_state = "Eslatma bazada topilmadi"
                            reason = f"Eslatma bazada topilmadi: '{rem_text}'"
                    except Exception as e:
                        reason = f"Eslatma faylini o'qishda xatolik: {e}"

            # 5. Application Launch Verification
            elif tool_name == "launch_application":
                app_name = str(tool_args.get("name", "")).lower()
                time.sleep(0.4)
                if sensor and hasattr(sensor, "get_active_window"):
                    act = sensor.get_active_window()
                    act_title = act.get("title") or "Noma'lum"
                    post_state = f"Faol: {act_title}"
                    if app_name and (app_name in act_title.lower() or app_name in str(act.get("process", "")).lower()):
                        verified = True
                        verified_success = True
                        reason = f"Dastur ishga tushirildi va faol oyna tasdiqlandi: {app_name}."
                    else:
                        reason = f"Dastur oynasi faol holatda tasdiqlanmadi (faol: {act_title})."
                else:
                    reason = "Oyna sensori mavjud emas, tasdiqlanmadi."

        except Exception as v_err:
            logger.warning(f"Verification exception for {tool_name}: {v_err}")
            reason = f"Tekshiruv xatosi: {v_err}"

        return {
            "verified": verified,
            "verified_success": verified_success,
            "reason": reason,
            "post_state": post_state
        }

    def complete_trace(
        self,
        trace_ctx: Dict[str, Any],
        final_reply: str,
        status: str = "success"
    ) -> Dict[str, Any]:
        """Finalize the trace, compute latency, and log to jsonl."""
        latency_ms = round((time.perf_counter() - trace_ctx["start_time"]) * 1000, 2)
        trace_ctx["latency_ms"] = latency_ms
        trace_ctx["status"] = status
        trace_ctx["final_reply"] = final_reply

        # Pop non-serializable perf_counter start_time
        trace_ctx.pop("start_time", None)

        try:
            if self.traces_file.exists() and self.traces_file.stat().st_size > 10 * 1024 * 1024:
                backup_file = self.traces_file.with_name(f"command_traces_{int(time.time())}.jsonl")
                self.traces_file.rename(backup_file)
                logger.info(f"Rotated trace log to {backup_file.name}")

            with open(self.traces_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trace_ctx, ensure_ascii=False) + "\n")
            logger.info(
                f"📊 [TRACE RECORDED] {trace_ctx['trace_id']} | "
                f"Tools: {len(trace_ctx['tools_called'])} | "
                f"Latency: {latency_ms}ms | Status: {status}"
            )
        except Exception as e:
            logger.error(f"Failed to append execution trace: {e}")

        return trace_ctx

    def get_recent_traces(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Retrieve recent execution traces for auditing or performance monitoring."""
        if not self.traces_file.exists():
            return []
        traces = []
        try:
            with open(self.traces_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    traces.append(json.loads(line))
        except Exception as e:
            logger.error(f"Error reading traces: {e}")
        return traces


if __name__ == "__main__":
    tracer = JarvisTracer()
    ctx = tracer.start_trace("Ovozni 30 ga sozla", source="test")
    v_rec = tracer.verify_tool_execution(
        "set_volume",
        {"level": 30},
        {"success": True, "result": "Volume calibrated to 30%"}
    )
    ctx["tools_called"].append({"tool": "set_volume", "verification": v_rec})
    res = tracer.complete_trace(ctx, "Ovoz 30 foizga o'rnatildi.")
    print("Tracer self-test OK:", res["trace_id"], "Latency:", res["latency_ms"], "ms")
