"""
JARVIS Central Configuration Module
Loads settings from environment variables or .env file with safe defaults.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Base paths
BASE_DIR = Path(__file__).resolve().parent
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
AUDIO_CACHE_DIR = BASE_DIR / "audio_cache"

# Ensure directories exist
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Load .env file
load_dotenv(BASE_DIR / ".env")

# Assistant Identity
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Monica")

# AI Settings
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Telegram Settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# Security & WebApp Settings
WEBAPP_API_KEY = os.getenv("WEBAPP_API_KEY", "monika-secret-key-2026")
ADMIN_PIN = os.getenv("ADMIN_PIN", "1234")

# Vision Settings
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
MOTION_SENSITIVITY = int(os.getenv("MOTION_DETECTION_SENSITIVITY", "25"))
MOTION_MIN_AREA = int(os.getenv("MOTION_MIN_AREA", "4000"))
MOTION_COOLDOWN = int(os.getenv("MOTION_ALERT_COOLDOWN_SECONDS", "20"))

# Voice Settings
VOICE_LANGUAGE = os.getenv("VOICE_LANGUAGE", "uz")
TTS_VOICE = os.getenv("TTS_VOICE", "uz-UZ-MadinaNeural")
_wake_words_raw = os.getenv("WAKE_WORDS", "monika,monica,monikaxon,madina,salom monika,hey monika,jarvis")
WAKE_WORDS = [w.strip().lower() for w in _wake_words_raw.split(",") if w.strip()]
GEMINI_MODEL = "models/gemini-3.5-flash"
GEMINI_FALLBACK_MODELS = [
    "models/gemini-3.5-flash",
    "models/gemini-3.5-flash-lite",
    "models/gemini-3.1-flash-lite",
    "models/gemini-flash-lite-latest",
]

def is_gemini_configured() -> bool:
    return bool(GEMINI_API_KEY and GEMINI_API_KEY.strip())

def is_telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID)
