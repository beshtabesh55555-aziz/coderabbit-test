# JARVIS / Monika AI System

A modular, production-ready AI Assistant and Autonomous Desktop Companion powered by multimodal LLMs, Windows OS integration, and secure remote control capabilities.

## Architecture & Subsystems

1. **Core Intelligence Engine (`jarvis_core.py`)**
   - High-level orchestrator mediating user intent, multimodal reasoning (Gemini API & local offline LLMs), and tool execution.
   - Built-in Zero-Hallucination Gate ensuring real-world physical verification before reporting task completion.

2. **System Telemetry & Resource Monitor (`jarvis_system.py`)**
   - Asynchronous telemetry collecting CPU load, RAM utilization, Disk space, battery status, and uptime.
   - Power state management and background process health checks.

3. **Desktop Automation & Screen Controller (`jarvis_desktop.py`)**
   - Safe mouse and keyboard event automation using PyAutoGUI with failsafe protections.
   - Screen capture and multi-monitor geometric bounding.

4. **Configuration Manager (`config.py`)**
   - Centralized environment variable loader supporting localized voice, camera thresholds, and security credentials.

## Setup & Getting Started

```bash
# 1. Clone the repository
git clone https://github.com/beshtabesh55555-aziz/coderabbit-test.git
cd coderabbit-test

# 2. Configure environment
cp .env.example .env
# Edit .env with your Gemini API key and Telegram Bot token

# 3. Install dependencies
pip install -r requirements.txt
```

## Security & Privacy Notice
All sensitive tokens, personal communication logs, and private desktop captures are strictly excluded from version control via `.gitignore`.
