"""
Comprehensive UI Verification Test Suite for Monika Modern Glassmorphism Dashboard (Milestone 3)
Verifies:
1. Design System Tokens (Obsidian #050811 base, frosted glass saturation, specular borders, Inter & JetBrains Mono typography).
2. Responsive Dual-Viewport Architecture (Desktop split command center lg:grid-cols-12 and Mobile touch tabs with lg:hidden nav bar).
3. Dedicated Screen Lock and Unlock Controls (Distinct header/card buttons, PIN unlock dialog modal, reactive lock status badges).
4. Visual System Telemetry (Animated progress gauges for CPU, RAM, Disk, battery, active window monitor pill).
5. Interactive Monika Chat (Quick prompt pills, input with voice mic trigger, inline screenshot zoom modal, diagnostic error banners).
6. PWA & Telegram WebApp Integration (manifest.json link, sw.js registration, Telegram WebApp SDK script).
7. Backend Serving Routes (GET /, /manifest.json, /sw.js, /api/screen/status, /api/screen/lock, /api/screen/unlock, /api/status).
"""

import sys
import os
import json
import unittest
import re
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from jarvis_webapp import create_app
import config


class TestWebappUIStatic(unittest.TestCase):
    """Static inspections of webapp/index.html verifying design system, DOM elements, and architecture."""

    @classmethod
    def setUpClass(cls):
        cls.html_path = BASE_DIR / "webapp" / "index.html"
        with open(cls.html_path, "r", encoding="utf-8") as f:
            cls.html_content = f.read()

    def test_01_design_system_and_color_palette(self):
        """Verify obsidian base (#050811), radial gradients, and frosted glass properties."""
        content = self.html_content

        # Obsidian base theme token
        self.assertIn("#050811", content, "Missing obsidian base color #050811")
        self.assertIn("theme-color", content)
        self.assertIn('content="#050811"', content)

        # Ambient radial gradients (cyan, violet, emerald accents)
        self.assertIn("radial-gradient", content, "Missing ambient radial gradients")
        self.assertIn("rgba(6, 182, 212", content, "Missing cyan ambient accent")
        self.assertIn("rgba(139, 92, 246", content, "Missing violet ambient accent")
        self.assertIn("rgba(16, 185, 129", content, "Missing emerald ambient accent")

        # Multi-layer frosted glass styling
        self.assertIn("backdrop-filter: blur(20px) saturate(180%)", content, "Missing frosted glass saturation/blur filter")
        self.assertIn("-webkit-backdrop-filter: blur(20px) saturate(180%)", content)

        # Specular border styling
        self.assertIn("rgba(255, 255, 255, 0.08)", content, "Missing subtle glass border")

        # Typography: Inter & JetBrains Mono
        self.assertIn("family=Inter", content, "Missing Google Fonts Inter preconnect/link")
        self.assertIn("family=JetBrains+Mono", content, "Missing Google Fonts JetBrains Mono link")
        self.assertIn("'Inter', sans-serif", content)
        self.assertIn("'JetBrains Mono', monospace", content)

    def test_02_responsive_dual_viewport_layout(self):
        """Verify desktop split command center grid and mobile navigation flow."""
        content = self.html_content

        # Desktop Grid Classes
        self.assertIn("lg:grid", content, "Missing lg:grid responsive desktop split container")
        self.assertIn("lg:grid-cols-12", content, "Missing lg:grid-cols-12 desktop command center layout")
        self.assertIn("lg:col-span-7", content, "Missing chat column span on desktop")
        self.assertIn("lg:col-span-5", content, "Missing telemetry/controls column span on desktop")

        # Mobile navigation bar hidden on desktop
        self.assertIn("lg:hidden", content, "Missing lg:hidden on mobile navigation bar")
        self.assertIn("nav-btn", content)
        self.assertIn("data-tab=\"chat\"", content)
        self.assertIn("data-tab=\"dashboard\"", content)
        self.assertIn("data-tab=\"memory\"", content)

    def test_03_dedicated_screen_lock_and_unlock_controls(self):
        """Verify explicit, distinct buttons for both Lock and Unlock, PIN modal, and reactive badges."""
        content = self.html_content

        # Distinct Lock Controls in Header and Dashboard
        self.assertIn("id=\"btn-header-lock\"", content, "Missing dedicated header Lock button")
        self.assertIn("id=\"btn-dedicated-lock\"", content, "Missing dedicated dashboard Lock button")
        self.assertIn("executeScreenLock()", content, "Missing lock execution handler")
        self.assertIn("/api/screen/lock", content, "Missing explicit POST /api/screen/lock reference")

        # Distinct Unlock Controls in Header and Dashboard
        self.assertIn("id=\"btn-header-unlock\"", content, "Missing dedicated header Unlock button")
        self.assertIn("id=\"btn-dedicated-unlock\"", content, "Missing dedicated dashboard Unlock button")
        self.assertIn("openPinModal()", content, "Missing PIN modal open handler")
        self.assertIn("/api/screen/unlock", content, "Missing explicit POST /api/screen/unlock reference")

        # PIN Unlock Dialog Modal
        self.assertIn("id=\"pin-unlock-modal\"", content, "Missing PIN Unlock Dialog Modal container")
        self.assertIn("id=\"pin-input-field\"", content, "Missing PIN input text/password element")
        self.assertIn("id=\"btn-submit-unlock\"", content, "Missing PIN submit unlock button")
        self.assertIn("executeScreenUnlock()", content, "Missing screen unlock execution logic")

        # Reactive Status Indicators
        self.assertIn("id=\"header-lock-badge\"", content, "Missing header reactive lock badge")
        self.assertIn("id=\"card-lock-badge\"", content, "Missing dashboard card reactive lock badge")
        self.assertIn("updateLockStatusUI", content, "Missing dynamic lock status UI synchronizer function")

    def test_04_visual_system_telemetry_meters(self):
        """Verify CPU, RAM, Disk, Battery progress meters and Active Window monitor."""
        content = self.html_content

        # Active Window Monitor
        self.assertIn("id=\"dash-active-window\"", content, "Missing active window display element")

        # Visual progress bars for telemetry
        self.assertIn("id=\"meter-cpu-bar\"", content, "Missing CPU animated progress bar meter")
        self.assertIn("id=\"meter-ram-bar\"", content, "Missing RAM animated progress bar meter")
        self.assertIn("id=\"meter-disk-bar\"", content, "Missing Disk animated progress bar meter")
        self.assertIn("id=\"meter-battery-bar\"", content, "Missing Battery animated progress bar meter")

        # Numeric metric labels
        self.assertIn("id=\"dash-cpu\"", content, "Missing CPU percentage label")
        self.assertIn("id=\"dash-ram\"", content, "Missing RAM percentage label")
        self.assertIn("id=\"dash-disk\"", content, "Missing Disk free space label")
        self.assertIn("id=\"dash-battery\"", content, "Missing Battery percentage label")
        self.assertIn("id=\"dash-uptime\"", content, "Missing Uptime label")

    def test_05_monika_chat_terminal_and_inline_media(self):
        """Verify chat stream, quick prompt pills, voice mic, inline screenshots, and zoom modal."""
        content = self.html_content

        # Chat stream & input controls
        self.assertIn("id=\"chat-messages\"", content, "Missing chat messages stream")
        self.assertIn("id=\"chat-input\"", content, "Missing chat text input")
        self.assertIn("id=\"send-btn\"", content, "Missing chat submit send button")
        self.assertIn("id=\"voice-btn\"", content, "Missing voice mic trigger button")
        self.assertIn("id=\"mic-icon\"", content, "Missing mic icon")
        self.assertIn("id=\"typing-indicator\"", content, "Missing Monika typing bounce indicator")

        # Quick prompt chips
        self.assertIn("Skrinshot ol", content, "Missing quick screenshot prompt pill")
        self.assertIn("Tizim holati", content, "Missing quick system status prompt pill")
        self.assertIn("Ekranni qulfla", content, "Missing quick lock screen prompt pill")
        self.assertIn("Ekranni och", content, "Missing quick unlock screen prompt pill")
        self.assertIn("Fayllarni qidir", content, "Missing quick file search prompt pill")

        # Inline screenshot click-to-zoom modal
        self.assertIn("id=\"image-zoom-modal\"", content, "Missing inline screenshot click-to-zoom modal")
        self.assertIn("id=\"image-zoom-target\"", content, "Missing image zoom target element")
        self.assertIn("openImageZoomModal", content, "Missing openImageZoomModal handler")

        # Diagnostic error banner handling in chat
        self.assertIn("isErrorBanner", content, "Missing actionable diagnostic error banner detection")
        self.assertIn("Diagnostika", content, "Missing diagnostic badge tag")

    def test_06_pwa_and_telegram_integration(self):
        """Verify PWA manifest link, service worker registration, and Telegram WebApp SDK."""
        content = self.html_content

        # PWA Manifest & Service Worker
        self.assertIn('<link rel="manifest" href="/manifest.json">', content, "Missing manifest.json link tag")
        self.assertIn("navigator.serviceWorker.register('/sw.js'", content, "Missing service worker registration script")

        # Telegram WebApp SDK
        self.assertIn("https://telegram.org/js/telegram-web-app.js", content, "Missing Telegram WebApp SDK script")
        self.assertIn("window.Telegram?.WebApp", content, "Missing Telegram WebApp initialization")
        self.assertIn("triggerHaptic", content, "Missing haptic feedback integration")

        # Surveillance modal
        self.assertIn("id=\"surveillance-modal\"", content, "Missing on-demand surveillance modal")
        self.assertIn("id=\"surveillance-modal-img\"", content, "Missing surveillance image element")


class TestWebappUIRoutes(AioHTTPTestCase):
    """Integration tests verifying HTTP route responses for the UI, PWA assets, and lock endpoints."""

    async def get_application(self):
        """Construct the test aiohttp application instance."""
        return create_app()

    @property
    def auth_headers(self):
        key = getattr(config, "WEBAPP_API_KEY", "monika-secret-key-2026")
        return {
            "X-API-Key": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }

    @unittest_run_loop
    async def test_01_serve_index_html(self):
        """Verify GET / and GET /index.html serve the redesigned glassmorphism UI."""
        # 1. Root route
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        text = await resp.text()
        self.assertIn("Monika AI", text)
        self.assertIn("#050811", text)
        self.assertIn("backdrop-filter: blur(20px) saturate(180%)", text)

        # 2. /index.html alias
        resp2 = await self.client.get("/index.html")
        self.assertEqual(resp2.status, 200)
        self.assertIn("text/html", resp2.headers.get("Content-Type", ""))

    @unittest_run_loop
    async def test_02_serve_pwa_manifest_and_sw(self):
        """Verify GET /manifest.json and GET /sw.js serve valid PWA assets."""
        # Manifest
        resp_m = await self.client.get("/manifest.json")
        self.assertEqual(resp_m.status, 200)
        manifest_data = await resp_m.json()
        self.assertIn("name", manifest_data)
        self.assertIn("Monika", manifest_data["name"])
        self.assertIn("start_url", manifest_data)

        # Service Worker
        resp_sw = await self.client.get("/sw.js")
        self.assertEqual(resp_sw.status, 200)
        sw_text = await resp_sw.text()
        self.assertIn("CACHE_NAME", sw_text)
        self.assertIn("fetch", sw_text)

    @unittest_run_loop
    async def test_03_screen_lock_and_unlock_api_routes(self):
        """Verify GET /api/screen/status, POST /api/screen/lock, and POST /api/screen/unlock."""
        # 1. Screen Status
        resp_status = await self.client.get("/api/screen/status", headers=self.auth_headers)
        self.assertEqual(resp_status.status, 200)
        status_data = await resp_status.json()
        self.assertEqual(status_data.get("status"), "success")
        self.assertIn("is_locked", status_data)
        self.assertIn("security_status", status_data)

        # 2. Screen Lock Endpoint
        resp_lock = await self.client.post("/api/screen/lock", json={}, headers=self.auth_headers)
        self.assertEqual(resp_lock.status, 200)
        lock_data = await resp_lock.json()
        self.assertEqual(lock_data.get("status"), "success")
        self.assertTrue(lock_data.get("is_locked"))

        # 3. Screen Unlock Endpoint (with PIN)
        resp_unlock = await self.client.post("/api/screen/unlock", json={"pin": "1234"}, headers=self.auth_headers)
        self.assertEqual(resp_unlock.status, 200)
        unlock_data = await resp_unlock.json()
        self.assertIn("status", unlock_data)
        self.assertIn("is_locked", unlock_data)

    @unittest_run_loop
    async def test_04_telemetry_status_route(self):
        """Verify /api/status provides real-time system metrics for UI meters."""
        resp = await self.client.get("/api/status", headers=self.auth_headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("cpu_percent", data)
        self.assertIn("ram_percent", data)
        self.assertIn("disk_free_gb", data)
        self.assertIn("battery_percent", data)
        self.assertIn("active_window", data)
        self.assertIn("is_locked", data)


if __name__ == "__main__":
    unittest.main()
