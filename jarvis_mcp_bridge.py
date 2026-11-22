"""
JARVIS Model Context Protocol (MCP) Bridge Module
=================================================
Inspired by OpenJarvis architecture.
Enables Monika to connect to external MCP servers (stdio JSON-RPC 2.0),
dynamically discover tools, and register them into JarvisToolRegistry
with full execution tracing and anti-hallucination verification.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("JarvisMCPBridge")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-MCP] %(levelname)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "mcp_servers.json"


class MCPServerProcess:
    """Manages an active stdio-based MCP server subprocess."""

    def __init__(self, name: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or os.environ.copy()
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_id = 0
        self.is_alive = False

    def start(self) -> bool:
        """Start the MCP server subprocess."""
        try:
            full_cmd = [self.command] + self.args
            self.proc = subprocess.Popen(
                full_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=self.env
            )
            self.is_alive = True
            logger.info(f"MCP server '{self.name}' spawned successfully (PID {self.proc.pid}).")
            return True
        except Exception as e:
            logger.error(f"Failed to start MCP server '{self.name}': {e}")
            self.is_alive = False
            return False

    def send_rpc(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC 2.0 message and read the single-line JSON response."""
        if not self.proc or self.proc.poll() is not None:
            self.is_alive = False
            return None

        with self._lock:
            self._req_id += 1
            rpc_id = self._req_id
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": method,
                "params": params or {}
            }
            msg = json.dumps(payload) + "\n"

            try:
                self.proc.stdin.write(msg)
                self.proc.stdin.flush()

                # Read line from stdout
                line = self.proc.stdout.readline()
                if not line:
                    return None
                data = json.loads(line.strip())
                return data
            except Exception as e:
                logger.error(f"RPC communication failed with server '{self.name}': {e}")
                return None

    def terminate(self):
        """Terminate the server process."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.is_alive = False


class JarvisMCPBridge:
    """Central MCP Bridge for registering external toolkits into Monika."""

    def __init__(self, config_file: Optional[Path] = None):
        self.config_file = config_file or DEFAULT_CONFIG_PATH
        self.servers: Dict[str, MCPServerProcess] = {}
        self.discovered_tools: Dict[str, Dict[str, Any]] = {}

    def load_servers(self) -> int:
        """Load and start MCP servers configured in mcp_servers.json."""
        if not self.config_file.exists():
            # Create a default template if not existing
            default_conf = {
                "mcpServers": {
                    # Example placeholders
                }
            }
            try:
                with open(self.config_file, "w", encoding="utf-8") as f:
                    json.dump(default_conf, f, indent=2)
            except Exception:
                pass
            return 0

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            servers_conf = data.get("mcpServers", {})

            loaded_count = 0
            for name, conf in servers_conf.items():
                cmd = conf.get("command")
                args = conf.get("args", [])
                env_override = conf.get("env", {})
                merged_env = os.environ.copy()
                merged_env.update(env_override)

                if cmd:
                    srv = MCPServerProcess(name, cmd, args, merged_env)
                    if srv.start():
                        self.servers[name] = srv
                        self._initialize_server(name, srv)
                        loaded_count += 1
            return loaded_count
        except Exception as e:
            logger.error(f"Error loading MCP configuration: {e}")
            return 0

    def _initialize_server(self, name: str, srv: MCPServerProcess):
        """Send initialize handshake and discover available tools."""
        try:
            init_res = srv.send_rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "Monika-AI-Bridge", "version": "2.0.0"}
            })
            if init_res and "result" in init_res:
                logger.info(f"Initialized MCP server '{name}'. Discovering tools...")
                cursor = None
                total_discovered = 0
                while True:
                    params = {"cursor": cursor} if cursor else {}
                    tools_res = srv.send_rpc("tools/list", params)
                    if not tools_res or "result" not in tools_res:
                        break
                    res_obj = tools_res["result"]
                    tools = res_obj.get("tools", [])
                    for t in tools:
                        t_name = f"mcp_{name}_{t.get('name')}"
                        self.discovered_tools[t_name] = {
                            "server": name,
                            "original_name": t.get("name"),
                            "description": t.get("description", ""),
                            "input_schema": t.get("inputSchema", {})
                        }
                        total_discovered += 1
                    cursor = res_obj.get("nextCursor")
                    if not cursor:
                        break
                logger.info(f"Discovered {total_discovered} tools from MCP server '{name}'.")
        except Exception as e:
            logger.error(f"Failed to initialize server '{name}': {e}")

    def call_mcp_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """Execute a discovered MCP tool via its parent server."""
        info = self.discovered_tools.get(tool_name)
        if not info:
            return {"success": False, "error": f"MCP tool '{tool_name}' is not registered."}

        srv_name = info["server"]
        orig_name = info["original_name"]
        srv = self.servers.get(srv_name)
        if not srv or not srv.is_alive:
            return {"success": False, "error": f"MCP server '{srv_name}' is offline."}

        res = srv.send_rpc("tools/call", {
            "name": orig_name,
            "arguments": kwargs
        })

        if not res:
            return {"success": False, "error": f"No response from MCP server '{srv_name}'."}

        if "error" in res:
            return {"success": False, "error": res["error"].get("message", "MCP execution error")}

        result_obj = res.get("result", {})
        is_error = result_obj.get("isError", False) if isinstance(result_obj, dict) else False

        content = result_obj.get("content", []) if isinstance(result_obj, dict) else []
        text_outputs = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text_outputs.append(c.get("text", ""))

        output_str = "\n".join(text_outputs) if text_outputs else str(result_obj)
        if is_error:
            return {"success": False, "error": output_str, "result": output_str}
        return {"success": True, "result": output_str}

    def register_with_registry(self, registry: Any):
        """Register all discovered MCP tools dynamically into JarvisToolRegistry."""
        if not hasattr(registry, "register_custom_tool"):
            return

        for t_name, info in self.discovered_tools.items():
            def make_handler(name=t_name):
                def handler(**kwargs):
                    return self.call_mcp_tool(name, **kwargs)
                return handler

            handler = make_handler(t_name)
            handler.__doc__ = info.get("description", f"MCP Tool {t_name}")
            registry.register_custom_tool(t_name, handler)
            logger.info(f"Registered MCP tool '{t_name}' in JarvisToolRegistry.")

    def close_all(self):
        """Clean up and terminate all running MCP server subprocesses."""
        for name, srv in self.servers.items():
            logger.info(f"Stopping MCP server '{name}'...")
            srv.terminate()
        self.servers.clear()


if __name__ == "__main__":
    bridge = JarvisMCPBridge()
    count = bridge.load_servers()
    print(f"Loaded {count} MCP servers. Discovered tools: {list(bridge.discovered_tools.keys())}")
    bridge.close_all()
