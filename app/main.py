from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# -----------------------------
# Optional TFLite import
# -----------------------------
Interpreter = None
try:
    from tflite_runtime.interpreter import Interpreter as _Interpreter
    Interpreter = _Interpreter
except Exception:
    try:
        from tensorflow.lite.python.interpreter import Interpreter as _Interpreter
        Interpreter = _Interpreter
    except Exception:
        Interpreter = None

BASE_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = BASE_DIR / "public"

# -----------------------------
# Model paths
# -----------------------------
AUDIO_MODEL_PATH = Path(
    os.getenv("AUDIO_MODEL_PATH", str(BASE_DIR / "models" / "improved_custom_cnn.tflite"))
)
VIDEO_MODEL_PATH = Path(
    os.getenv("VIDEO_MODEL_PATH", str(BASE_DIR / "models" / "best_float32.tflite"))
)

# -----------------------------
# Labels
# -----------------------------
AUDIO_LABELS = [
    "normal",
    "intruded",
    "false",
    "swarming",
    "queenless",
    "queenright",
]

VIDEO_LABELS_FALLBACK = {
    0: "Hive_entrance",
    1: "Other_insect",
    2: "Person",
    3: "TB_cluster",
    4: "T_biroi",
    5: "Vertebrate",
}

# -----------------------------
# Global status
# -----------------------------
AI_STATUS: Dict[str, Any] = {
    "audio_class": "normal",
    "audio_conf": 0.0,
    "audio_scores": [],
    "audio_alert": False,
    "log_only": False,
    "camera_class": "idle",
    "camera_conf": 0.0,
    "camera_detections": [],
    "camera_alert": False,
    "camera_log_only": False,
    "colony_state": "normal_activity",
    "last_audio_analysis_ts": 0.0,
    "last_video_analysis_ts": 0.0,
    "alert_active": False,
    "alert_acknowledged": False,
}

ALERT_STATE: Dict[str, Any] = {
    "active": False,
    "source": None,
    "trigger_audio_class": None,
    "triggered_at": 0.0,
    "acknowledged": False,
    "acknowledged_at": 0.0,
    "latched": False,   # once acknowledged, do not re-trigger until reset
}

MANUAL_VIDEO_VIEW: Dict[str, Any] = {
    "active": False,
    "opened_at": 0.0,
}

# -----------------------------
# Video config
# -----------------------------
VIDEO_W = int(os.getenv("VIDEO_W", "640"))
VIDEO_H = int(os.getenv("VIDEO_H", "640"))
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "20"))
VIDEO_QUALITY = int(os.getenv("VIDEO_QUALITY", "55"))
CAMERA_IDLE_SECONDS = int(os.getenv("CAMERA_IDLE_SECONDS", "5"))
VIDEO_INFER_INTERVAL = float(os.getenv("VIDEO_INFER_INTERVAL", "0.5"))
VIDEO_CONF_THRESHOLD = float(os.getenv("VIDEO_CONF_THRESHOLD", "0.25"))
VIDEO_MAX_DETECTIONS = int(os.getenv("VIDEO_MAX_DETECTIONS", "20"))

# -----------------------------
# Audio config
# -----------------------------
AUDIO_ARECORD_DEVICE = os.getenv("AUDIO_ARECORD_DEVICE", "plughw:3,0")
AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "S16_LE")
AUDIO_RATE = int(os.getenv("AUDIO_RATE", "48000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
AUDIO_CHUNK_MS = int(os.getenv("AUDIO_CHUNK_MS", "40"))

PCM_BYTES_PER_SAMPLE = 2
AUDIO_CHUNK_SAMPLES = int(AUDIO_RATE * AUDIO_CHUNK_MS / 1000)
AUDIO_CHUNK_BYTES = AUDIO_CHUNK_SAMPLES * AUDIO_CHANNELS * PCM_BYTES_PER_SAMPLE

AUDIO_WINDOW_SECONDS = float(os.getenv("AUDIO_WINDOW_SECONDS", "2.0"))
AUDIO_INFER_INTERVAL = float(os.getenv("AUDIO_INFER_INTERVAL", "1.0"))
AUDIO_WINDOW_SAMPLES = int(AUDIO_RATE * AUDIO_WINDOW_SECONDS)

# -----------------------------
# Model holders
# -----------------------------
audio_interpreter: Optional[Any] = None
audio_input_details = None
audio_output_details = None

video_interpreter: Optional[Any] = None
video_input_details = None
video_output_details = None
video_model_names = VIDEO_LABELS_FALLBACK.copy()

# -----------------------------
# Runtime state
# -----------------------------
WORKING_CAMERA_CMD: Optional[str] = None
WORKING_AUDIO_DEVICE: Optional[str] = None

audio_ws_clients: set[WebSocket] = set()
audio_broadcast_lock = asyncio.Lock()


# -----------------------------
# Alert / manual helpers
# -----------------------------
def should_trigger_video_from_audio(audio_class: str) -> bool:
    return audio_class in {"intruded", "swarming"}


def sync_alert_status_to_ai() -> None:
    AI_STATUS["alert_active"] = bool(ALERT_STATE["active"])
    AI_STATUS["alert_acknowledged"] = bool(ALERT_STATE["acknowledged"])


def activate_alert(audio_class: str) -> None:
    # latched means acknowledged already; keep log-only behavior until reset
    if ALERT_STATE["latched"]:
        sync_alert_status_to_ai()
        return

    ALERT_STATE["active"] = True
    ALERT_STATE["source"] = "audio"
    ALERT_STATE["trigger_audio_class"] = audio_class
    ALERT_STATE["triggered_at"] = time.time()
    ALERT_STATE["acknowledged"] = False
    ALERT_STATE["acknowledged_at"] = 0.0

    sync_alert_status_to_ai()


def activate_video_alert(camera_class: str) -> None:
    # same latched behavior for visual alerts
    if ALERT_STATE["latched"]:
        sync_alert_status_to_ai()
        return

    ALERT_STATE["active"] = True
    ALERT_STATE["source"] = "video"
    ALERT_STATE["trigger_audio_class"] = camera_class
    ALERT_STATE["triggered_at"] = time.time()
    ALERT_STATE["acknowledged"] = False
    ALERT_STATE["acknowledged_at"] = 0.0

    sync_alert_status_to_ai()


def acknowledge_alert() -> None:
    # acknowledge = hide/stop retrigger until reset
    ALERT_STATE["active"] = False
    ALERT_STATE["acknowledged"] = True
    ALERT_STATE["acknowledged_at"] = time.time()
    ALERT_STATE["latched"] = True

    sync_alert_status_to_ai()


def reset_alert_latch() -> None:
    ALERT_STATE["active"] = False
    ALERT_STATE["source"] = None
    ALERT_STATE["trigger_audio_class"] = None
    ALERT_STATE["triggered_at"] = 0.0
    ALERT_STATE["acknowledged"] = False
    ALERT_STATE["acknowledged_at"] = 0.0
    ALERT_STATE["latched"] = False

    sync_alert_status_to_ai()


def is_camera_needed() -> bool:
    return ALERT_STATE["active"] or MANUAL_VIDEO_VIEW["active"]


def is_video_ai_needed() -> bool:
    # Manual open of card should also open video AI
    return ALERT_STATE["active"] or MANUAL_VIDEO_VIEW["active"]


# -----------------------------
# Model loading
# -----------------------------
def load_audio_model() -> None:
    global audio_interpreter, audio_input_details, audio_output_details

    print(f"[AUDIO MODEL] interpreter available: {Interpreter is not None}")
    print(f"[AUDIO MODEL] path: {AUDIO_MODEL_PATH}")
    print(f"[AUDIO MODEL] exists: {AUDIO_MODEL_PATH.exists()}")

    if Interpreter is None:
        print("⚠️ TFLite interpreter not available. Audio AI disabled.")
        return

    if not AUDIO_MODEL_PATH.exists():
        print(f"⚠️ Audio model not found: {AUDIO_MODEL_PATH}")
        return

    try:
        audio_interpreter = Interpreter(model_path=str(AUDIO_MODEL_PATH))
        audio_interpreter.allocate_tensors()
        audio_input_details = audio_interpreter.get_input_details()
        audio_output_details = audio_interpreter.get_output_details()

        print(f"✅ Audio model loaded: {AUDIO_MODEL_PATH}")
        print(f"   input: {audio_input_details[0]['shape']}")
        print(f"   output: {audio_output_details[0]['shape']}")
    except Exception as e:
        print(f"⚠️ Failed to load audio model: {e}")
        audio_interpreter = None
        audio_input_details = None
        audio_output_details = None


def load_video_model() -> None:
    global video_interpreter, video_input_details, video_output_details, video_model_names

    print(f"[VIDEO MODEL] interpreter available: {Interpreter is not None}")
    print(f"[VIDEO MODEL] path: {VIDEO_MODEL_PATH}")
    print(f"[VIDEO MODEL] exists: {VIDEO_MODEL_PATH.exists()}")

    if Interpreter is None:
        print("⚠️ TFLite interpreter not available. Video AI disabled.")
        return

    if not VIDEO_MODEL_PATH.exists():
        print(f"⚠️ Video model not found: {VIDEO_MODEL_PATH}")
        return

    try:
        video_interpreter = Interpreter(model_path=str(VIDEO_MODEL_PATH))
        video_interpreter.allocate_tensors()
        video_input_details = video_interpreter.get_input_details()
        video_output_details = video_interpreter.get_output_details()

        print(f"✅ Video model loaded: {VIDEO_MODEL_PATH}")
        print(f"   input: {video_input_details[0]['shape']}")
        print(f"   outputs: {[d['shape'] for d in video_output_details]}")
        print(f"   classes: {video_model_names}")
    except Exception as e:
        print(f"⚠️ Failed to load video model: {e}")
        video_interpreter = None
        video_input_details = None
        video_output_details = None


# -----------------------------
# Status helpers
# -----------------------------
def compute_audio_flags(audio_class: str) -> Dict[str, bool]:
    # if alert already acknowledged, abnormal classes should become log-only until reset
    if ALERT_STATE["latched"]:
        return {
            "audio_alert": False,
            "log_only": audio_class in {"intruded", "swarming", "false", "queenless", "queenright"},
        }

    return {
        "audio_alert": audio_class in {"intruded", "swarming"},
        "log_only": audio_class in {"false", "queenless", "queenright"},
    }


def compute_video_flags(camera_class: str) -> Dict[str, bool]:
    # if alert already acknowledged, visual alerts also become log-only until reset
    if ALERT_STATE["latched"]:
        return {
            "camera_alert": False,
            "camera_log_only": camera_class in {"Person", "Other_insect", "TB_cluster", "T_biroi", "Vertebrate"},
        }

    return {
        "camera_alert": camera_class in {"Person", "Other_insect"},
        "camera_log_only": camera_class in {"TB_cluster", "T_biroi", "Vertebrate"},
    }


def derive_colony_state(audio_class: str, camera_class: str) -> str:
    if audio_class == "intruded":
        return "intrusion_detected"
    if audio_class == "swarming":
        return "swarming_detected"
    if audio_class == "queenless":
        return "post_split_queenless"
    if audio_class == "queenright":
        return "post_split_queenright"
    if camera_class in {"Person", "Other_insect"}:
        return "intrusion_detected"
    if camera_class == "Vertebrate":
        return "vertebrate_logged"
    if camera_class == "TB_cluster":
        return "tb_cluster_logged"
    if camera_class == "T_biroi":
        return "t_biroi_logged"
    return "normal_activity"


# -----------------------------
# Audio preprocessing
# -----------------------------
def pcm16_bytes_to_float32(buf: bytes) -> np.ndarray:
    audio = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
    audio /= 32768.0
    return np.clip(audio, -1.0, 1.0)


def make_log_spectrogram_128x128(audio: np.ndarray, sr: int) -> np.ndarray:
    target_len = AUDIO_WINDOW_SAMPLES
    if len(audio) < target_len:
        audio = np.pad(audio, (0, target_len - len(audio)))
    elif len(audio) > target_len:
        audio = audio[-target_len:]

    if len(audio) > 1:
        audio = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])

    frame_length = 512
    hop_length = 256
    window = np.hanning(frame_length).astype(np.float32)

    frames = []
    for start in range(0, max(1, len(audio) - frame_length + 1), hop_length):
        frame = audio[start:start + frame_length]
        if len(frame) < frame_length:
            frame = np.pad(frame, (0, frame_length - len(frame)))
        frame = frame * window
        spec = np.fft.rfft(frame)
        mag = np.abs(spec)
        frames.append(mag)

    if not frames:
        frames = [np.zeros((frame_length // 2 + 1,), dtype=np.float32)]

    spec = np.stack(frames, axis=1)
    spec = np.log1p(spec)

    spec -= spec.min()
    if spec.max() > 0:
        spec /= spec.max()

    spec_img = cv2.resize(spec, (128, 128), interpolation=cv2.INTER_AREA).astype(np.float32)
    spec_img = np.expand_dims(spec_img, axis=(0, -1))
    return spec_img


def run_audio_inference_from_pcm_bytes(buf: bytes) -> Dict[str, Any]:
    if (
        audio_interpreter is None
        or audio_input_details is None
        or audio_output_details is None
    ):
        return {
            "audio_class": "audio_model_unavailable",
            "audio_conf": 0.0,
            "audio_scores": [],
        }

    try:
        audio = pcm16_bytes_to_float32(buf)
        x = make_log_spectrogram_128x128(audio, AUDIO_RATE)

        input_index = audio_input_details[0]["index"]
        output_index = audio_output_details[0]["index"]

        input_dtype = audio_input_details[0]["dtype"]
        if input_dtype == np.float32:
            tensor = x.astype(np.float32)
        else:
            scale, zero_point = audio_input_details[0].get("quantization", (0.0, 0))
            if scale and scale > 0:
                tensor = np.round(x / scale + zero_point).astype(input_dtype)
            else:
                tensor = x.astype(input_dtype)

        audio_interpreter.set_tensor(input_index, tensor)
        audio_interpreter.invoke()
        output = audio_interpreter.get_tensor(output_index)[0].astype(np.float32)

        out_scale, out_zero = audio_output_details[0].get("quantization", (0.0, 0))
        if out_scale and out_scale > 0:
            output = (output - out_zero) * out_scale

        probs = output.astype(np.float32)
        if probs.ndim != 1:
            probs = probs.reshape(-1)

        if np.any(probs < 0.0) or np.sum(probs) > 1.5:
            exps = np.exp(probs - np.max(probs))
            probs = exps / np.sum(exps)

        top_idx = int(np.argmax(probs))
        top_conf = float(probs[top_idx])
        label = AUDIO_LABELS[top_idx] if top_idx < len(AUDIO_LABELS) else f"class_{top_idx}"

        scores = []
        for i, score in enumerate(probs.tolist()):
            lbl = AUDIO_LABELS[i] if i < len(AUDIO_LABELS) else f"class_{i}"
            scores.append({"class": lbl, "conf": round(float(score), 4)})

        scores.sort(key=lambda x: x["conf"], reverse=True)

        return {
            "audio_class": label,
            "audio_conf": round(top_conf, 4),
            "audio_scores": scores,
        }
    except Exception as e:
        print(f"⚠️ Audio inference failed: {e}")
        return {
            "audio_class": "audio_inference_error",
            "audio_conf": 0.0,
            "audio_scores": [],
        }


# -----------------------------
# Video preprocessing / parsing
# -----------------------------
def _video_input_shape() -> Tuple[int, int, int, int]:
    shape = list(video_input_details[0]["shape"])
    if len(shape) != 4:
        raise ValueError(f"Unsupported video input shape: {shape}")
    return int(shape[0]), int(shape[1]), int(shape[2]), int(shape[3])


def _letterbox(image: np.ndarray, new_shape: Tuple[int, int]) -> Tuple[np.ndarray, float, Tuple[float, float]]:
    h, w = image.shape[:2]
    new_w, new_h = new_shape
    r = min(new_w / w, new_h / h)

    resized_w = int(round(w * r))
    resized_h = int(round(h * r))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)

    canvas = np.full((new_h, new_w, 3), 114, dtype=np.uint8)
    dw = (new_w - resized_w) / 2
    dh = (new_h - resized_h) / 2

    left = int(round(dw - 0.1))
    top = int(round(dh - 0.1))
    canvas[top:top + resized_h, left:left + resized_w] = resized
    return canvas, r, (dw, dh)


def _prepare_video_input(frame_bgr: np.ndarray) -> Tuple[np.ndarray, float, Tuple[float, float]]:
    _, in_h, in_w, in_c = _video_input_shape()
    if in_c != 3:
        raise ValueError(f"Unsupported video channels: {in_c}")

    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    padded, ratio, dwdh = _letterbox(rgb, (in_w, in_h))
    x = np.expand_dims(padded, axis=0)

    dtype = video_input_details[0]["dtype"]
    scale, zero_point = video_input_details[0].get("quantization", (0.0, 0))

    if dtype == np.float32:
        x = x.astype(np.float32) / 255.0
    elif np.issubdtype(dtype, np.integer):
        x = x.astype(np.float32)
        if scale and scale > 0:
            x = np.round(x / scale + zero_point)
        x = np.clip(x, np.iinfo(dtype).min, np.iinfo(dtype).max).astype(dtype)
    else:
        x = x.astype(dtype)

    return x, ratio, dwdh


def _dequantize_output(arr: np.ndarray, detail: Dict[str, Any]) -> np.ndarray:
    arr = arr.astype(np.float32)
    scale, zero_point = detail.get("quantization", (0.0, 0))
    if scale and scale > 0:
        arr = (arr - zero_point) * scale
    return arr


def _xywh_to_xyxy(xywh: np.ndarray) -> np.ndarray:
    out = np.zeros_like(xywh, dtype=np.float32)
    out[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    out[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    out[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    out[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    return out


def _scale_boxes_to_original(
    boxes: np.ndarray,
    original_shape: Tuple[int, int],
    ratio: float,
    dwdh: Tuple[float, float],
) -> np.ndarray:
    h0, w0 = original_shape
    dw, dh = dwdh

    boxes = boxes.copy().astype(np.float32)
    boxes[:, [0, 2]] -= dw
    boxes[:, [1, 3]] -= dh
    boxes[:, :4] /= max(ratio, 1e-6)

    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, w0)
    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, h0)
    return boxes


def _iou(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])

    inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    area1 = np.maximum(0, box[2] - box[0]) * np.maximum(0, box[3] - box[1])
    area2 = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])

    union = area1 + area2 - inter + 1e-6
    return inter / union


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float = 0.45) -> List[int]:
    if len(boxes) == 0:
        return []

    order = scores.argsort()[::-1]
    keep: List[int] = []

    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break

        ious = _iou(boxes[i], boxes[order[1:]])
        inds = np.where(ious <= iou_thr)[0]
        order = order[inds + 1]

    return keep


def _parse_yolo_output(
    outputs: List[np.ndarray],
    frame_shape: Tuple[int, int],
    ratio: float,
    dwdh: Tuple[float, float],
) -> Dict[str, Any]:
    if not outputs:
        return {
            "camera_class": "no_detection",
            "camera_conf": 0.0,
            "camera_detections": [],
        }

    pred = np.squeeze(outputs[0])

    if pred.ndim != 2:
        pred = pred.reshape(pred.shape[0], -1) if pred.ndim > 2 else pred.reshape(1, -1)

    num_classes = len(video_model_names)

    if pred.shape[0] == 4 + num_classes:
        pred = pred.T
    elif pred.shape[1] == 4 + num_classes:
        pass
    elif pred.T.shape[1] == 4 + num_classes:
        pred = pred.T
    else:
        return {
            "camera_class": "video_output_unrecognized",
            "camera_conf": 0.0,
            "camera_detections": [],
        }

    boxes_xywh = pred[:, :4].astype(np.float32)
    cls_scores = pred[:, 4:4 + num_classes].astype(np.float32)

    cls_ids = np.argmax(cls_scores, axis=1)
    confs = cls_scores[np.arange(len(cls_scores)), cls_ids]

    mask = confs >= VIDEO_CONF_THRESHOLD
    if not np.any(mask):
        return {
            "camera_class": "no_detection",
            "camera_conf": 0.0,
            "camera_detections": [],
        }

    boxes_xywh = boxes_xywh[mask]
    cls_ids = cls_ids[mask]
    confs = confs[mask]

    boxes_xyxy = _xywh_to_xyxy(boxes_xywh)
    boxes_xyxy = _scale_boxes_to_original(boxes_xyxy, frame_shape, ratio, dwdh)

    final_detections: List[Dict[str, Any]] = []

    for cls_id in np.unique(cls_ids):
        idxs = np.where(cls_ids == cls_id)[0]
        class_boxes = boxes_xyxy[idxs]
        class_scores = confs[idxs]
        keep = _nms(class_boxes, class_scores, iou_thr=0.45)

        for k in keep:
            box = class_boxes[k]
            score = float(class_scores[k])
            label = video_model_names.get(int(cls_id), f"class_{int(cls_id)}")
            final_detections.append({
                "class": label,
                "conf": round(score, 4),
                "bbox": [
                    round(float(box[0]), 2),
                    round(float(box[1]), 2),
                    round(float(box[2]), 2),
                    round(float(box[3]), 2),
                ],
            })

    final_detections.sort(key=lambda x: x["conf"], reverse=True)
    final_detections = final_detections[:VIDEO_MAX_DETECTIONS]

    if not final_detections:
        return {
            "camera_class": "no_detection",
            "camera_conf": 0.0,
            "camera_detections": [],
        }

    top = final_detections[0]
    return {
        "camera_class": top["class"],
        "camera_conf": top["conf"],
        "camera_detections": final_detections,
    }


def run_video_inference(frame_bgr: np.ndarray) -> Dict[str, Any]:
    if (
        video_interpreter is None
        or video_input_details is None
        or video_output_details is None
    ):
        return {
            "camera_class": "video_model_unavailable",
            "camera_conf": 0.0,
            "camera_detections": [],
        }

    try:
        h, w = frame_bgr.shape[:2]
        x, ratio, dwdh = _prepare_video_input(frame_bgr)

        input_index = video_input_details[0]["index"]
        video_interpreter.set_tensor(input_index, x)
        video_interpreter.invoke()

        outputs = []
        for d in video_output_details:
            raw = video_interpreter.get_tensor(d["index"])
            outputs.append(_dequantize_output(raw, d))

        return _parse_yolo_output(outputs, (h, w), ratio, dwdh)
    except Exception as e:
        print(f"⚠️ Video inference failed: {e}")
        return {
            "camera_class": "video_inference_error",
            "camera_conf": 0.0,
            "camera_detections": [],
        }


# -----------------------------
# Camera helpers
# -----------------------------
def _camera_cmd_candidates() -> List[str]:
    out: List[str] = []
    for name in ("rpicam-vid", "libcamera-vid"):
        p = shutil.which(name)
        if p:
            out.append(p)
    return out


def _validate_camera_cmd(cmd: str) -> bool:
    try:
        subprocess.run([cmd, "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
        return True
    except OSError as e:
        print(f"⚠️ Camera command unusable: {cmd} ({e})")
        return False
    except Exception:
        return True


def _detect_camera_backend() -> None:
    global WORKING_CAMERA_CMD
    for cmd in _camera_cmd_candidates():
        if _validate_camera_cmd(cmd):
            WORKING_CAMERA_CMD = cmd
            print(f"✅ Pi camera backend: {cmd}")
            return
    WORKING_CAMERA_CMD = None
    print("⚠️ No usable Pi camera backend found.")


async def _spawn_camera_mjpeg() -> asyncio.subprocess.Process:
    if WORKING_CAMERA_CMD is None:
        raise RuntimeError("No rpicam/libcamera backend available.")

    cmd = [
        WORKING_CAMERA_CMD,
        "-t", "0",
        "--width", str(VIDEO_W),
        "--height", str(VIDEO_H),
        "--framerate", str(VIDEO_FPS),
        "--rotation", "180",
        "--codec", "mjpeg",
        "-o", "-",
        "--nopreview",
    ]
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


@dataclass
class CameraState:
    proc: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = None
    last_frame: Optional[bytes] = None
    last_client_ts: float = 0.0
    clients: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


camera_state = CameraState()


async def _stop_camera_if_idle() -> None:
    async with camera_state.lock:
        if camera_state.proc:
            with contextlib.suppress(Exception):
                camera_state.proc.terminate()
            camera_state.proc = None
        camera_state.last_frame = None
        camera_state.last_client_ts = time.time()


async def _camera_reader_loop() -> None:
    soi, eoi = b"\xff\xd8", b"\xff\xd9"
    buf = bytearray()

    while True:
        proc = None
        should_keep_running = False
        last_seen = 0.0

        async with camera_state.lock:
            should_keep_running = is_camera_needed() or camera_state.clients > 0
            last_seen = camera_state.last_client_ts
            proc = camera_state.proc

        if not should_keep_running:
            if (time.time() - last_seen) > CAMERA_IDLE_SECONDS:
                async with camera_state.lock:
                    if camera_state.proc:
                        with contextlib.suppress(Exception):
                            camera_state.proc.terminate()
                        camera_state.proc = None
                        camera_state.last_frame = None
                return

            await asyncio.sleep(0.25)
            continue

        if proc is None:
            try:
                proc = await _spawn_camera_mjpeg()
                buf.clear()
                async with camera_state.lock:
                    camera_state.proc = proc
            except Exception as e:
                print(f"❌ Failed to start camera: {e}")
                await asyncio.sleep(1)
                continue

        try:
            chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=2.0)
            if not chunk:
                await asyncio.sleep(0.01)
                continue

            buf.extend(chunk)

            while True:
                start = buf.find(soi)
                if start == -1:
                    break
                end = buf.find(eoi, start + 2)
                if end == -1:
                    break

                jpg = bytes(buf[start:end + 2])
                del buf[:end + 2]

                async with camera_state.lock:
                    camera_state.last_frame = jpg
                    camera_state.last_client_ts = time.time()

        except Exception:
            await asyncio.sleep(0.05)
            continue


async def _ensure_camera_running() -> None:
    async with camera_state.lock:
        camera_state.last_client_ts = time.time()
        if not camera_state.task or camera_state.task.done():
            camera_state.task = asyncio.create_task(_camera_reader_loop())


async def _mjpeg_stream_generator():
    if not is_camera_needed():
        return

    await _ensure_camera_running()

    async with camera_state.lock:
        camera_state.clients += 1
        camera_state.last_client_ts = time.time()

    try:
        while is_camera_needed():
            async with camera_state.lock:
                frame = camera_state.last_frame
                camera_state.last_client_ts = time.time()

            if frame:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

            await asyncio.sleep(1.0 / max(VIDEO_FPS, 1))
    finally:
        async with camera_state.lock:
            camera_state.clients = max(camera_state.clients - 1, 0)
            camera_state.last_client_ts = time.time()


def decode_jpeg_to_bgr(jpg_bytes: bytes) -> Optional[np.ndarray]:
    try:
        arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


# -----------------------------
# Audio helpers
# -----------------------------
def _audio_device_candidates() -> List[str]:
    seen = set()
    out = []
    for dev in [AUDIO_ARECORD_DEVICE, "default"]:
        if dev and dev not in seen:
            seen.add(dev)
            out.append(dev)
    return out


async def _spawn_arecord_for_device(device: str) -> asyncio.subprocess.Process:
    cmd = [
        "arecord",
        "-q",
        "-D", device,
        "-f", AUDIO_FORMAT,
        "-c", str(AUDIO_CHANNELS),
        "-r", str(AUDIO_RATE),
        "-t", "raw",
    ]
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _spawn_arecord() -> asyncio.subprocess.Process:
    global WORKING_AUDIO_DEVICE

    errors = []
    for dev in _audio_device_candidates():
        try:
            proc = await _spawn_arecord_for_device(dev)
            WORKING_AUDIO_DEVICE = dev
            print(f"✅ Audio capture device: {dev}")
            return proc
        except Exception as e:
            errors.append(f"{dev}: {e}")

    raise RuntimeError("No working audio device. " + " | ".join(errors))


# -----------------------------
# Background tasks
# -----------------------------
async def background_audio_analyzer():
    print("🎙️ Background Audio Analyzer started")

    while True:
        proc = None
        try:
            proc = await _spawn_arecord()
            rolling = deque(maxlen=AUDIO_WINDOW_SAMPLES)
            last_infer = 0.0

            while True:
                chunk = await proc.stdout.readexactly(AUDIO_CHUNK_BYTES)

                async with audio_broadcast_lock:
                    dead = []
                    for client in audio_ws_clients:
                        try:
                            await client.send_bytes(chunk)
                        except Exception:
                            dead.append(client)
                    for client in dead:
                        audio_ws_clients.discard(client)

                samples = np.frombuffer(chunk, dtype=np.int16)
                rolling.extend(samples.tolist())

                now = time.time()
                if len(rolling) < AUDIO_WINDOW_SAMPLES:
                    continue
                if (now - last_infer) < AUDIO_INFER_INTERVAL:
                    continue

                pcm_window = np.array(rolling, dtype=np.int16).tobytes()
                pred = run_audio_inference_from_pcm_bytes(pcm_window)
                flags = compute_audio_flags(pred["audio_class"])

                print(
                    f"[AUDIO AI] class={pred['audio_class']} "
                    f"conf={pred['audio_conf']} "
                    f"ts={round(now, 2)} "
                    f"latched={ALERT_STATE['latched']}"
                )

                AI_STATUS["audio_class"] = pred["audio_class"]
                AI_STATUS["audio_conf"] = pred["audio_conf"]
                AI_STATUS["audio_scores"] = pred["audio_scores"]
                AI_STATUS["audio_alert"] = flags["audio_alert"]
                AI_STATUS["log_only"] = flags["log_only"]
                AI_STATUS["last_audio_analysis_ts"] = now

                if flags["audio_alert"] and should_trigger_video_from_audio(pred["audio_class"]):
                    activate_alert(pred["audio_class"])

                AI_STATUS["colony_state"] = derive_colony_state(
                    AI_STATUS["audio_class"],
                    AI_STATUS["camera_class"],
                )

                sync_alert_status_to_ai()
                last_infer = now

        except asyncio.IncompleteReadError:
            print("⚠️ Audio stream interrupted, restarting arecord...")
            await asyncio.sleep(1)

        except Exception as e:
            print(f"❌ Background audio error: {e}")
            await asyncio.sleep(2)

        finally:
            if proc:
                with contextlib.suppress(Exception):
                    proc.terminate()


async def background_video_analyzer():
    print("📷 Background Video Analyzer started")

    while True:
        try:
            if not is_video_ai_needed():
                AI_STATUS["camera_class"] = "idle"
                AI_STATUS["camera_conf"] = 0.0
                AI_STATUS["camera_detections"] = []
                AI_STATUS["camera_alert"] = False
                AI_STATUS["camera_log_only"] = False

                if not is_camera_needed():
                    await _stop_camera_if_idle()

                await asyncio.sleep(0.5)
                continue

            await _ensure_camera_running()

            async with camera_state.lock:
                jpg = camera_state.last_frame

            if jpg:
                frame = decode_jpeg_to_bgr(jpg)
                if frame is not None:
                    pred = run_video_inference(frame)
                    flags = compute_video_flags(pred["camera_class"])

                    AI_STATUS["camera_class"] = pred["camera_class"]
                    AI_STATUS["camera_conf"] = pred["camera_conf"]
                    AI_STATUS["camera_detections"] = pred["camera_detections"]
                    AI_STATUS["camera_alert"] = flags["camera_alert"]
                    AI_STATUS["camera_log_only"] = flags["camera_log_only"]
                    AI_STATUS["last_video_analysis_ts"] = time.time()

                    if flags["camera_alert"]:
                        activate_video_alert(pred["camera_class"])

                    AI_STATUS["colony_state"] = derive_colony_state(
                        AI_STATUS["audio_class"],
                        AI_STATUS["camera_class"],
                    )

                    sync_alert_status_to_ai()

            await asyncio.sleep(VIDEO_INFER_INTERVAL)

        except Exception as e:
            print(f"❌ Background video error: {e}")
            await asyncio.sleep(1)


# -----------------------------
# FastAPI
# -----------------------------
app = FastAPI(title="HiveMind Pi Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    load_audio_model()
    load_video_model()
    _detect_camera_backend()

    asyncio.create_task(background_audio_analyzer())
    asyncio.create_task(background_video_analyzer())


class EnvPayload(BaseModel):
    temperature: float
    humidity: float
    weight: float


LATEST_ENV = {
    "temp_c": None,
    "hum_pct": None,
    "weight_kg": None,
    "ts": None,
}


@app.post("/receive-data")
async def receive_data(payload: EnvPayload):
    LATEST_ENV.update({
        "temp_c": payload.temperature,
        "hum_pct": payload.humidity,
        "weight_kg": payload.weight, # <--- Siguraduhin na may LATEST_ENV.update
        "ts": time.time(),
    })
    print(f"📥 SUCCESS: Received Weight {payload.weight}kg")
    return {"ok": True}


@app.post("/api/alert/ack")
async def api_ack_alert():
    acknowledge_alert()

    # after acknowledge: stop active visual alert UI/stream trigger,
    # but keep AI + logs behavior available via latest status
    AI_STATUS["camera_alert"] = False

    if not is_camera_needed():
        await _stop_camera_if_idle()

    AI_STATUS["colony_state"] = derive_colony_state(
        AI_STATUS["audio_class"],
        AI_STATUS["camera_class"],
    )
    sync_alert_status_to_ai()

    return {
        "ok": True,
        "alert_active": ALERT_STATE["active"],
        "alert_acknowledged": ALERT_STATE["acknowledged"],
        "alert_latched": ALERT_STATE["latched"],
    }


@app.post("/api/alert/reset")
async def api_reset_alert():
    reset_alert_latch()
    AI_STATUS["colony_state"] = derive_colony_state(
        AI_STATUS["audio_class"],
        AI_STATUS["camera_class"],
    )
    return {
        "ok": True,
        "alert_active": ALERT_STATE["active"],
        "alert_acknowledged": ALERT_STATE["acknowledged"],
        "alert_latched": ALERT_STATE["latched"],
    }


@app.post("/api/video/open")
async def api_video_open():
    MANUAL_VIDEO_VIEW["active"] = True
    MANUAL_VIDEO_VIEW["opened_at"] = time.time()
    await _ensure_camera_running()
    return {"ok": True, "manual_video_active": True}


@app.post("/receive-data")
async def receive_data(payload: EnvPayload):
    LATEST_ENV.update({
        "temp_c": payload.temperature,
        "hum_pct": payload.humidity,
        "weight_kg": payload.weight,
        "ts": time.time(),
    })
    return {"ok": True}


@app.get("/api/latest")
async def api_latest():
    sync_alert_status_to_ai()

    # Kunin natin ang values sa LATEST_ENV nang maayos
    temp = LATEST_ENV.get("temp_c")
    hum = LATEST_ENV.get("hum_pct")
    weight = LATEST_ENV.get("weight_kg")

    return JSONResponse({
        # --- SENSOR DATA ---
        "temp_c": temp if temp is not None else 0.0,
        "hum_pct": hum if hum is not None else 0.0,
        "weight_kg": weight if weight is not None else 0.0,
        "weight": weight if weight is not None else 0.0, # Backup key para sa website

        # --- AUDIO AI STATUS ---
        "audio_class": AI_STATUS.get("audio_class", "normal"),
        "audio_conf": AI_STATUS.get("audio_conf", 0.0),
        "audio_scores": AI_STATUS.get("audio_scores", []),
        "audio_alert": AI_STATUS.get("audio_alert", False),
        "log_only": AI_STATUS.get("log_only", False),

        # --- CAMERA AI STATUS ---
        "camera_class": AI_STATUS.get("camera_class", "idle"),
        "camera_conf": AI_STATUS.get("camera_conf", 0.0),
        "camera_detections": AI_STATUS.get("camera_detections", []),
        "camera_alert": AI_STATUS.get("camera_alert", False),
        "camera_log_only": AI_STATUS.get("camera_log_only", False),

        # --- HARDWARE STATUS ---
        "audio_device": WORKING_AUDIO_DEVICE,
        "camera_cmd": WORKING_CAMERA_CMD,

        # --- ALERT STATE ---
        "alert_active": ALERT_STATE.get("active", False),
        "alert_source": ALERT_STATE.get("source"),
        "trigger_audio_class": ALERT_STATE.get("trigger_audio_class"),
        "triggered_at": ALERT_STATE.get("triggered_at", 0.0),
        "alert_acknowledged": ALERT_STATE.get("acknowledged", False),
        "alert_acknowledged_at": ALERT_STATE.get("acknowledged_at", 0.0),
        "alert_latched": ALERT_STATE.get("latched", False),

        # --- MANUAL OVERRIDE ---
        "manual_video_active": MANUAL_VIDEO_VIEW.get("active", False),
        "manual_video_opened_at": MANUAL_VIDEO_VIEW.get("opened_at", 0.0),

        # --- COLONY STATUS ---
        "colony_state": AI_STATUS.get("colony_state", "normal_activity"),
        "last_audio_analysis_ts": AI_STATUS.get("last_audio_analysis_ts", 0.0),
        "last_video_analysis_ts": AI_STATUS.get("last_video_analysis_ts", 0.0),
        "ts": time.time(),
    })

@app.get("/video.mjpg")
async def video_mjpg():
    if not is_camera_needed():
        return JSONResponse({"ok": False, "message": "video not active"}, status_code=404)

    return StreamingResponse(
        _mjpeg_stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()

    async with audio_broadcast_lock:
        audio_ws_clients.add(ws)

    try:
        while True:
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with audio_broadcast_lock:
            audio_ws_clients.discard(ws)


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
