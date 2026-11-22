"""
JARVIS Desktop Dashboard & HUD Interface
Built with CustomTkinter for a modern, sleek interface.
Displays live camera snapshot, system vitals, security status, and voice controls.
"""

import os
import sys
import time
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

from PIL import Image, ImageTk

# Import local modules
import config
from jarvis_system import JarvisSystem

class JarvisHUD(ctk.CTk if ctk else tk.Tk):
    def __init__(self, core_ref=None):
        super().__init__()
        
        self.core = core_ref
        self.system = JarvisSystem()
        
        # Window setup
        self.title("J.A.R.V.I.S. - Tactical AI & Security HUD")
        self.geometry("900x650")
        self.minsize(800, 600)
        
        if ctk:
            ctk.set_appearance_mode("Dark")
            ctk.set_default_color_theme("blue")

        self._build_ui()
        self._start_vitals_thread()

    def _build_ui(self):
        # Header banner
        header = ctk.CTkFrame(self, height=60, corner_radius=0, fg_color="#101827")
        header.pack(fill="x", side="top", padx=0, pady=0)
        
        title_label = ctk.CTkLabel(
            header,
            text="J.A.R.V.I.S.  //  PERSONAL SURVEILLANCE & AI SUITE",
            font=ctk.CTkFont(family="Consolas", size=18, weight="bold"),
            text_color="#00ffff"
        )
        title_label.pack(side="left", padx=20, pady=15)

        self.status_badge = ctk.CTkLabel(
            header,
            text="● SYSTEM ONLINE",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#10b981"
        )
        self.status_badge.pack(side="right", padx=20)

        # Main Layout: 2 Columns
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)
        main_frame.grid_columnconfigure(0, weight=3) # Left: Camera & Security
        main_frame.grid_columnconfigure(1, weight=2) # Right: Controls & Vitals
        main_frame.grid_rowconfigure(0, weight=1)

        # LEFT COLUMN: Camera Monitor & Snapshot
        left_col = ctk.CTkFrame(main_frame, corner_radius=10, fg_color="#1a202c")
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        cam_title = ctk.CTkLabel(
            left_col,
            text="📷 CAMERA SURVEILLANCE FEED",
            font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
            text_color="#94a3b8"
        )
        cam_title.pack(anchor="w", padx=15, pady=(15, 5))

        # Camera Display Canvas/Label
        self.camera_label = ctk.CTkLabel(
            left_col,
            text="[ Kamera tasviri yuklanmoqda... ]\nSurat olish tugmasini bosing",
            fg_color="#0f172a",
            corner_radius=8,
            height=260
        )
        self.camera_label.pack(fill="x", padx=15, pady=10)

        # Motion & Security Status Box
        sec_box = ctk.CTkFrame(left_col, fg_color="#0f172a", corner_radius=8)
        sec_box.pack(fill="x", padx=15, pady=5)
        
        self.lbl_motion = ctk.CTkLabel(
            sec_box,
            text="🛡️ Qo'riqlash holati: FAOL (Harakat datchigi yoqilgan)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8"
        )
        self.lbl_motion.pack(anchor="w", padx=10, pady=8)

        # Event Log Window
        log_title = ctk.CTkLabel(
            left_col,
            text="📜 TIZIM VA HARAKATLAR JURNALI",
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            text_color="#94a3b8"
        )
        log_title.pack(anchor="w", padx=15, pady=(10, 2))

        self.txt_log = ctk.CTkTextbox(left_col, height=120, font=ctk.CTkFont(family="Consolas", size=11))
        self.txt_log.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        self.log_event("JARVIS tizimi ishga tushdi.")
        self.log_event("Xavfsizlik monitoringi faollashtirildi.")

        # RIGHT COLUMN: System Vitals & Quick Controls
        right_col = ctk.CTkFrame(main_frame, corner_radius=10, fg_color="#1a202c")
        right_col.grid(row=0, column=1, sticky="nsew")

        vitals_title = ctk.CTkLabel(
            right_col,
            text="⚡ TIZIM RESURSLARI",
            font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
            text_color="#94a3b8"
        )
        vitals_title.pack(anchor="w", padx=15, pady=(15, 10))

        # CPU bar
        self.lbl_cpu = ctk.CTkLabel(right_col, text="CPU: 0%", font=ctk.CTkFont(size=12))
        self.lbl_cpu.pack(anchor="w", padx=15)
        self.progress_cpu = ctk.CTkProgressBar(right_col, height=10)
        self.progress_cpu.pack(fill="x", padx=15, pady=(2, 10))
        self.progress_cpu.set(0)

        # RAM bar
        self.lbl_ram = ctk.CTkLabel(right_col, text="RAM: 0%", font=ctk.CTkFont(size=12))
        self.lbl_ram.pack(anchor="w", padx=15)
        self.progress_ram = ctk.CTkProgressBar(right_col, height=10)
        self.progress_ram.pack(fill="x", padx=15, pady=(2, 10))
        self.progress_ram.set(0)

        # Battery & Host Info
        self.lbl_info = ctk.CTkLabel(
            right_col,
            text="Batareya: Tekshirilmoqda...",
            font=ctk.CTkFont(size=12),
            text_color="#cbd5e1"
        )
        self.lbl_info.pack(anchor="w", padx=15, pady=(0, 15))

        # ACTION BUTTONS
        btn_title = ctk.CTkLabel(
            right_col,
            text="🎛️ TEZKOR BOSHQARUV",
            font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
            text_color="#94a3b8"
        )
        btn_title.pack(anchor="w", padx=15, pady=(5, 10))

        btn_snap = ctk.CTkButton(
            right_col,
            text="📸 Suratga olish",
            command=self.action_snapshot,
            fg_color="#0284c7",
            hover_color="#0369a1"
        )
        btn_snap.pack(fill="x", padx=15, pady=5)

        btn_lock = ctk.CTkButton(
            right_col,
            text="🔒 Ekranni qulflash",
            command=self.action_lock,
            fg_color="#e11d48",
            hover_color="#be123c"
        )
        btn_lock.pack(fill="x", padx=15, pady=5)

        btn_voice = ctk.CTkButton(
            right_col,
            text="🗣️ Ovozni sinash (Salom Jarvis)",
            command=self.action_test_voice,
            fg_color="#10b981",
            hover_color="#059669"
        )
        btn_voice.pack(fill="x", padx=15, pady=5)

        btn_agy = ctk.CTkButton(
            right_col,
            text="🤖 Antigravity muhandisni chaqirish",
            command=self.action_call_antigravity,
            fg_color="#8b5cf6",
            hover_color="#7c3aed"
        )
        btn_agy.pack(fill="x", padx=15, pady=5)

    def log_event(self, text: str):
        timestamp = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{timestamp}] {text}\n")
        self.txt_log.see("end")

    def action_snapshot(self):
        self.log_event("Kameradan surat olinmoqda...")
        if self.core and self.core.vision:
            path = self.core.vision.capture_snapshot()
            if path and os.path.exists(path):
                self.log_event(f"Surat saqlandi: {os.path.basename(path)}")
                self._display_image(path)
                return
        # Fallback direct
        self.log_event("Snapshot tizimi chaqirildi.")

    def _display_image(self, path: str):
        try:
            img = Image.open(path)
            img = img.resize((360, 240), Image.Resampling.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(360, 240))
            self.camera_label.configure(image=ctk_img, text="")
        except Exception as e:
            self.log_event(f"Rasmni ko'rsatishda xato: {e}")

    def action_lock(self):
        self.log_event("Kompyuter ekrani qulflanmoqda...")
        try:
            res = self.system.lock_workstation()
            if isinstance(res, dict) and not res.get("success", False):
                err = res.get("error") or res.get("message") or "Qulflashda xatolik"
                self.log_event(f"Xatolik: Ekranni qulflab bo'lmadi - {err}")
            else:
                self.log_event("Kompyuter ekrani muvaffaqiyatli qulflandi.")
        except Exception as e:
            self.log_event(f"Xatolik: Ekranni qulflashda istisno - {e}")

    def action_test_voice(self):
        self.log_event("Ovozli javob sinovdan o'tkazilmoqda...")
        msg = "Assalomu alaykum xo'jayin! Men Jarvisman, tizim mukammal holatda ishlamoqda."
        if self.core:
            self.core.speak(msg)
        else:
            threading.Thread(target=lambda: self.system.speak(msg), daemon=True).start()

    def action_call_antigravity(self):
        self.log_event("Antigravity muhandislik moduli chaqirilmoqda...")
        if self.core and self.core.antigravity:
            res = self.core.antigravity.delegate_task("Status check")
            self.log_event(f"Antigravity javobi: {res}")
        else:
            self.log_event("Antigravity tayyor holatda.")

    def _start_vitals_thread(self):
        def loop():
            while True:
                try:
                    stats = self.system.get_system_stats()
                    cpu = stats.get("cpu_percent", 0.0)
                    ram = stats.get("ram_percent", 0.0)
                    bat = stats.get("battery_percent", None)
                    plugged = stats.get("battery_plugged", False)

                    self.lbl_cpu.configure(text=f"CPU: {cpu}%")
                    self.progress_cpu.set(cpu / 100.0)

                    self.lbl_ram.configure(text=f"RAM: {ram}% ({stats.get('ram_used_gb', 0)}GB / {stats.get('ram_total_gb', 0)}GB)")
                    self.progress_ram.set(ram / 100.0)

                    bat_str = f"Batareya: {bat}%" if bat is not None else "Batareya: Statsionar (AC)"
                    if plugged and bat is not None:
                        bat_str += " [Quvvatlanmoqda]"
                    self.lbl_info.configure(text=f"{bat_str}\nXavfsizlik: {stats.get('security_status', 'Faol')}")
                except Exception as e:
                    try:
                        self.lbl_cpu.configure(text="CPU: N/A (Stale)")
                        self.lbl_ram.configure(text="RAM: N/A (Stale)")
                        self.lbl_info.configure(text=f"Telemetriya xatosi: {e}")
                    except Exception:
                        pass
                time.sleep(2)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

if __name__ == "__main__":
    app = JarvisHUD()
    app.mainloop()
