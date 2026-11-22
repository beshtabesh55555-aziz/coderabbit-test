"""
Self-Healing Persistent HTTPS Tunnel Runner for JARVIS WebApp
============================================================
Maintains a continuous OpenSSH reverse tunnel to localhost.run.
Automatically detects URL assignments and domain rotations, and updates
the Telegram Bot Menu Button dynamically via the Telegram Bot API.
"""

import sys
import os
import re
import time
import json
import logging
import subprocess
import urllib.request
from pathlib import Path
import config

BASE_DIR = Path(__file__).resolve().parent
URL_FILE = BASE_DIR / "tunnel_url.txt"

# Bot configuration loaded safely from config / .env
BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
ADMIN_CHAT_ID = int(config.TELEGRAM_ADMIN_CHAT_ID) if (config and getattr(config, "TELEGRAM_ADMIN_CHAT_ID", None)) else 0

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [TUNNEL-RUNNER] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TunnelRunner")

try:
    import psutil
    my_pid = os.getpid()
    my_ppid = os.getppid()
    my_script = str(Path(__file__).resolve()).lower()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        if proc.pid == my_pid or proc.pid == my_ppid:
            continue
        try:
            try:
                proc_cwd = Path(proc.cwd())
            except Exception:
                proc_cwd = Path.cwd()

            cmdline_parts = []
            for arg in (proc.info.get('cmdline') or []):
                p = Path(arg)
                if p.is_absolute():
                    cmdline_parts.append(str(p.resolve()).lower())
                else:
                    resolved = proc_cwd / p
                    cmdline_parts.append(str(resolved.resolve()).lower() if resolved.exists() else arg.lower())

            if my_script in cmdline_parts:
                logger.warning(f"Terminating duplicate tunnel_runner process (PID {proc.pid})...")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
except Exception:
    pass


def update_telegram_menu_button(app_url: str) -> bool:
    """Set Telegram Chat Menu Button to native Bot Commands menu."""
    menu_btn = {
        "type": "commands"
    }

    try:
        # Default menu button
        data_default = json.dumps({"menu_button": menu_btn}).encode("utf-8")
        req1 = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
            data=data_default,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req1, timeout=10) as r:
            res1 = json.loads(r.read().decode("utf-8"))
        logger.info(f"Reset default menu button to commands: {res1.get('ok')}")

        # Admin menu button
        if ADMIN_CHAT_ID:
            data_admin = json.dumps({"chat_id": ADMIN_CHAT_ID, "menu_button": menu_btn}).encode("utf-8")
            req2 = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
                data=data_admin,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req2, timeout=10) as r:
                res2 = json.loads(r.read().decode("utf-8"))
            logger.info(f"Reset admin menu button for {ADMIN_CHAT_ID} to commands: {res2.get('ok')}")

        return bool(res1.get("ok"))
    except Exception as e:
        logger.error(f"Failed to update Telegram menu button: {e}")
        return False


def run_tunnel_loop():
    """Continuously run and supervise the Tunnel with Cloudflare priority."""
    logger.info("Starting Persistent Self-Healing Tunnel Runner (Cloudflare Tunnel Priority)...")
    
    cf_bin = BASE_DIR / "cloudflared.exe"
    use_cloudflare = cf_bin.exists()
    
    cf_url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
    lhr_url_pattern = re.compile(r"https://[a-zA-Z0-9\.\-]+\.lhr\.life")
    current_url = ""
    menu_updated = False

    while True:
        proc = None
        if use_cloudflare:
            cmd = [str(cf_bin), "tunnel", "--url", "http://127.0.0.1:8088"]
            logger.info("Spawning Cloudflare Quick Tunnel (trycloudflare.com) ...")
            target_pattern = cf_url_pattern
        else:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=3",
                "-R", "80:127.0.0.1:8088",
                "nokey@localhost.run"
            ]
            logger.info("Spawning OpenSSH tunnel to nokey@localhost.run ...")
            target_pattern = lhr_url_pattern

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            for line in proc.stdout:
                line_str = line.strip()
                if not line_str:
                    continue

                # Search for target pattern
                match = target_pattern.search(line_str)
                if match:
                    detected_url = match.group(0)
                    if detected_url != current_url or not menu_updated:
                        current_url = detected_url
                        logger.info(f"🔥 Active HTTPS Tunnel detected: {current_url}")
                        URL_FILE.write_text(current_url, encoding="utf-8")
                        menu_updated = update_telegram_menu_button(current_url)

            proc.wait()
            logger.warning(f"Tunnel process exited with code {proc.returncode}. Reconnecting in 3 seconds...")
            if use_cloudflare and proc.returncode != 0:
                logger.warning("Cloudflare tunnel failed. Switching to OpenSSH fallback...")
                use_cloudflare = False
        except Exception as e:
            logger.error(f"Error in tunnel loop: {e}")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.kill()
                except Exception:
                    pass
            if use_cloudflare:
                logger.warning("Falling back to OpenSSH localhost.run...")
                use_cloudflare = False

        time.sleep(3)


if __name__ == "__main__":
    run_tunnel_loop()
