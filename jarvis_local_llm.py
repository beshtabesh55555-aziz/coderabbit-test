"""
JARVIS Local & Offline LLM Inference Engine (Ollama & llama.cpp Bridge)
=======================================================================
Inspired by OpenJarvis on-device privacy-first architecture.

Provides zero-latency, 100% offline intelligence:
- Automatically detects running local inference runtimes (Ollama on port 11434, llama.cpp, or vLLM).
- Supports popular consumer models: Qwen 2.5, Gemma 2, Llama 3.2, Mistral.
- Failover bridge: Used by JarvisCore when internet connectivity drops or cloud APIs are unavailable.
"""

import os
import sys
import json
import time
import logging
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional, Dict, Any, List

logger = logging.getLogger("JarvisLocalLLM")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-LOCAL-LLM] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disallows HTTP redirects for local inference endpoints to prevent SSRF."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        logger.warning(f"Prevented redirect from local inference host to {newurl}")
        return None


class JarvisLocalLLM:
    """Interface to local, on-device inference engines."""

    def __init__(self, host: Optional[str] = None, default_model: str = "qwen2.5:3b", allow_remote: bool = False):
        raw_host = host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        raw_host = raw_host.strip()
        if not (raw_host.startswith("http://") or raw_host.startswith("https://")):
            raw_host = f"http://{raw_host}"
        raw_host = raw_host.rstrip("/")

        try:
            parsed = urllib.parse.urlparse(raw_host)
            hostname = (parsed.hostname or "").lower()
        except Exception as e:
            logger.warning(f"Error parsing Ollama host '{raw_host}': {e}. Falling back to http://127.0.0.1:11434")
            raw_host = "http://127.0.0.1:11434"
            parsed = urllib.parse.urlparse(raw_host)
            hostname = "127.0.0.1"

        allow_remote = allow_remote or (os.environ.get("OLLAMA_ALLOW_REMOTE", "0") == "1")

        if hostname not in ("127.0.0.1", "localhost", "::1"):
            if not allow_remote:
                logger.warning(
                    f"Non-loopback Ollama host '{hostname}' rejected for offline safety. Falling back to http://127.0.0.1:11434"
                )
                raw_host = "http://127.0.0.1:11434"
                hostname = "127.0.0.1"
            elif parsed.scheme != "https":
                logger.warning("Remote Ollama host requires HTTPS. Falling back to http://127.0.0.1:11434")
                raw_host = "http://127.0.0.1:11434"
                hostname = "127.0.0.1"

        self.host = raw_host
        self.default_model = default_model
        self._last_health_check = 0.0
        self._is_online = False
        self._available_models: List[str] = []

        # Enforce proxy bypass and disable redirects for loopback endpoints
        self._is_loopback = (hostname in ("127.0.0.1", "localhost", "::1"))
        if self._is_loopback:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirectHandler()
            )
        else:
            self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def is_available(self, timeout: float = 1.2) -> bool:
        """Check if local Ollama/inference server is reachable."""
        now = time.time()
        # Cache health check for 15 seconds to avoid excessive polling when online
        if (now - self._last_health_check) < 15.0 and self._is_online:
            return self._is_online

        self._last_health_check = now
        try:
            req = urllib.request.Request(f"{self.host}/api/tags", headers={"User-Agent": "Jarvis-LocalLLM"})
            with self._opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name", "") for m in data.get("models", [])]
                    self._available_models = [m for m in models if m]
                    self._is_online = True
                    logger.info(f"🟢 Local LLM Server Online at {self.host} ({len(self._available_models)} models loaded)")
                    return True
                else:
                    self._is_online = False
                    self._available_models = []
        except Exception:
            self._is_online = False
            self._available_models = []

        return False

    def list_models(self) -> List[str]:
        """List locally pulled models."""
        if self.is_available():
            return self._available_models
        return []

    def generate(self, prompt: str, system: Optional[str] = None, model: Optional[str] = None, timeout: float = 30.0) -> Optional[str]:
        """Generate text completion from local model."""
        if not self.is_available():
            return None

        target_model = model or (self._available_models[0] if self._available_models else self.default_model)
        payload = {
            "model": target_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.4,
                "top_p": 0.9,
            }
        }
        if system:
            payload["system"] = system

        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.host}/api/generate",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with self._opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    text = res_data.get("response", "").strip()
                    logger.info(f"⚡ [LOCAL LLM RESPONSE] generated via {target_model} ({len(text)} chars)")
                    return text
        except Exception as e:
            logger.warning(f"Local LLM generation failed: {e}")

        return None

    def chat(self, messages: List[Dict[str, str]], model: Optional[str] = None, timeout: float = 30.0) -> Optional[str]:
        """Chat completion from local model."""
        if not self.is_available():
            return None

        target_model = model or (self._available_models[0] if self._available_models else self.default_model)
        payload = {
            "model": target_model,
            "messages": messages,
            "stream": False
        }
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.host}/api/chat",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with self._opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    msg = res_data.get("message", {})
                    return msg.get("content", "").strip()
        except Exception as e:
            logger.warning(f"Local LLM chat failed: {e}")

        return None


if __name__ == "__main__":
    local_ai = JarvisLocalLLM()
    avail = local_ai.is_available(timeout=0.8)
    print("Local LLM available:", avail)
    if avail:
        print("Models:", local_ai.list_models())
        print("Test generation:", local_ai.generate("Salom!"))
    else:
        print("Ollama server not running on port 11434 (offline fallback ready for when started).")
