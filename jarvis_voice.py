"""
JARVIS Voice & Audio Module
===========================
High-fidelity Text-to-Speech (TTS) and Speech-to-Text (STT) interface.

Features:
- Primary TTS: Edge-TTS for ultra-realistic neural speech in Uzbek, English, Russian.
- Offline Fallback TTS: Windows SAPI (System.Speech.Synthesis) - zero network dependency.
- Speech Recognition: Microphone listener with noise calibration and multi-language support.
- Non-blocking audio playback via PowerShell SoundPlayer / Windows Media Player.
- Wake-word detection loop ("Jarvis", "Salom Jarvis", "Hey Jarvis").
"""

import os
import sys
import time
import queue
import logging
import asyncio
import threading
import subprocess
import collections
import numpy as np
from pathlib import Path
from typing import Optional, List, Callable, Any

# Load configuration
try:
    import config
    AUDIO_CACHE_DIR = config.AUDIO_CACHE_DIR
    VOICE_LANGUAGE = config.VOICE_LANGUAGE
    TTS_VOICE = config.TTS_VOICE
    WAKE_WORDS = config.WAKE_WORDS
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent
    AUDIO_CACHE_DIR = BASE_DIR / "audio_cache"
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_LANGUAGE = "uz"
    TTS_VOICE = "uz-UZ-SardorNeural"
    WAKE_WORDS = ["jarvis", "salom jarvis", "hey jarvis"]

logger = logging.getLogger("JarvisVoice")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s] [JARVIS-VOICE] %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

# Check optional libraries
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    edge_tts = None
    EDGE_TTS_AVAILABLE = False

try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except ImportError:
    sr = None
    SR_AVAILABLE = False

try:
    import sounddevice as sd
    SD_AVAILABLE = True
except ImportError:
    sd = None
    SD_AVAILABLE = False


class JarvisVoice:
    """Voice Engine providing speech synthesis, speech recognition, and wake-word listening."""

    # Curated high quality neural voices
    VOICE_PRESETS = {
        "uz_male": "uz-UZ-SardorNeural",
        "uz_female": "uz-UZ-MadinaNeural",
        "en_jarvis": "en-US-ChristopherNeural",
        "en_guy": "en-US-GuyNeural",
        "ru_male": "ru-RU-DmitryNeural",
        "ru_female": "ru-RU-SvetlanaNeural"
    }

    def __init__(
        self,
        language: str = VOICE_LANGUAGE,
        voice_name: str = TTS_VOICE,
        wake_words: Optional[List[str]] = None
    ):
        self.language = language
        self.voice_name = voice_name
        self.wake_words = [w.lower().strip() for w in (wake_words or WAKE_WORDS)]
        self._speech_queue: queue.Queue = queue.Queue()
        self._is_speaking = False
        self._last_speech_end_time = 0.0
        self._stop_listening = False
        self._active_player_process: Optional[subprocess.Popen] = None
        self._should_stop_speech = False
        self._ptt_active = False
        
        # Audio player thread
        self._player_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self._player_thread.start()

        # Initialize STT recognizer if available
        self.recognizer = sr.Recognizer() if SR_AVAILABLE else None
        if self.recognizer:
            self.recognizer.energy_threshold = 280
            self.recognizer.dynamic_energy_threshold = True

        logger.info(f"JarvisVoice ready. Voice='{self.voice_name}', EdgeTTS={EDGE_TTS_AVAILABLE}, STT={SR_AVAILABLE}")

    def stop_speaking(self):
        """Immediately interrupt and stop any ongoing speech playback."""
        logger.info("🛑 Speech playback interrupt requested.")
        self._should_stop_speech = True
        
        # 1. Clear queued speech and release waiting callers
        try:
            while not self._speech_queue.empty():
                item = self._speech_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and hasattr(item[1], "set"):
                    item[1].set()
                self._speech_queue.task_done()
        except Exception:
            pass

        # 2. Terminate active player process
        proc = self._active_player_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.kill()
            except Exception:
                pass
            self._active_player_process = None

        self._is_speaking = False

    def speak(self, text: str, wait: bool = False):
        """Queue text to be spoken via TTS."""
        if not text or not text.strip():
            return
        
        self._should_stop_speech = False
        clean_text = text.strip()
        if wait:
            done_event = threading.Event()
            self._speech_queue.put((clean_text, done_event))
            done_event.wait()
        else:
            self._speech_queue.put(clean_text)

    def _speech_worker(self):
        """Background worker consuming speech queue in FIFO order."""
        while True:
            try:
                item = self._speech_queue.get()
                if item is None:
                    break
                done_event = None
                if isinstance(item, tuple):
                    text, done_event = item
                else:
                    text = item
                try:
                    if not self._should_stop_speech:
                        self._is_speaking = True
                        self._speak_now(text)
                        self._last_speech_end_time = time.time()
                        self._is_speaking = False
                finally:
                    if done_event is not None:
                        done_event.set()
                    self._speech_queue.task_done()
            except Exception as e:
                logger.error(f"Speech worker error: {e}")
                self._last_speech_end_time = time.time()
                self._is_speaking = False

    def _clean_tts_text(self, text: str) -> str:
        """Clean and normalize text for graceful, natural spoken delivery in Uzbek."""
        import re
        t = text
        # Remove markdown bold/italics/headers/links
        t = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", t)
        t = re.sub(r"[*#_`~]", " ", t)
        t = re.sub(r"\[.*?\]", " ", t)
        t = re.sub(r"\(.*?\)", " ", t)
        # Normalize punctuation and symbols
        t = t.replace("%", " foiz ")
        t = t.replace("&", " va ")
        t = t.replace("+", " plyus ")
        t = t.replace("=", " teng ")
        t = t.replace('"', " ")
        t = t.replace("'", " ")
        t = t.replace("«", " ").replace("»", " ")
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _speak_now(self, text: str):
        """Synthesize and play speech with Edge-TTS or fallback to Windows SAPI."""
        if self._should_stop_speech:
            self._should_stop_speech = False
            return

        cleaned_text = self._clean_tts_text(text)
        if not cleaned_text:
            return

        self._is_speaking = True
        try:
            # 1. Try Edge-TTS (Ultra realistic neural voice with feminine pitch)
            if EDGE_TTS_AVAILABLE:
                try:
                    cache_file = AUDIO_CACHE_DIR / f"tts_{int(time.time() * 1000)}.mp3"
                    success = asyncio.run(self._generate_edge_tts(cleaned_text, str(cache_file)))
                    if success and cache_file.exists():
                        played = False
                        if not self._should_stop_speech:
                            played = self._play_audio_file(str(cache_file))
                        try:
                            cache_file.unlink()
                        except Exception:
                            pass
                        if played:
                            return
                except Exception as e:
                    logger.warning(f"Edge-TTS failed: {e}")

            # 2. Windows Native SAPI Fallback (Offline, zero dependency)
            if not self._should_stop_speech:
                self._speak_sapi(cleaned_text)
        finally:
            self._is_speaking = False

    async def _generate_edge_tts(self, text: str, output_path: str) -> bool:
        """Asynchronously call edge_tts to synthesize MP3 audio with gentle feminine pitch."""
        try:
            communicate = edge_tts.Communicate(text, self.voice_name, pitch="+2Hz")
            await communicate.save(output_path)
            return True
        except Exception as e:
            logger.warning(f"edge_tts generation error: {e}")
            return False

    def _play_audio_file(self, file_path: str) -> bool:
        """Play audio file with live interruption support."""
        if self._should_stop_speech:
            return False

        abs_path = str(Path(file_path).resolve())
        base_dir = Path(__file__).resolve().parent
        ps1_script = base_dir / "play_audio.ps1"
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1_script),
            "-FilePath",
            abs_path
        ]
        try:
            self._active_player_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            # Estimate reasonable max playback duration based on file size
            try:
                file_size = Path(file_path).stat().st_size
                max_timeout = max(30.0, (file_size / 4000.0) + 15.0)
            except Exception:
                max_timeout = 120.0

            start_t = time.time()
            interrupted = False
            while self._active_player_process and self._active_player_process.poll() is None:
                if self._should_stop_speech:
                    interrupted = True
                    try:
                        self._active_player_process.terminate()
                        self._active_player_process.kill()
                    except Exception:
                        pass
                    break
                time.sleep(0.06)
                if (time.time() - start_t) > max_timeout:
                    try:
                        self._active_player_process.kill()
                    except Exception:
                        pass
                    break
            success = (not interrupted and self._active_player_process is not None and self._active_player_process.poll() == 0)
            self._active_player_process = None
            return success
        except Exception as e:
            logger.warning(f"Audio playback error: {e}")
            self._active_player_process = None
            return False

    def _speak_sapi(self, text: str) -> bool:
        """Speak text through Windows SAPI using PowerShell with interruption support."""
        if self._should_stop_speech:
            return False
        safe_text = text.replace("'", "''").replace("\n", " ")
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{safe_text}')"
        ]
        try:
            self._active_player_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            while self._active_player_process and self._active_player_process.poll() is None:
                if self._should_stop_speech:
                    try:
                        self._active_player_process.terminate()
                        self._active_player_process.kill()
                    except Exception:
                        pass
                    break
                time.sleep(0.06)
            self._active_player_process = None
            return True
        except Exception as e:
            logger.error(f"SAPI TTS error: {e}")
            self._active_player_process = None
            return False

    def _record_audio(self, listen_timeout: float = 3.5, max_phrase_duration: float = 8.0, sample_rate: int = 16000) -> Optional[Any]:
        """
        Record audio from hardware microphone via sounddevice with Voice Activity Detection (VAD).
        - Listens in 100ms frames.
        - Preserves a 400ms pre-roll buffer to prevent cutting off the first syllable.
        - Triggers speech buffering when amplitude exceeds speech_threshold (280 normally, 450 while speaking).
        - Automatically terminates when user finishes speaking (800ms silence) or reaches max_phrase_duration.
        - Full natural utterance capture with live barge-in support.
        """
        # 1. Primary: sounddevice Streaming VAD
        if SD_AVAILABLE:
            block_size = int(sample_rate * 0.1)  # 100ms = 1600 samples
            pre_roll = collections.deque(maxlen=4)  # 400ms pre-speech buffer
            speech_frames = []
            is_speech = False
            speech_start_time = 0.0
            silence_count = 0
            max_silence_blocks = 8  # 800ms silence = end of utterance
            # While speaking, require louder voice (450) so speaker output isn't mistaken for user speech
            speech_threshold = 450 if self._is_speaking else 280
            start_wait = time.time()

            try:
                with sd.RawInputStream(samplerate=sample_rate, blocksize=block_size, dtype="int16", channels=1) as stream:
                    while not self._stop_listening:
                        # Wait timeout if user has not started speaking
                        if not is_speech and (time.time() - start_wait) > listen_timeout:
                            return None

                        # Max phrase duration timeout
                        if is_speech and (time.time() - speech_start_time) > max_phrase_duration:
                            break

                        data, overflow = stream.read(block_size)
                        audio_chunk = np.frombuffer(data, dtype=np.int16)
                        peak = int(np.abs(audio_chunk).max())

                        if not is_speech:
                            pre_roll.append(data)
                            if peak >= speech_threshold:
                                is_speech = True
                                speech_start_time = time.time()
                                speech_frames.extend(pre_roll)
                                silence_count = 0
                                logger.debug(f"VAD: Speech onset detected (peak={peak})")
                        else:
                            speech_frames.append(data)
                            if peak < speech_threshold:
                                silence_count += 1
                                if silence_count >= max_silence_blocks:
                                    logger.debug(f"VAD: Speech complete ({silence_count * 100}ms pause)")
                                    break
                            else:
                                silence_count = 0

                if speech_frames and len(speech_frames) >= 4:  # At least 400ms of audio
                    raw_bytes = b"".join(speech_frames)
                    return sr.AudioData(raw_bytes, sample_rate, 2)
                return None
            except Exception as e:
                logger.debug(f"sounddevice streaming VAD error: {e}")

        # 2. Fallback: sr.Microphone (PyAudio)
        if SR_AVAILABLE and self.recognizer:
            try:
                with sr.Microphone() as source:
                    return self.recognizer.listen(source, timeout=3, phrase_time_limit=6)
            except Exception as e:
                logger.debug(f"sr.Microphone fallback error: {e}")

        return None

    def listen(self, timeout: int = 4, phrase_time_limit: int = 8) -> Optional[str]:
        """
        Listen to the microphone and convert speech to text with complete natural utterance capture.
        Supports live barge-in: If user says 'to'xta' or 'stop' while assistant speaks, it immediately halts playback.
        """
        if not SR_AVAILABLE:
            return None

        audio = self._record_audio(listen_timeout=float(timeout), max_phrase_duration=float(phrase_time_limit), sample_rate=16000)
        if not audio:
            return None

        # 1. Barge-in check while assistant is speaking
        if self._is_speaking:
            try:
                text = self.recognizer.recognize_google(audio, language="uz-UZ")
                if text:
                    t_lower = text.lower()
                    if any(w in t_lower for w in ["to'xta", "toxta", "stop", "jim", "bas", "bo'ldi", "boldi", "gapirma"]):
                        logger.info(f"🛑 [BARGE-IN STOP TRIGGERED WHILE SPEAKING]: '{text}'")
                        self.stop_speaking()
                        return "to'xta"
            except Exception:
                pass
            return None

        # 2. Self-Echo Guard (Mark-LV style): Drop residual speaker acoustic feedback within 0.5s of speech completion
        if (time.time() - self._last_speech_end_time) < 0.5:
            logger.debug("🛡️ [SELF-ECHO GUARD]: Audio packet dropped to prevent echo interruption.")
            return None

        # 2. Standard recognition when assistant is NOT speaking
        lang_code = "uz-UZ" if self.language == "uz" else "ru-RU" if self.language == "ru" else "en-US"
        try:
            text = self.recognizer.recognize_google(audio, language=lang_code)
            logger.info(f"🎙️ [MIKROFON ESHITILDI] ({lang_code}): \"{text}\"")
            return text
        except sr.UnknownValueError:
            return None
        except Exception as e:
            try:
                text = self.recognizer.recognize_google(audio, language="en-US")
                logger.info(f"🎙️ [MIKROFON ESHITILDI] (en-US): \"{text}\"")
                return text
            except Exception:
                return None

    def start_wake_word_listener(self, on_wake_callback: Callable[[str], None]):
        """Runs continuous listening in a background thread and triggers callback on wake word."""
        def loop():
            logger.info(f"Wake-word listener started for: {self.wake_words}")
            while not self._stop_listening:
                try:
                    # Acoustic feedback filter: Pause listener if JARVIS is currently speaking
                    if self._is_speaking:
                        time.sleep(0.2)
                        continue

                    heard = self.listen(timeout=4, phrase_time_limit=6)
                    if heard:
                        h_lower = heard.lower()
                        for w in self.wake_words:
                            if w in h_lower:
                                logger.info(f"Wake word detected: '{w}' in '{heard}'")
                                on_wake_callback(heard)
                                break
                except Exception:
                    time.sleep(0.5)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    def play_activation_chime(self):
        """Play a futuristic sci-fi activation chime when Push-to-Talk or wake-word is triggered."""
        def _beep():
            try:
                import winsound
                winsound.Beep(987, 80)
                winsound.Beep(1318, 120)
            except Exception:
                pass
        threading.Thread(target=_beep, daemon=True).start()

    def play_instant_acknowledgment(self, phrase: Optional[str] = None):
        """Instant acknowledgment (Mark-LV style): speaks a quick filler or plays a working chime."""
        fillers = ["Bir soniya...", "Xo'p bo'ladi...", "Bajarmoqdaman..."]
        chosen = phrase or fillers[int(time.time()) % len(fillers)]
        self.speak(chosen, wait=False)

    def start_push_to_talk_listener(self, on_ptt_callback: Callable[[], None], hotkey_check_interval: float = 0.05):
        """
        Global Push-to-Talk (PTT) Hotkey Monitor (Mark-LV style).
        Monitors Ctrl + Space globally across Windows using native user32.GetAsyncKeyState.
        Does not require administrative privileges or external hooks.
        """
        def _ptt_loop():
            import ctypes
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_SPACE = 0x20
            debounce_until = 0.0
            logger.info("⚡ Push-to-Talk (PTT) Global Hotkey Monitor active: [Ctrl + Space]")

            while not self._stop_listening:
                try:
                    now = time.time()
                    ctrl_pressed = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                    space_pressed = bool(user32.GetAsyncKeyState(VK_SPACE) & 0x8000)

                    if ctrl_pressed and space_pressed:
                        if now > debounce_until:
                            debounce_until = now + 1.2  # 1.2s debounce
                            logger.info("🎙️ [PUSH-TO-TALK TRIGGERED] via Ctrl + Space!")
                            self.play_activation_chime()
                            try:
                                on_ptt_callback()
                            except Exception as cb_err:
                                logger.error(f"Error in PTT callback: {cb_err}")
                    time.sleep(hotkey_check_interval)
                except Exception as e:
                    time.sleep(0.5)

        t = threading.Thread(target=_ptt_loop, daemon=True, name="JARVIS-PTT-Monitor")
        t.start()
        return t

    def stop(self):
        self._stop_listening = True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JARVIS Voice Module Test")
    parser.add_argument("--speak", type=str, help="Text to speak")
    parser.add_argument("--listen", action="store_true", help="Listen from microphone")
    args = parser.parse_args()

    voice = JarvisVoice()
    if args.speak:
        print(f"Speaking: {args.speak}")
        voice.speak(args.speak, wait=True)
    elif args.listen:
        print("Listening from microphone... Speak now:")
        res = voice.listen(timeout=7)
        print("Result:", res)
    else:
        test_msg = "Assalomu alaykum xo'jayin! Jarvis ovozli tizimi to'liq sozlandi."
        print(f"Testing TTS: {test_msg}")
        voice.speak(test_msg, wait=True)
