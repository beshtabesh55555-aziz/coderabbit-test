"""
JARVIS Vision Module
====================
Production-grade computer vision pipeline for JARVIS AI.

Key Features:
- DirectShow multi-frame warm-up and explicit handle release in finally blocks
- OpenCV webcam capture with MSMF hardware transform bypass
- Real-time motion detection (BGR MOG2 with shadow discrimination + morphological opening)
- Safe frame differencing fallback (replaces static thresholding)
- Edge-triggered security alerts with 2.0s exit hysteresis
- Strict simulation mode protection: NEVER send security alerts in simulation mode
- Automatic snapshot pruning retaining a maximum of 150 snapshots
- Thread-safe operations with re-entrant RLock
- Background thread streaming & asynchronous frame generation
- Gemini Multimodal Vision API integration for intelligent room scene description
"""

import os
# Set MSMF hardware transforms to 0 before importing cv2
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import io
import sys
import time
import math
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Callable, Dict, Any, Generator

# Import local JARVIS config if available
try:
    from config import (
        SNAPSHOTS_DIR,
        CAMERA_INDEX,
        MOTION_SENSITIVITY,
        MOTION_MIN_AREA,
        MOTION_COOLDOWN,
        GEMINI_API_KEY,
        is_gemini_configured,
    )
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent
    SNAPSHOTS_DIR = BASE_DIR / "snapshots"
    CAMERA_INDEX = 0
    MOTION_SENSITIVITY = 25
    MOTION_MIN_AREA = 4000
    MOTION_COOLDOWN = 10
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

    def is_gemini_configured() -> bool:
        return bool(GEMINI_API_KEY and GEMINI_API_KEY.strip())

# Optional cv2 and numpy import
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    CV2_AVAILABLE = False

# Optional PIL import
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    PIL_AVAILABLE = False

# Configure logger
logger = logging.getLogger("JarvisVision")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [JARVIS-VISION] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class SimulatedFrame:
    """Fallback frame container when OpenCV/NumPy are not yet installed."""

    def __init__(self, pil_image: Any):
        self._image = pil_image
        self.width = pil_image.width if pil_image else 640
        self.height = pil_image.height if pil_image else 480
        self.shape = (self.height, self.width, 3)
        self.size = self.height * self.width * 3

    def copy(self):
        if self._image is not None:
            return SimulatedFrame(self._image.copy())
        return SimulatedFrame(None)

    def to_bytes(self) -> bytes:
        if self._image is not None:
            buf = io.BytesIO()
            self._image.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
        return b""

    def save(self, filepath: Path) -> bool:
        if self._image is not None:
            self._image.save(str(filepath), format="JPEG", quality=90)
            return True
        return False


class JarvisVision:
    """
    JARVIS Vision Subsystem.
    Provides camera capture, motion detection, snapshots, and AI scene understanding.
    Thread-safe and hardened for production reliability.
    """

    def __init__(
        self,
        camera_index: int = CAMERA_INDEX,
        sensitivity: int = MOTION_SENSITIVITY,
        min_area: int = MOTION_MIN_AREA,
        cooldown: int = MOTION_COOLDOWN,
        snapshots_dir: Optional[Path] = None,
        force_simulation: bool = False,
    ):
        """
        Initialize the JARVIS Vision subsystem.

        :param camera_index: Primary webcam index (default 0)
        :param sensitivity: MOG2 threshold / delta sensitivity (1-100)
        :param min_area: Minimum contour pixel area to qualify as motion
        :param cooldown: Minimum seconds between motion alert triggers
        :param snapshots_dir: Target folder for saving snapshots
        :param force_simulation: Force simulation mode even if camera exists
        """
        self.camera_index = camera_index
        self.sensitivity = max(5, min(100, sensitivity))
        self.min_area = max(500, min_area)
        self.cooldown = max(1, cooldown)
        self.force_simulation = force_simulation

        # Directory setup
        self.snapshots_dir = Path(snapshots_dir or SNAPSHOTS_DIR)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # VideoCapture handle
        self.cap = None
        self.is_simulation_mode = force_simulation or not CV2_AVAILABLE
        self._last_frame_is_fallback = False
        self._motion_detector_initialized = False
        self.actual_camera_index = -1

        # Background subtractor & safe fallback frame buffer
        self.bg_subtractor = None
        self._prev_gray: Optional[Any] = None

        # State Machine: Motion detection & 2.0s exit hysteresis
        self.exit_hysteresis: float = 2.0
        self.last_motion_seen_time: Optional[float] = None
        self.last_motion_alert_time: float = 0.0
        self.is_motion_active: bool = False
        self.motion_start_time: Optional[float] = None
        self.motion_end_time: Optional[float] = None
        self.presence_duration: float = 0.0

        # Frame dimensions and internal cache
        self.frame_width = 640
        self.frame_height = 480
        self.last_frame = None
        self.last_annotated_frame = None

        # Re-entrant lock for thread safety
        self.lock = threading.RLock()

        # Background worker thread state
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.is_monitoring: bool = False

        # Callbacks
        self.on_motion_detected: Optional[Callable[[Any, str, List[Tuple[int, int, int, int]]], None]] = None
        self.on_motion_stopped: Optional[Callable[[float], None]] = None
        self.on_person_present: Optional[Callable[[Any], None]] = None

        # Simulation animation states
        self._sim_tick = 0
        self._sim_motion_cycle = 0

        # Initialize hardware or simulation
        self._init_camera()
        self._init_motion_detector()

    def _init_camera(self) -> bool:
        """
        Try opening the primary camera, then fallback indices and DirectShow.
        Performs multi-frame warm-up reads and ensures explicit handle release in finally blocks.
        If unavailable, gracefully switches to simulation mode.
        """
        if self.force_simulation or not CV2_AVAILABLE:
            self.is_simulation_mode = True
            logger.warning("Camera operating in SIMULATION mode (cv2 not available or force_simulation=True).")
            return False

        indices_to_try = [self.camera_index]
        for alt_idx in [0, 1, 2]:
            if alt_idx not in indices_to_try:
                indices_to_try.append(alt_idx)

        # Try DirectShow first on Windows, standard fallback next
        backends = []
        if sys.platform.startswith("win"):
            backends = [cv2.CAP_DSHOW, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]

        for idx in indices_to_try:
            for backend in backends:
                cap = None
                try:
                    cap = cv2.VideoCapture(idx, backend)
                    if cap.isOpened():
                        # DirectShow multi-frame warm-up: discard initial unstable/dark frames
                        warmup_ok = False
                        test_frame = None
                        for _ in range(5):
                            ret, frame_read = cap.read()
                            if ret and frame_read is not None and frame_read.size > 0:
                                warmup_ok = True
                                test_frame = frame_read
                            time.sleep(0.02)

                        if warmup_ok and test_frame is not None:
                            self.cap = cap
                            self.actual_camera_index = idx
                            self.is_simulation_mode = False
                            # Optimize resolution for low CPU overhead
                            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                            self.cap.set(cv2.CAP_PROP_FPS, 30)
                            self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
                            self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
                            logger.info(
                                f"Webcam connected successfully on index {idx} with backend {backend} "
                                f"({self.frame_width}x{self.frame_height} @ 30fps)"
                            )
                            return True
                except Exception as ex:
                    logger.debug(f"Failed opening camera index {idx} with backend {backend}: {ex}")
                finally:
                    # Explicit handle release if this capture handle was not retained
                    if self.cap is not cap and cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass

        # Fallback to simulation
        self.is_simulation_mode = True
        logger.warning(
            f"Physical camera could not be opened (indices {indices_to_try} busy/unavailable). "
            "Switching to SIMULATION mode safely."
        )
        return False

    def _init_motion_detector(self) -> None:
        """Initialize the BGR MOG2 background subtractor with shadow discrimination."""
        self._motion_detector_initialized = True
        if CV2_AVAILABLE:
            try:
                self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
                    history=500,
                    varThreshold=float(self.sensitivity),
                    detectShadows=True,
                )
            except Exception as e:
                logger.warning(f"Failed to create MOG2 subtractor: {e}")
                self.bg_subtractor = None

    def read_frame(self) -> Tuple[bool, Optional[Any], bool]:
        """
        Read the next frame from the camera or generate a synthetic frame.
        Returns (success, frame, is_fallback).
        Thread-safe using re-entrant RLock.
        """
        with self.lock:
            if not self.is_simulation_mode and self.cap is not None and self.cap.isOpened():
                try:
                    ret, frame = self.cap.read()
                    if ret and frame is not None and frame.size > 0:
                        self.last_frame = frame.copy()
                        return True, frame, False
                    else:
                        logger.warning("Camera read failed, returning fallback simulation frame.")
                except Exception as e:
                    logger.error(f"Error reading from camera: {e}")

            # Simulation fallback
            sim_frame = self._generate_simulation_frame()
            if sim_frame is not None:
                self.last_frame = sim_frame.copy()
                return True, sim_frame, True
            return False, None, True

    def _generate_simulation_frame(self) -> Any:
        """
        Generate a synthetic test room frame for simulation mode.
        Supports both OpenCV (NumPy) and Pillow (PIL) backends.
        """
        self._sim_tick += 1
        width, height = self.frame_width, self.frame_height
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Cycle motion simulation
        self._sim_motion_cycle = (self._sim_motion_cycle + 1) % 180
        person_present = self._sim_motion_cycle > 60
        progress = (self._sim_motion_cycle - 60) / 120.0 if person_present else 0.0
        x_pos = int(60 + progress * (width - 120))
        y_pos = int(height * 0.45 + math.sin(progress * math.pi * 4) * 8)

        # Branch 1: OpenCV + NumPy
        if CV2_AVAILABLE and np is not None:
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            # Wall & Floor
            frame[0 : int(height * 0.7), :] = (40, 35, 30)
            frame[int(height * 0.7) :, :] = (25, 25, 25)
            # Perspective lines
            cv2.line(frame, (0, int(height * 0.7)), (width, int(height * 0.7)), (60, 60, 60), 2)
            cv2.line(frame, (int(width * 0.2), int(height * 0.7)), (0, height), (50, 50, 50), 1)
            cv2.line(frame, (int(width * 0.8), int(height * 0.7)), (width, height), (50, 50, 50), 1)
            # Desk & Computer
            cv2.rectangle(frame, (100, int(height * 0.65)), (width - 100, int(height * 0.78)), (55, 45, 40), -1)
            cv2.rectangle(frame, (int(width * 0.42), int(height * 0.52)), (int(width * 0.58), int(height * 0.65)), (70, 70, 70), -1)

            if person_present:
                cv2.circle(frame, (x_pos, y_pos - 45), 22, (90, 110, 140), -1)
                cv2.ellipse(frame, (x_pos, y_pos + 15), (30, 45), 0, 0, 360, (75, 85, 115), -1)
                cv2.putText(frame, "SIMULATED PERSON", (x_pos - 60, y_pos - 75),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (120, 200, 255), 1)

            cv2.putText(frame, f"[JARVIS VISION SIMULATION]  {timestamp_str}", (15, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 255), 2)
            cv2.putText(frame, "STATUS: ACTIVE MONITORING (SAFE SIMULATION)", (15, height - 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 120), 1)
            return frame

        # Branch 2: Pillow (PIL) fallback
        if PIL_AVAILABLE and Image is not None:
            img = Image.new("RGB", (width, height), color=(30, 35, 40))
            draw = ImageDraw.Draw(img)
            # Wall & Floor
            draw.rectangle([(0, 0), (width, int(height * 0.7))], fill=(40, 35, 30))
            draw.rectangle([(0, int(height * 0.7)), (width, height)], fill=(25, 25, 25))
            draw.line([(0, int(height * 0.7)), (width, int(height * 0.7))], fill=(60, 60, 60), width=2)
            # Desk
            draw.rectangle([(100, int(height * 0.65)), (width - 100, int(height * 0.78))], fill=(55, 45, 40))

            if person_present:
                draw.ellipse([(x_pos - 22, y_pos - 67), (x_pos + 22, y_pos - 23)], fill=(140, 110, 90))
                draw.ellipse([(x_pos - 30, y_pos - 30), (x_pos + 30, y_pos + 60)], fill=(115, 85, 75))
                draw.text((x_pos - 50, y_pos - 85), "SIMULATED PERSON", fill=(255, 200, 120))

            draw.text((15, 15), f"[JARVIS VISION SIMULATION] {timestamp_str}", fill=(0, 220, 255))
            draw.text((15, height - 25), "STATUS: ACTIVE MONITORING (SAFE SIMULATION)", fill=(0, 255, 120))
            return SimulatedFrame(img)

        # Fallback dummy frame
        return None

    def process_motion(
        self,
        frame: Any,
        is_fallback: bool = False,
    ) -> Tuple[bool, List[Tuple[int, int, int, int]], Any]:
        """
        Analyze a frame for motion using BGR MOG2 background subtraction with shadow discrimination
        and morphological opening, or safe frame differencing fallback.

        Implements Edge-Triggered Alerting with 2.0s exit hysteresis.
        Strictly suppresses security alerts (on_motion_detected) in simulation mode or fallback frames.

        :param frame: Image frame (NumPy ndarray or SimulatedFrame)
        :param is_fallback: True if this frame is a synthetic simulation fallback frame
        :return: (is_motion_active, bounding_boxes, annotated_frame)
        """
        if frame is None:
            return False, [], None

        now = time.time()
        bounding_boxes = []
        raw_motion = False
        annotated_frame = frame.copy() if hasattr(frame, "copy") else frame

        # Check if running real OpenCV pipeline on a physical camera frame
        if not self.is_simulation_mode and not is_fallback and CV2_AVAILABLE and np is not None and isinstance(frame, np.ndarray):
            h, w = frame.shape[:2]
            proc_w = 640
            proc_h = int(h * (proc_w / w)) if w > 0 else 480
            small_frame = cv2.resize(frame, (proc_w, proc_h), interpolation=cv2.INTER_LINEAR)

            if not self._motion_detector_initialized and self.bg_subtractor is None:
                self._init_motion_detector()

            if self.bg_subtractor is not None:
                # BGR MOG2 directly with Gaussian blur to smooth sensor noise
                blurred_bgr = cv2.GaussianBlur(small_frame, (5, 5), 0)
                fg_mask = self.bg_subtractor.apply(blurred_bgr)
                # Shadow discrimination: MOG2 assigns 127 to detected shadows and 255 to true motion.
                # Thresholding at 250 removes shadows (127) and keeps only foreground motion (255).
                _, thresh = cv2.threshold(fg_mask, 250, 255, cv2.THRESH_BINARY)
            else:
                # Safe frame differencing fallback (completely replaces static gray > 50)
                gray = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)
                blurred_gray = cv2.GaussianBlur(gray, (21, 21), 0)
                if self._prev_gray is None or self._prev_gray.shape != blurred_gray.shape:
                    self._prev_gray = blurred_gray.copy()
                    thresh = np.zeros_like(blurred_gray)
                else:
                    frame_diff = cv2.absdiff(self._prev_gray, blurred_gray)
                    _, thresh = cv2.threshold(frame_diff, float(self.sensitivity), 255, cv2.THRESH_BINARY)
                    self._prev_gray = blurred_gray.copy()

            # Morphological opening (removes small speckle noise) followed by dilation
            kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            opened = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel_open, iterations=1)
            dilated = cv2.dilate(opened, kernel_dilate, iterations=2)
            contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            scale_x = w / proc_w
            scale_y = h / proc_h

            for c in contours:
                area = cv2.contourArea(c) * (scale_x * scale_y)
                if area >= self.min_area:
                    raw_motion = True
                    bx, by, bw, bh = cv2.boundingRect(c)
                    orig_x = int(bx * scale_x)
                    orig_y = int(by * scale_y)
                    orig_w = int(bw * scale_x)
                    orig_h = int(bh * scale_y)
                    bounding_boxes.append((orig_x, orig_y, orig_w, orig_h))

                    cv2.rectangle(
                        annotated_frame,
                        (orig_x, orig_y),
                        (orig_x + orig_w, orig_y + orig_h),
                        (0, 0, 255),
                        2,
                    )
                    cv2.putText(
                        annotated_frame,
                        f"MOTION ({int(area)}px)",
                        (orig_x, max(orig_y - 10, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 0, 255),
                        2,
                    )

            status_color = (0, 0, 255) if raw_motion else (0, 255, 0)
            status_text = "ALERT: MOTION DETECTED" if raw_motion else "SURVEILLANCE: NORMAL"
            cv2.putText(
                annotated_frame,
                f"JARVIS VISION - {status_text}",
                (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                status_color,
                2,
            )
        else:
            # Simulated motion analysis
            raw_motion = self._sim_motion_cycle > 60
            if raw_motion:
                progress = (self._sim_motion_cycle - 60) / 120.0
                x_pos = int(60 + progress * (self.frame_width - 120))
                y_pos = int(self.frame_height * 0.45)
                bounding_boxes.append((x_pos - 40, y_pos - 60, 80, 140))

        # ---------------------------------------------------------------------
        # Edge-Triggered Motion State Machine with 2.0s Exit Hysteresis
        # ---------------------------------------------------------------------
        if raw_motion:
            self.last_motion_seen_time = now

            if not self.is_motion_active:
                # Rising edge: Inactive -> Active
                self.is_motion_active = True
                self.motion_start_time = now
                logger.info("Motion started: Target entered camera field of view (rising edge).")

                # Strictly suppress security callbacks (on_motion_detected) in simulation mode or fallback frames!
                if not self.is_simulation_mode and not is_fallback:
                    if (now - self.last_motion_alert_time) >= self.cooldown:
                        self.last_motion_alert_time = now
                        logger.info(f"Triggering motion alert (rising edge, cooldown {self.cooldown}s satisfied).")
                        snapshot_path = self.capture_snapshot(annotated=True, frame=annotated_frame)
                        if self.on_motion_detected:
                            try:
                                self.on_motion_detected(annotated_frame, str(snapshot_path), bounding_boxes)
                            except Exception as e:
                                logger.error(f"Error in on_motion_detected callback: {e}")
                else:
                    logger.debug("Motion detected in SIMULATION/fallback mode: security alert strictly suppressed.")
            else:
                # Sustained motion: check cooldown for recurring alert
                if not self.is_simulation_mode and not is_fallback:
                    if (now - self.last_motion_alert_time) >= self.cooldown:
                        self.last_motion_alert_time = now
                        logger.info(f"Triggering recurring motion alert (cooldown {self.cooldown}s satisfied).")
                        snapshot_path = self.capture_snapshot(annotated=True, frame=annotated_frame)
                        if self.on_motion_detected:
                            try:
                                self.on_motion_detected(annotated_frame, str(snapshot_path), bounding_boxes)
                            except Exception as e:
                                logger.error(f"Error in on_motion_detected callback: {e}")

            self.presence_duration = now - (self.motion_start_time or now)

            if self.on_person_present:
                try:
                    self.on_person_present(annotated_frame)
                except Exception as e:
                    logger.error(f"Error in on_person_present callback: {e}")

        else:
            # No raw motion detected in current frame
            if self.is_motion_active:
                time_since_motion = now - (self.last_motion_seen_time or now)
                if time_since_motion >= self.exit_hysteresis:
                    # 2.0s Exit hysteresis period satisfied with no motion -> transition to Inactive
                    self.is_motion_active = False
                    self.motion_end_time = now
                    duration = self.motion_end_time - (self.motion_start_time or self.motion_end_time)
                    logger.info(
                        f"Motion ended: Target left field of view (hysteresis {self.exit_hysteresis}s satisfied). "
                        f"Active duration: {duration:.1f}s"
                    )
                    if self.on_motion_stopped:
                        try:
                            self.on_motion_stopped(duration)
                        except Exception as e:
                            logger.error(f"Error in on_motion_stopped callback: {e}")
                else:
                    # Within hysteresis window: retain active state to prevent bounce
                    self.presence_duration = now - (self.motion_start_time or now)

        with self.lock:
            self.last_annotated_frame = annotated_frame.copy() if hasattr(annotated_frame, "copy") else annotated_frame

        return self.is_motion_active, bounding_boxes, annotated_frame

    def _prune_snapshots(self, max_retained: int = 150) -> int:
        """
        Prune oldest snapshot files in snapshots_dir if total count exceeds max_retained.
        Retains at most max_retained files.
        Returns number of deleted files.
        """
        try:
            if not self.snapshots_dir.exists():
                return 0

            image_files = [
                f for f in self.snapshots_dir.iterdir()
                if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png"}
            ]
            if len(image_files) <= max_retained:
                return 0

            # Sort ascending by modification time (oldest first)
            image_files.sort(key=lambda p: p.stat().st_mtime)
            to_remove = len(image_files) - max_retained
            deleted_count = 0

            for f in image_files[:to_remove]:
                try:
                    f.unlink(missing_ok=True)
                    deleted_count += 1
                except Exception as ex:
                    logger.debug(f"Snapshot pruning failed for {f.name}: {ex}")

            if deleted_count > 0:
                logger.info(f"Pruned {deleted_count} old snapshots. Retained {max_retained} images.")
            return deleted_count
        except Exception as e:
            logger.error(f"Snapshot pruning error: {e}")
            return 0

    def capture_snapshot(
        self,
        filepath: Optional[str] = None,
        annotated: bool = False,
        return_bytes: bool = False,
        frame: Optional[Any] = None,
    ) -> Optional[Any]:
        """
        Capture a clean or annotated snapshot and save to disk.
        Automatically prunes old snapshots to maintain at most 150 snapshots.

        :param filepath: Optional explicit file path. If None, saves timestamped file to snapshots_dir.
        :param annotated: If True, saves annotated frame with detection boxes; else raw frame.
        :param return_bytes: If True, also returns the encoded image bytes (JPEG format).
        :param frame: Optional explicit frame to save.
        :return: Path to saved file, or (filepath, bytes) if return_bytes=True.
        """
        with self.lock:
            if frame is not None:
                frame_to_save = frame
            elif annotated and self.last_annotated_frame is not None:
                frame_to_save = self.last_annotated_frame
            elif self.last_frame is not None:
                frame_to_save = self.last_frame
            else:
                ret, frame_read, _ = self.read_frame()
                frame_to_save = frame_read if ret else None

        if frame_to_save is None:
            logger.error("Cannot capture snapshot: No frame available.")
            return None

        # Determine target file path
        if filepath is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
            prefix = "alert" if annotated else "snapshot"
            filename = f"{prefix}_{ts}.jpg"
            target_path = self.snapshots_dir / filename
        else:
            target_path = Path(filepath)
            target_path.parent.mkdir(parents=True, exist_ok=True)

        saved = False
        img_bytes = b""

        # Case 1: Real OpenCV NumPy frame
        if CV2_AVAILABLE and np is not None and isinstance(frame_to_save, np.ndarray):
            try:
                cv2.imwrite(str(target_path), frame_to_save, [cv2.IMWRITE_JPEG_QUALITY, 92])
                saved = True
                if return_bytes:
                    ret, enc = cv2.imencode(".jpg", frame_to_save)
                    if ret:
                        img_bytes = enc.tobytes()
            except Exception as e:
                logger.error(f"Failed to write snapshot image via cv2: {e}")

        # Case 2: SimulatedFrame (Pillow)
        elif isinstance(frame_to_save, SimulatedFrame):
            try:
                saved = frame_to_save.save(target_path)
                if return_bytes:
                    img_bytes = frame_to_save.to_bytes()
            except Exception as e:
                logger.error(f"Failed to save SimulatedFrame: {e}")

        # Case 3: PIL Image directly
        elif PIL_AVAILABLE and Image is not None and isinstance(frame_to_save, Image.Image):
            try:
                frame_to_save.save(str(target_path), format="JPEG", quality=90)
                saved = True
                if return_bytes:
                    buf = io.BytesIO()
                    frame_to_save.save(buf, format="JPEG", quality=90)
                    img_bytes = buf.getvalue()
            except Exception as e:
                logger.error(f"Failed to save PIL Image snapshot: {e}")

        if not saved:
            logger.error(f"Failed to encode or write valid snapshot to {target_path}")
            return (None, b"") if return_bytes else None

        logger.info(f"Snapshot saved: {target_path}")
        # Prune snapshot directory to retain max 150 files
        self._prune_snapshots(max_retained=150)

        if return_bytes:
            return str(target_path), img_bytes
        return str(target_path)

    def describe_current_view(
        self,
        prompt: str = "Xonada kim bor va nima qilyapti?",
        frame: Optional[Any] = None,
    ) -> str:
        """
        Use Google Gemini Multimodal Vision API to describe what is currently visible in front of the camera.

        :param prompt: Question or instruction for Gemini Vision. Default: Uzbek room analysis.
        :param frame: Optional BGR frame to analyze. If None, captures the latest frame.
        :return: AI text description of the scene.
        """
        api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            msg = (
                "[JARVIS Vision] Gemini API kaliti sozlanmagan. "
                ".env faylida GEMINI_API_KEY ni o'rnating yoki export qiling."
            )
            logger.warning(msg)
            return msg

        # Save snapshot for upload
        snap_path = self.capture_snapshot(
            filepath=str(self.snapshots_dir / f"gemini_query_{int(time.time())}.jpg"),
            frame=frame
        )
        if not snap_path or not Path(snap_path).exists():
            return "[JARVIS Vision] Tasvir saqlab bo'lmadi."

        logger.info(f"Sending visual frame to Gemini Vision (Prompt: '{prompt}')...")

        # Attempt 1: Official google-genai SDK
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            with open(snap_path, "rb") as f:
                image_bytes = f.read()

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
            )
            answer = response.text.strip()
            logger.info(f"Gemini Vision response received ({len(answer)} chars).")
            try:
                Path(snap_path).unlink(missing_ok=True)
            except Exception:
                pass
            return answer
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"google.genai call failed, trying google.generativeai fallback: {e}")

        # Attempt 2: google.generativeai fallback
        try:
            import google.generativeai as legacy_genai
            from PIL import Image

            legacy_genai.configure(api_key=api_key)
            pil_img = Image.open(snap_path)

            model = legacy_genai.GenerativeModel("models/gemini-2.5-flash")
            response = model.generate_content([prompt, pil_img], request_options={"timeout": 6})
            answer = response.text.strip()
            logger.info(f"Gemini Vision (legacy SDK) response received: {answer[:80]}...")
            try:
                Path(snap_path).unlink(missing_ok=True)
            except Exception:
                pass
            return answer
        except Exception as e:
            err_msg = f"[JARVIS Vision] Gemini Vision tahlilida xatolik: {str(e)}"
            logger.error(err_msg)
            return err_msg
        finally:
            try:
                if snap_path and Path(snap_path).exists():
                    Path(snap_path).unlink(missing_ok=True)
            except Exception:
                pass

    def stream_frames(self) -> Generator[Tuple[Any, bool, List[Tuple[int, int, int, int]]], None, None]:
        """
        Asynchronous generator yielding processed frames continuously.
        Yields (annotated_frame, motion_detected, bounding_boxes).
        """
        while not self._stop_event.is_set():
            ret, frame, is_fallback = self.read_frame()
            if not ret or frame is None:
                time.sleep(0.05)
                continue

            motion_detected, boxes, annotated = self.process_motion(frame, is_fallback=is_fallback)
            yield annotated, motion_detected, boxes
            time.sleep(0.04)

    def start_stream(
        self,
        callback: Optional[Callable[[Any, bool, List[Tuple[int, int, int, int]]], None]] = None,
    ) -> threading.Thread:
        """
        Start camera capture and motion detection loop in a background daemon thread.

        :param callback: Function invoked on every processed frame: callback(annotated_frame, motion_detected, boxes)
        :return: Monitoring thread object
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            if not self._stop_event.is_set():
                logger.warning("Monitoring stream already running.")
                return self._monitor_thread
            self._monitor_thread.join(timeout=2.0)

        self._stop_event.clear()
        self.is_monitoring = True

        def _worker_loop():
            logger.info("Vision background monitoring thread started (optimized 8 FPS with IPC buffer).")
            fps_target = 8
            interval = 1.0 / fps_target
            last_buffer_save = 0.0

            while not self._stop_event.is_set():
                t0 = time.time()
                try:
                    ret, frame, is_fallback = self.read_frame()
                    if ret and frame is not None:
                        motion_detected, boxes, annotated = self.process_motion(frame, is_fallback=is_fallback)
                        if callback:
                            try:
                                callback(annotated, motion_detected, boxes)
                            except Exception as ex:
                                logger.error(f"Error in stream callback: {ex}")

                        # IPC Shared Buffer for WebApp on-demand viewing (every 1.0s)
                        now = time.time()
                        if now - last_buffer_save >= 1.0:
                            last_buffer_save = now
                            try:
                                buffer_path = self.snapshots_dir / "live_webcam_buffer.jpg"
                                temp_path = self.snapshots_dir / "live_webcam_buffer.tmp.jpg"
                                cv2.imwrite(str(temp_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                                if temp_path.exists():
                                    temp_path.replace(buffer_path)
                            except Exception:
                                pass
                except Exception as err:
                    logger.error(f"Unexpected error in vision worker loop: {err}")

                elapsed = time.time() - t0
                sleep_time = max(0.01, interval - elapsed)
                time.sleep(sleep_time)

            logger.info("Vision background monitoring thread stopped cleanly.")
            self.is_monitoring = False

        self._monitor_thread = threading.Thread(target=_worker_loop, name="JARVIS-Vision-Worker", daemon=True)
        self._monitor_thread.start()
        return self._monitor_thread

    def stop_stream(self, timeout: float = 2.0) -> None:
        """Stop background monitoring stream gracefully."""
        logger.info("Stopping vision stream...")
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=timeout)
        self.is_monitoring = bool(self._monitor_thread and self._monitor_thread.is_alive())

    def set_motion_callback(self, callback: Callable) -> None:
        """Register a callback when motion alert triggers."""
        self.on_motion_detected = callback

    def start_monitoring(self) -> threading.Thread:
        """Alias for start_stream to start background surveillance."""
        return self.start_stream()

    def stop(self) -> None:
        """Stop monitoring and release camera."""
        self.release()

    def release(self) -> None:
        """Release webcam hardware and resources cleanly in try/finally blocks."""
        try:
            self.stop_stream()
        finally:
            with self.lock:
                if self.cap is not None:
                    try:
                        self.cap.release()
                        logger.info("Camera device released cleanly.")
                    except Exception as e:
                        logger.error(f"Error releasing camera: {e}")
                    finally:
                        self.cap = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __del__(self):
        self.release()


# ---------------------------------------------------------------------------
# CLI / Standalone Runner & Testing Interface
# ---------------------------------------------------------------------------
def run_cli():
    """CLI testing interface for JARVIS Vision."""
    import argparse

    parser = argparse.ArgumentParser(description="JARVIS Vision Subsystem CLI")
    parser.add_argument("--test", action="store_true", help="Perform comprehensive system self-test")
    parser.add_argument("--detect", action="store_true", help="Run live motion detection in console/window")
    parser.add_argument("--analyze", action="store_true", help="Capture snapshot and request Gemini Vision analysis")
    parser.add_argument("--simulate", action="store_true", help="Force simulation mode")
    parser.add_argument("--camera", type=int, default=CAMERA_INDEX, help="Camera index to use")
    parser.add_argument("--cooldown", type=int, default=MOTION_COOLDOWN, help="Alert cooldown in seconds")
    parser.add_argument("--sensitivity", type=int, default=MOTION_SENSITIVITY, help="Motion sensitivity")
    parser.add_argument("--min-area", type=int, default=MOTION_MIN_AREA, help="Min contour area for motion")
    parser.add_argument("--prompt", type=str, default="Xonada kim bor va nima qilyapti?", help="Gemini prompt")
    parser.add_argument("--headless", action="store_true", help="Do not open GUI windows (console-only mode)")
    args = parser.parse_args()

    print("=" * 65)
    print("           JARVIS VISION SUBSYSTEM - INITIALIZING")
    print("=" * 65)

    vision = JarvisVision(
        camera_index=args.camera,
        sensitivity=args.sensitivity,
        min_area=args.min_area,
        cooldown=args.cooldown,
        force_simulation=args.simulate,
    )

    if args.test:
        print("\n--- [1] Checking Camera & Mode ---")
        if vision.is_simulation_mode:
            print("Mode: SIMULATION (Safe-mode synthetic video generator active)")
        else:
            print(f"Mode: HARDWARE (Webcam index {vision.actual_camera_index} active)")

        print("\n--- [2] Testing Frame Capture ---")
        ret, frame = vision.read_frame()
        if ret and frame is not None:
            shape_str = str(frame.shape) if hasattr(frame, "shape") else "OK"
            print(f"Frame capture SUCCESS: shape = {shape_str}")
        else:
            print("Frame capture FAILED.")

        print("\n--- [3] Testing Snapshot Capture ---")
        snap_path = vision.capture_snapshot()
        print(f"Snapshot saved to: {snap_path}")

        print("\n--- [4] Testing Motion Processing Pipeline ---")
        motion, boxes, annotated = vision.process_motion(frame)
        print(f"Motion detected: {motion}, Bounding boxes count: {len(boxes)}")

        print("\n--- [5] Checking Gemini Vision Integration ---")
        if is_gemini_configured():
            print("Gemini API Key detected. Testing scene analysis...")
            description = vision.describe_current_view(prompt="Tasvirda nima ko'rinyapti? Qisqacha ayting.")
            print(f"Gemini Response:\n{description}")
        else:
            print("Gemini API Key is not set in .env. Skipping cloud API query.")

        print("\n--- [6] Testing Background Worker Stream ---")
        received_frames = []

        def _test_cb(f, m, b):
            received_frames.append((m, len(b)))

        vision.start_stream(_test_cb)
        time.sleep(1.0)
        vision.stop_stream()
        print(f"Stream received {len(received_frames)} frames in 1.0s background test.")

        print("\n" + "=" * 65)
        print("           ALL JARVIS VISION SELF-TESTS COMPLETED")
        print("=" * 65)
        vision.release()
        return

    if args.analyze:
        print(f"\nCapturing frame and querying Gemini Vision with prompt: '{args.prompt}'...")
        ret, frame = vision.read_frame()
        snap_path = vision.capture_snapshot(annotated=False)
        print(f"Snapshot captured: {snap_path}")
        analysis = vision.describe_current_view(prompt=args.prompt, frame=frame)
        print("\n--- GEMINI VISION ANALYSIS RESULT ---")
        print(analysis)
        print("-------------------------------------")
        vision.release()
        return

    if args.detect:
        print("\nStarting live motion detection. Press Ctrl+C to terminate.")
        print(f"Alert cooldown: {vision.cooldown}s | Min area: {vision.min_area}px | Sensitivity: {vision.sensitivity}")

        def _on_alert(frame, filepath, boxes):
            print(f"\n[ALERT] Motion triggered! Contours: {len(boxes)} -> Snapshot: {filepath}")

        vision.on_motion_detected = _on_alert

        show_gui = not args.headless and CV2_AVAILABLE
        try:
            count = 0
            for annotated_frame, is_motion, boxes in vision.stream_frames():
                count += 1
                status_str = f"MOTION [{len(boxes)}]" if is_motion else "IDLE"
                print(f"\r[SURVEILLANCE #{count:04d}] Status: {status_str} | Active: {vision.is_motion_active} | Presence: {vision.presence_duration:.1f}s", end="", flush=True)

                if show_gui:
                    cv2.imshow("JARVIS Vision Monitor", annotated_frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q") or key == 27:
                        break
                    elif key == ord("s"):
                        saved_path = vision.capture_snapshot(annotated=True)
                        print(f"\nManual snapshot saved: {saved_path}")
        except KeyboardInterrupt:
            print("\nMonitoring stopped by user.")
        finally:
            if show_gui:
                cv2.destroyAllWindows()
            vision.release()
        return

    # Default help when no flag passed
    parser.print_help()
    vision.release()


if __name__ == "__main__":
    run_cli()
