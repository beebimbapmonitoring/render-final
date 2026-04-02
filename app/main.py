# app/main.py
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
from typing import Optional, List, Dict, Any, Tuple, AsyncGenerator

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

PI_API_TOKEN = os.getenv("PI_API_TOKEN", "").strip()


def require_bearer(authorization: str | None) -> None:
    if not PI_API_TOKEN:
        return
    if authorization != f"Bearer {PI_API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def ws_require_bearer(ws: WebSocket) -> bool:
    if not PI_API_TOKEN:
        return True
    return ws.headers.get("authorization") == f"Bearer {PI_API_TOKEN}"


AUDIO_MODEL_PATH = Path(
    os.getenv("AUDIO_MODEL_PATH", str(BASE_DIR / "models" / "improved_custom_cnn.tflite"))
)
VIDEO_MODEL_PATH = Path(
    os.getenv("VIDEO_MODEL_PATH", str(BASE_DIR / "models" / "best_float32.tflite"))
)

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

AUDIO_CAMERA_TRIGGER_CLASSES = {"intruded", "swarming", "normal"}
AUDIO_ALERT_CLASSES = {"intruded", "swarming"}
VIDEO_IGNORED_CLASSES = {"T_biroi"}

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
    "audio_first_normal_logged_at": 0.0,
    "audio_last_change_ts": 0.0,
    "audio_video_trigger_active": False,
    "audio_video_trigger_class": None,
    "audio_video_triggered_at": 0.0,
}

ALERT_STATE: Dict[str, Any] = {
    "active": False,
    "source": None,
    "trigger_audio_class": None,
    "triggered_at": 0.0,
    "acknowledged": False,
    "acknowledged_at": 0.0,
    "latched": False,
}

AUDIO_VIDEO_TRIGGER: Dict[str, Any] = {
    "active": False,
    "audio_class": None,
    "triggered_at": 0.0,
}

MANUAL_VIDEO_VIEW: Dict[str, Any] = {
    "active": False,
    "opened_at": 0.0,
}

AUDIO_STATE: Dict[str, Any] = {
    "last_class": None,
    "last_change_ts": 0.0,
    "normal_logged": False,
    "normal_first_logged_at": 0.0,
}

VIDEO_W = int(os.getenv("VIDEO_W", "480"))
VIDEO_H = int(os.getenv("VIDEO_H", "480"))
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "40"))
VIDEO_QUALITY = int(os.getenv("VIDEO_QUALITY", "40"))

STREAM_W = int(os.getenv("STREAM_W", "320"))
STREAM_H = int(os.getenv("STREAM_H", "320"))
STREAM_FPS = int(os.getenv("STREAM_FPS", "5"))
STREAM_QUALITY = int(os.getenv("STREAM_QUALITY", "35"))

CAMERA_IDLE_SECONDS = int(os.getenv("CAMERA_IDLE_SECONDS", "2"))
VIDEO_INFER_INTERVAL = float(os.getenv("VIDEO_INFER_INTERVAL", "0.75"))
VIDEO_CONF_THRESHOLD = float(os.getenv("VIDEO_CONF_THRESHOLD", "0.25"))
VIDEO_MAX_DETECTIONS = int(os.getenv("VIDEO_MAX_DETECTIONS", "10"))
VIDEO_AI_ALWAYS_ON = os.getenv("VIDEO_AI_ALWAYS_ON", "0").strip().lower() in {"1", "true", "yes", "on"}
CV2_CAMERA_INDEX = int(os.getenv("CV2_CAMERA_INDEX", "0"))
CV2_CAMERA_ROTATE_180 = os.getenv("CV2_CAMERA_ROTATE_180", "1").strip().lower() in {"1", "true", "yes", "on"}

AUDIO_ARECORD_DEVICE = os.getenv("AUDIO_ARECORD_DEVICE", "plughw:2,0")
AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "S16_LE")
AUDIO_RATE = int(os.getenv("AUDIO_RATE", "16000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
AUDIO_CHUNK_MS = int(os.getenv("AUDIO_CHUNK_MS", "40"))

PCM_BYTES_PER_SAMPLE = 2
AUDIO_CHUNK_SAMPLES = int(AUDIO_RATE * AUDIO_CHUNK_MS / 1000)
AUDIO_CHUNK_BYTES = AUDIO_CHUNK_SAMPLES * AUDIO_CHANNELS * PCM_BYTES_PER_SAMPLE

AUDIO_WINDOW_SECONDS = float(os.getenv("AUDIO_WINDOW_SECONDS", "2.0"))
AUDIO_INFER_INTERVAL = float(os.getenv("AUDIO_INFER_INTERVAL", "1.0"))
AUDIO_WINDOW_SAMPLES = int(AUDIO_RATE * AUDIO_WINDOW_SECONDS)
AUDIO_HTTP_QUEUE_MAX = int(os.getenv("AUDIO_HTTP_QUEUE_MAX", "80"))

audio_interpreter: Optional[Any] = None
audio_input_details = None
audio_output_details = None

video_interpreter: Optional[Any] = None
video_input_details = None
video_output_details = None
video_model_names = VIDEO_LABELS_FALLBACK.copy()

WORKING_CAMERA_CMD: Optional[str] = None
WORKING_AUDIO_DEVICE: Optional[str] = None

audio_ws_clients: set[WebSocket] = set()
audio_http_clients: set[asyncio.Queue] = set()
audio_broadcast_lock = asyncio.Lock()

AUDIO_RUNTIME: Dict[str, Any] = {
    "restarts": 0,
    "last_error": None,
    "last_start_ts": 0.0,
    "last_chunk_ts": 0.0,
    "last_exit_code": None,
    "stderr_tail": "",
}

CAMERA_RUNTIME: Dict[str, Any] = {
    "starts": 0,
    "last_error": None,
    "last_start_ts": 0.0,
    "last_frame_ts": 0.0,
    "last_exit_code": None,
    "stderr_tail": "",
    "backend": None,
}


class EnvPayload(BaseModel):
    temperature: float
    humidity: float
    weight: float


LATEST_ENV: Dict[str, Any] = {
    "temp_c": None,
    "hum_pct": None,
    "weight_kg": None,
    "ts": None,
}


def should_trigger_video_from_audio(audio_class: str) -> bool:
    return audio_class in AUDIO_CAMERA_TRIGGER_CLASSES


def should_raise_audio_alert(audio_class: str) -> bool:
    return audio_class in AUDIO_ALERT_CLASSES


def sanitize_audio_class(audio_class: str) -> str:
    raw = str(audio_class or "normal").strip().lower()
    if raw == "false":
        return "normal"
    return raw


def sanitize_audio_scores(audio_scores: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for item in audio_scores or []:
        label = str(item.get("class", "")).strip().lower()
        if label == "false":
            continue
        cleaned.append(item)
    return cleaned


def sync_alert_status_to_ai() -> None:
    AI_STATUS["alert_active"] = bool(ALERT_STATE["active"])
    AI_STATUS["alert_acknowledged"] = bool(ALERT_STATE["acknowledged"])
    AI_STATUS["audio_video_trigger_active"] = bool(AUDIO_VIDEO_TRIGGER["active"])
    AI_STATUS["audio_video_trigger_class"] = AUDIO_VIDEO_TRIGGER["audio_class"]
    AI_STATUS["audio_video_triggered_at"] = AUDIO_VIDEO_TRIGGER["triggered_at"]


def activate_alert(audio_class: str) -> None:
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


def activate_audio_video_trigger(audio_class: str) -> None:
    AUDIO_VIDEO_TRIGGER["active"] = True
    AUDIO_VIDEO_TRIGGER["audio_class"] = audio_class
    AUDIO_VIDEO_TRIGGER["triggered_at"] = time.time()
    sync_alert_status_to_ai()


def deactivate_audio_video_trigger() -> None:
    AUDIO_VIDEO_TRIGGER["active"] = False
    AUDIO_VIDEO_TRIGGER["audio_class"] = None
    AUDIO_VIDEO_TRIGGER["triggered_at"] = 0.0
    sync_alert_status_to_ai()


def acknowledge_alert() -> None:
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


def activate_manual_video_view() -> None:
    MANUAL_VIDEO_VIEW["active"] = True
    MANUAL_VIDEO_VIEW["opened_at"] = time.time()


def deactivate_manual_video_view() -> None:
    MANUAL_VIDEO_VIEW["active"] = False
    MANUAL_VIDEO_VIEW["opened_at"] = 0.0


def is_camera_needed() -> bool:
    return (
        ALERT_STATE["active"]
        or MANUAL_VIDEO_VIEW["active"]
        or AUDIO_VIDEO_TRIGGER["active"]
        or VIDEO_AI_ALWAYS_ON
    )


def is_video_ai_needed() -> bool:
    return (
        ALERT_STATE["active"]
        or MANUAL_VIDEO_VIEW["active"]
        or AUDIO_VIDEO_TRIGGER["active"]
        or VIDEO_AI_ALWAYS_ON
    )


def update_audio_state(audio_class: str) -> None:
    now = time.time()
    previous = AUDIO_STATE["last_class"]

    if previous != audio_class:
        AUDIO_STATE["last_class"] = audio_class
        AUDIO_STATE["last_change_ts"] = now
        AI_STATUS["audio_last_change_ts"] = now

        if audio_class == "normal":
            AUDIO_STATE["normal_logged"] = True
            AUDIO_STATE["normal_first_logged_at"] = now
            AI_STATUS["audio_first_normal_logged_at"] = now
            print(f"🟢 First normal detected at {now:.3f}")
        else:
            AUDIO_STATE["normal_logged"] = False
            AUDIO_STATE["normal_first_logged_at"] = 0.0
            AI_STATUS["audio_first_normal_logged_at"] = 0.0

        print(f"🎧 Audio class changed: {previous} -> {audio_class}")
        return

    if audio_class == "normal" and AUDIO_STATE["normal_logged"]:
        return


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


def compute_audio_flags(audio_class: str) -> Dict[str, bool]:
    audio_class = sanitize_audio_class(audio_class)

    if ALERT_STATE["latched"]:
        return {
            "audio_alert": False,
            "log_only": audio_class in {"queenless", "queenright"},
        }

    return {
        "audio_alert": audio_class in AUDIO_ALERT_CLASSES,
        "log_only": audio_class in {"queenless", "queenright"},
    }


def compute_video_flags(camera_class: str) -> Dict[str, bool]:
    if camera_class in VIDEO_IGNORED_CLASSES:
        return {
            "camera_alert": False,
            "camera_log_only": False,
        }

    if ALERT_STATE["latched"]:
        return {
            "camera_alert": False,
            "camera_log_only": camera_class in {"Person", "Other_insect", "TB_cluster", "Vertebrate"},
        }

    return {
        "camera_alert": camera_class in {"Person", "Other_insect"},
        "camera_log_only": camera_class in {"TB_cluster", "Vertebrate"},
    }


def derive_colony_state(audio_class: str, camera_class: str) -> str:
    audio_class = sanitize_audio_class(audio_class)

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
    return "normal_activity"


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
    if audio_interpreter is None or audio_input_details is None or audio_output_details is None:
        return {"audio_class": "audio_model_unavailable", "audio_conf": 0.0, "audio_scores": []}

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
        raw_label = AUDIO_LABELS[top_idx] if top_idx < len(AUDIO_LABELS) else f"class_{top_idx}"
        label = sanitize_audio_class(raw_label)

        scores = []
        for i, score in enumerate(probs.tolist()):
            lbl = AUDIO_LABELS[i] if i < len(AUDIO_LABELS) else f"class_{i}"
            scores.append({"class": lbl, "conf": round(float(score), 4)})

        scores.sort(key=lambda x: x["conf"], reverse=True)
        scores = sanitize_audio_scores(scores)

        if label == "normal":
            top_conf = next((float(item["conf"]) for item in scores if item["class"] == "normal"), 0.0)

        return {
            "audio_class": label,
            "audio_conf": round(float(top_conf), 4),
            "audio_scores": scores,
        }
    except Exception as e:
        print(f"⚠️ Audio inference failed: {e}")
        return {"audio_class": "audio_inference_error", "audio_conf": 0.0, "audio_scores": []}


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
        return {"camera_class": "no_detection", "camera_conf": 0.0, "camera_detections": []}

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
        return {"camera_class": "video_output_unrecognized", "camera_conf": 0.0, "camera_detections": []}

    boxes_xywh = pred[:, :4].astype(np.float32)
    cls_scores = pred[:, 4:4 + num_classes].astype(np.float32)

    cls_ids = np.argmax(cls_scores, axis=1)
    confs = cls_scores[np.arange(len(cls_scores)), cls_ids]

    mask = confs >= VIDEO_CONF_THRESHOLD
    if not np.any(mask):
        return {"camera_class": "no_detection", "camera_conf": 0.0, "camera_detections": []}

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

            if label in VIDEO_IGNORED_CLASSES:
                continue

            final_detections.append(
                {
                    "class": label,
                    "conf": round(score, 4),
                    "bbox": [
                        round(float(box[0]), 2),
                        round(float(box[1]), 2),
                        round(float(box[2]), 2),
                        round(float(box[3]), 2),
                    ],
                }
            )

    final_detections.sort(key=lambda x: x["conf"], reverse=True)
    final_detections = final_detections[:VIDEO_MAX_DETECTIONS]
    if not final_detections:
        return {"camera_class": "no_detection", "camera_conf": 0.0, "camera_detections": []}

    top = final_detections[0]
    return {"camera_class": top["class"], "camera_conf": top["conf"], "camera_detections": final_detections}


def run_video_inference(frame_bgr: np.ndarray) -> Dict[str, Any]:
    if video_interpreter is None or video_input_details is None or video_output_details is None:
        return {"camera_class": "video_model_unavailable", "camera_conf": 0.0, "camera_detections": []}

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
        return {"camera_class": "video_inference_error", "camera_conf": 0.0, "camera_detections": []}


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
            CAMERA_RUNTIME["backend"] = cmd
            print(f"✅ Pi camera backend: {cmd}")
            return
    WORKING_CAMERA_CMD = None
    CAMERA_RUNTIME["backend"] = None
    print("⚠️ No usable Pi camera backend found. OpenCV fallback may still work.")


async def _read_stderr_tail(stream: Optional[asyncio.StreamReader], bucket: Dict[str, Any], key: str) -> None:
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="ignore").strip()
            if text:
                bucket[key] = text[-1000:]
    except Exception:
        pass


async def _spawn_camera_mjpeg() -> asyncio.subprocess.Process:
    if WORKING_CAMERA_CMD is None:
        raise RuntimeError("No rpicam/libcamera backend available.")

    cmd = [
        WORKING_CAMERA_CMD,
        "-t",
        "0",
        "--width",
        str(VIDEO_W),
        "--height",
        str(VIDEO_H),
        "--framerate",
        str(VIDEO_FPS),
        "--rotation",
        "180",
        "--codec",
        "mjpeg",
        "-o",
        "-",
        "--nopreview",
    ]

    print(f"📷 Starting camera: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    CAMERA_RUNTIME["starts"] += 1
    CAMERA_RUNTIME["last_start_ts"] = time.time()
    asyncio.create_task(_read_stderr_tail(proc.stderr, CAMERA_RUNTIME, "stderr_tail"))
    return proc


def encode_bgr_to_jpeg_bytes(frame_bgr: np.ndarray, quality: int = 80) -> Optional[bytes]:
    try:
        ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None
        return buf.tobytes()
    except Exception:
        return None


def rotate_frame_if_needed(frame: np.ndarray) -> np.ndarray:
    if CV2_CAMERA_ROTATE_180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return frame


def prepare_stream_frame(frame_bgr: np.ndarray) -> Optional[bytes]:
    try:
        resized = cv2.resize(frame_bgr, (STREAM_W, STREAM_H), interpolation=cv2.INTER_AREA)
        return encode_bgr_to_jpeg_bytes(resized, quality=max(20, min(95, STREAM_QUALITY)))
    except Exception:
        return None


@dataclass
class CameraState:
    proc: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = None
    last_stream_jpg: Optional[bytes] = None
    last_ai_frame_bgr: Optional[np.ndarray] = None
    last_client_ts: float = 0.0
    last_stream_frame_ts: float = 0.0
    clients: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    backend: str = "none"
    cv_cap: Optional[Any] = None


camera_state = CameraState()


async def update_camera_buffers(frame_bgr: np.ndarray) -> None:
    stream_jpg = prepare_stream_frame(frame_bgr)
    async with camera_state.lock:
        camera_state.last_ai_frame_bgr = frame_bgr.copy()
        if stream_jpg:
            camera_state.last_stream_jpg = stream_jpg
            camera_state.last_stream_frame_ts = time.time()
        camera_state.last_client_ts = time.time()
    CAMERA_RUNTIME["last_frame_ts"] = time.time()


async def _stop_cv2_camera() -> None:
    async with camera_state.lock:
        cap = camera_state.cv_cap
        camera_state.cv_cap = None
        if cap is not None:
            with contextlib.suppress(Exception):
                cap.release()


async def _open_cv2_camera() -> bool:
    cap = cv2.VideoCapture(CV2_CAMERA_INDEX)
    if not cap or not cap.isOpened():
        with contextlib.suppress(Exception):
            if cap:
                cap.release()
        return False

    with contextlib.suppress(Exception):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_H)
        cap.set(cv2.CAP_PROP_FPS, VIDEO_FPS)

    async with camera_state.lock:
        camera_state.cv_cap = cap
        camera_state.backend = "cv2"
        CAMERA_RUNTIME["backend"] = "cv2"

    print(f"✅ OpenCV camera fallback opened: index={CV2_CAMERA_INDEX}")
    return True


async def _stop_camera_if_idle() -> None:
    async with camera_state.lock:
        if camera_state.proc:
            with contextlib.suppress(Exception):
                camera_state.proc.terminate()
            camera_state.proc = None

        cap = camera_state.cv_cap
        camera_state.cv_cap = None
        if cap is not None:
            with contextlib.suppress(Exception):
                cap.release()

        camera_state.last_stream_jpg = None
        camera_state.last_ai_frame_bgr = None
        camera_state.last_client_ts = time.time()
        camera_state.last_stream_frame_ts = 0.0
        camera_state.backend = "none"
        CAMERA_RUNTIME["backend"] = "none"


async def _cv2_camera_loop() -> None:
    print("📷 CV2 camera loop started")
    while True:
        async with camera_state.lock:
            should_keep_running = is_camera_needed() or camera_state.clients > 0
            last_seen = camera_state.last_client_ts
            cap = camera_state.cv_cap

        if not should_keep_running:
            if (time.time() - last_seen) > CAMERA_IDLE_SECONDS:
                await _stop_cv2_camera()
                async with camera_state.lock:
                    camera_state.last_stream_jpg = None
                    camera_state.last_ai_frame_bgr = None
                    camera_state.backend = "none"
                    CAMERA_RUNTIME["backend"] = "none"
                return
            await asyncio.sleep(0.25)
            continue

        if cap is None:
            ok = await _open_cv2_camera()
            if not ok:
                CAMERA_RUNTIME["last_error"] = f"OpenCV camera open failed at index {CV2_CAMERA_INDEX}"
                await asyncio.sleep(1.0)
                continue
            async with camera_state.lock:
                cap = camera_state.cv_cap

        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                await asyncio.sleep(0.05)
                continue

            frame = rotate_frame_if_needed(frame)
            await update_camera_buffers(frame)

            await asyncio.sleep(1.0 / max(VIDEO_FPS, 1))
        except Exception as e:
            CAMERA_RUNTIME["last_error"] = str(e)
            print(f"❌ CV2 camera loop error: {e}")
            await asyncio.sleep(0.25)


async def _camera_reader_loop() -> None:
    soi, eoi = b"\xff\xd8", b"\xff\xd9"
    buf = bytearray()

    while True:
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
                        camera_state.last_stream_jpg = None
                        camera_state.last_ai_frame_bgr = None
                        camera_state.backend = "none"
                        CAMERA_RUNTIME["backend"] = "none"
                return

            await asyncio.sleep(0.25)
            continue

        if proc is None:
            try:
                proc = await _spawn_camera_mjpeg()
                buf.clear()
                async with camera_state.lock:
                    camera_state.proc = proc
                    camera_state.backend = "rpicam"
                    CAMERA_RUNTIME["backend"] = WORKING_CAMERA_CMD or "rpicam"
            except Exception as e:
                CAMERA_RUNTIME["last_error"] = str(e)
                print(f"❌ Failed to start rpicam/libcamera backend: {e}")
                print("↪ Falling back to OpenCV camera")
                await _cv2_camera_loop()
                return

        try:
            chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=3.0)
            if not chunk:
                if proc.returncode is not None:
                    CAMERA_RUNTIME["last_exit_code"] = proc.returncode
                    CAMERA_RUNTIME["last_error"] = f"camera process exited with code {proc.returncode}"
                    print(f"❌ Camera process exited with code {proc.returncode}")
                    async with camera_state.lock:
                        camera_state.proc = None
                        camera_state.last_stream_jpg = None
                        camera_state.last_ai_frame_bgr = None
                        camera_state.backend = "none"
                        CAMERA_RUNTIME["backend"] = "none"
                    print("↪ Falling back to OpenCV camera")
                    await _cv2_camera_loop()
                    return
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

                frame_bgr = decode_jpeg_to_bgr(jpg)
                if frame_bgr is not None:
                    await update_camera_buffers(frame_bgr)

        except asyncio.TimeoutError:
            if proc.returncode is not None:
                CAMERA_RUNTIME["last_exit_code"] = proc.returncode
                CAMERA_RUNTIME["last_error"] = f"camera process exited with code {proc.returncode}"
                print(f"❌ Camera process exited with code {proc.returncode}")
                async with camera_state.lock:
                    camera_state.proc = None
                    camera_state.last_stream_jpg = None
                    camera_state.last_ai_frame_bgr = None
                    camera_state.backend = "none"
                    CAMERA_RUNTIME["backend"] = "none"
                print("↪ Falling back to OpenCV camera")
                await _cv2_camera_loop()
                return
            await asyncio.sleep(0.05)
        except Exception as e:
            CAMERA_RUNTIME["last_error"] = str(e)
            print(f"❌ Camera reader error: {e}")
            await asyncio.sleep(0.05)


async def _ensure_camera_running() -> None:
    async with camera_state.lock:
        camera_state.last_client_ts = time.time()
        if not camera_state.task or camera_state.task.done():
            camera_state.task = asyncio.create_task(_camera_reader_loop())


async def _mjpeg_stream_generator():
    await _ensure_camera_running()

    async with camera_state.lock:
        camera_state.clients += 1
        camera_state.last_client_ts = time.time()

    try:
        wait_started = time.time()
        while is_camera_needed() or camera_state.clients > 0:
            async with camera_state.lock:
                frame = camera_state.last_stream_jpg
                camera_state.last_client_ts = time.time()

            if frame:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            else:
                if time.time() - wait_started > 10:
                    print("⚠️ Still waiting for first camera frame...")
                    wait_started = time.time()

            await asyncio.sleep(1.0 / max(STREAM_FPS, 1))
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


def _audio_device_candidates() -> List[str]:
    seen = set()
    out = []
    for dev in [AUDIO_ARECORD_DEVICE, "default", "plughw:0,0", "hw:0,0"]:
        if dev and dev not in seen:
            seen.add(dev)
            out.append(dev)
    return out


async def _spawn_arecord_for_device(device: str) -> asyncio.subprocess.Process:
    cmd = [
        "arecord",
        "-q",
        "-D",
        device,
        "-f",
        AUDIO_FORMAT,
        "-c",
        str(AUDIO_CHANNELS),
        "-r",
        str(AUDIO_RATE),
        "-t",
        "raw",
    ]

    print(f"🎙️ Starting arecord: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    asyncio.create_task(_read_stderr_tail(proc.stderr, AUDIO_RUNTIME, "stderr_tail"))
    return proc


async def _spawn_arecord() -> asyncio.subprocess.Process:
    global WORKING_AUDIO_DEVICE

    errors = []
    for dev in _audio_device_candidates():
        try:
            proc = await _spawn_arecord_for_device(dev)
            await asyncio.sleep(0.25)
            if proc.returncode is not None:
                errors.append(f"{dev}: exited immediately ({proc.returncode})")
                continue

            WORKING_AUDIO_DEVICE = dev
            AUDIO_RUNTIME["last_start_ts"] = time.time()
            print(f"✅ Audio capture device: {dev}")
            return proc
        except Exception as e:
            errors.append(f"{dev}: {e}")

    raise RuntimeError("No working audio device. " + " | ".join(errors))


async def _broadcast_audio_chunk(chunk: bytes) -> None:
    async with audio_broadcast_lock:
        dead_ws: list[WebSocket] = []
        for client in audio_ws_clients:
            try:
                await client.send_bytes(chunk)
            except Exception:
                dead_ws.append(client)
        for c in dead_ws:
            audio_ws_clients.discard(c)

        dead_q: list[asyncio.Queue] = []
        for q in audio_http_clients:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    q.put_nowait(chunk)
            except Exception:
                dead_q.append(q)
        for q in dead_q:
            audio_http_clients.discard(q)


async def background_audio_analyzer():
    print("🎙️ Background Audio Analyzer started")

    while True:
        proc = None
        try:
            proc = await _spawn_arecord()
            AUDIO_RUNTIME["restarts"] += 1
            rolling = deque(maxlen=AUDIO_WINDOW_SAMPLES)
            last_infer = 0.0

            while True:
                if proc.returncode is not None:
                    AUDIO_RUNTIME["last_exit_code"] = proc.returncode
                    raise RuntimeError(f"arecord exited with code {proc.returncode}")

                chunk = await proc.stdout.read(AUDIO_CHUNK_BYTES)
                if not chunk:
                    AUDIO_RUNTIME["last_exit_code"] = proc.returncode
                    raise RuntimeError(f"audio stream closed; arecord exit={proc.returncode}")

                if len(chunk) < AUDIO_CHUNK_BYTES:
                    continue

                AUDIO_RUNTIME["last_chunk_ts"] = time.time()
                await _broadcast_audio_chunk(chunk)

                samples = np.frombuffer(chunk, dtype=np.int16)
                rolling.extend(samples.tolist())

                now = time.time()
                if len(rolling) < AUDIO_WINDOW_SAMPLES:
                    continue
                if (now - last_infer) < AUDIO_INFER_INTERVAL:
                    continue

                pcm_window = np.array(rolling, dtype=np.int16).tobytes()
                pred = run_audio_inference_from_pcm_bytes(pcm_window)

                audio_class = sanitize_audio_class(pred["audio_class"])
                audio_scores = sanitize_audio_scores(pred["audio_scores"])

                update_audio_state(audio_class)
                flags = compute_audio_flags(audio_class)

                AI_STATUS["audio_class"] = audio_class
                AI_STATUS["audio_conf"] = pred["audio_conf"]
                AI_STATUS["audio_scores"] = audio_scores
                AI_STATUS["audio_alert"] = flags["audio_alert"]
                AI_STATUS["log_only"] = flags["log_only"]
                AI_STATUS["last_audio_analysis_ts"] = now

                if should_trigger_video_from_audio(audio_class):
                    activate_audio_video_trigger(audio_class)
                    await _ensure_camera_running()

                    if should_raise_audio_alert(audio_class) and not MANUAL_VIDEO_VIEW["active"]:
                        activate_alert(audio_class)
                else:
                    deactivate_audio_video_trigger()

                AI_STATUS["colony_state"] = derive_colony_state(
                    AI_STATUS["audio_class"],
                    AI_STATUS["camera_class"],
                )

                sync_alert_status_to_ai()
                last_infer = now

        except Exception as e:
            AUDIO_RUNTIME["last_error"] = str(e)
            print(f"❌ Background audio error: {e}")
            await asyncio.sleep(1)
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
                frame = None if camera_state.last_ai_frame_bgr is None else camera_state.last_ai_frame_bgr.copy()

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
            CAMERA_RUNTIME["last_error"] = str(e)
            print(f"❌ Background video error: {e}")
            await asyncio.sleep(1)


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

    print("✅ Server started. Key routes:")
    print("   GET  /health")
    print("   GET  /api/latest")
    print("   GET  /video.mjpg")
    print("   GET  /audio/stream")
    print("   GET  /api/audio/stream")
    print("   GET  /api/debug/audio")
    print("   GET  /api/debug/camera")


@app.get("/__routes")
async def debug_routes():
    out = []
    for r in app.router.routes:
        methods = sorted(getattr(r, "methods", []) or [])
        path = getattr(r, "path", str(r))
        out.append({"path": path, "methods": methods})
    return {"routes": out}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "has_pi_api_token": bool(PI_API_TOKEN),
        "audio_model": str(AUDIO_MODEL_PATH),
        "video_model": str(VIDEO_MODEL_PATH),
        "video_ai_always_on": VIDEO_AI_ALWAYS_ON,
        "stream_w": STREAM_W,
        "stream_h": STREAM_H,
        "stream_fps": STREAM_FPS,
        "stream_quality": STREAM_QUALITY,
    }


@app.get("/api/debug/audio")
async def api_debug_audio():
    return {
        "working_audio_device": WORKING_AUDIO_DEVICE,
        "runtime": AUDIO_RUNTIME,
        "audio_chunk_bytes": AUDIO_CHUNK_BYTES,
        "audio_rate": AUDIO_RATE,
        "audio_channels": AUDIO_CHANNELS,
        "audio_format": AUDIO_FORMAT,
        "http_clients": len(audio_http_clients),
        "ws_clients": len(audio_ws_clients),
        "audio_state": AUDIO_STATE,
        "audio_video_trigger": AUDIO_VIDEO_TRIGGER,
    }


@app.get("/api/debug/camera")
async def api_debug_camera():
    async with camera_state.lock:
        has_proc = camera_state.proc is not None
        has_stream_frame = camera_state.last_stream_jpg is not None
        has_ai_frame = camera_state.last_ai_frame_bgr is not None
        clients = camera_state.clients
        backend = camera_state.backend

    return {
        "working_camera_cmd": WORKING_CAMERA_CMD,
        "runtime": CAMERA_RUNTIME,
        "manual_video_active": MANUAL_VIDEO_VIEW["active"],
        "alert_active": ALERT_STATE["active"],
        "camera_needed": is_camera_needed(),
        "video_ai_needed": is_video_ai_needed(),
        "video_ai_always_on": VIDEO_AI_ALWAYS_ON,
        "audio_video_trigger_active": AUDIO_VIDEO_TRIGGER["active"],
        "audio_video_trigger_class": AUDIO_VIDEO_TRIGGER["audio_class"],
        "audio_video_triggered_at": AUDIO_VIDEO_TRIGGER["triggered_at"],
        "has_proc": has_proc,
        "has_stream_frame": has_stream_frame,
        "has_ai_frame": has_ai_frame,
        "clients": clients,
        "backend": backend,
        "stream_w": STREAM_W,
        "stream_h": STREAM_H,
        "stream_fps": STREAM_FPS,
        "stream_quality": STREAM_QUALITY,
    }


@app.post("/receive-data")
async def receive_data(payload: EnvPayload, authorization: str | None = Header(default=None)):
    require_bearer(authorization)

    LATEST_ENV.update(
        {
            "temp_c": float(payload.temperature),
            "hum_pct": float(payload.humidity),
            "weight_kg": float(payload.weight),
            "ts": time.time(),
        }
    )
    return {"ok": True}


@app.post("/api/alert/ack")
async def api_ack_alert():
    acknowledge_alert()
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
    activate_manual_video_view()
    await _ensure_camera_running()
    return {"ok": True, "manual_video_active": True}


@app.post("/api/video/close")
async def api_video_close():
    deactivate_manual_video_view()

    if not is_camera_needed():
        await _stop_camera_if_idle()

    return {"ok": True, "manual_video_active": False}


@app.get("/api/latest")
async def api_latest():
    sync_alert_status_to_ai()

    temp = LATEST_ENV.get("temp_c")
    hum = LATEST_ENV.get("hum_pct")
    weight = LATEST_ENV.get("weight_kg")

    return JSONResponse(
        {
            "temp_c": temp if temp is not None else 0.0,
            "hum_pct": hum if hum is not None else 0.0,
            "weight_kg": weight if weight is not None else 0.0,
            "weight": weight if weight is not None else 0.0,
            "audio_class": AI_STATUS.get("audio_class", "normal"),
            "audio_conf": AI_STATUS.get("audio_conf", 0.0),
            "audio_scores": AI_STATUS.get("audio_scores", []),
            "audio_alert": AI_STATUS.get("audio_alert", False),
            "log_only": AI_STATUS.get("log_only", False),
            "audio_first_normal_logged_at": AI_STATUS.get("audio_first_normal_logged_at", 0.0),
            "audio_last_change_ts": AI_STATUS.get("audio_last_change_ts", 0.0),
            "audio_video_trigger_active": AI_STATUS.get("audio_video_trigger_active", False),
            "audio_video_trigger_class": AI_STATUS.get("audio_video_trigger_class"),
            "audio_video_triggered_at": AI_STATUS.get("audio_video_triggered_at", 0.0),
            "camera_class": AI_STATUS.get("camera_class", "idle"),
            "camera_conf": AI_STATUS.get("camera_conf", 0.0),
            "camera_detections": AI_STATUS.get("camera_detections", []),
            "camera_alert": AI_STATUS.get("camera_alert", False),
            "camera_log_only": AI_STATUS.get("camera_log_only", False),
            "audio_device": WORKING_AUDIO_DEVICE,
            "camera_cmd": WORKING_CAMERA_CMD,
            "camera_backend": camera_state.backend,
            "video_ai_always_on": VIDEO_AI_ALWAYS_ON,
            "stream_w": STREAM_W,
            "stream_h": STREAM_H,
            "stream_fps": STREAM_FPS,
            "stream_quality": STREAM_QUALITY,
            "alert_active": ALERT_STATE.get("active", False),
            "alert_source": ALERT_STATE.get("source"),
            "trigger_audio_class": ALERT_STATE.get("trigger_audio_class"),
            "triggered_at": ALERT_STATE.get("triggered_at", 0.0),
            "alert_acknowledged": ALERT_STATE.get("acknowledged", False),
            "alert_acknowledged_at": ALERT_STATE.get("acknowledged_at", 0.0),
            "alert_latched": ALERT_STATE.get("latched", False),
            "manual_video_active": MANUAL_VIDEO_VIEW.get("active", False),
            "manual_video_opened_at": MANUAL_VIDEO_VIEW.get("opened_at", 0.0),
            "colony_state": AI_STATUS.get("colony_state", "normal_activity"),
            "last_audio_analysis_ts": AI_STATUS.get("last_audio_analysis_ts", 0.0),
            "last_video_analysis_ts": AI_STATUS.get("last_video_analysis_ts", 0.0),
            "ts": time.time(),
        }
    )


@app.get("/video.mjpg")
async def video_mjpg():
    if not MANUAL_VIDEO_VIEW["active"] and not ALERT_STATE["active"]:
        activate_manual_video_view()

    await _ensure_camera_running()

    return StreamingResponse(
        _mjpeg_stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def _audio_stream_impl(request: Request, authorization: str | None) -> StreamingResponse:
    require_bearer(authorization)

    q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=AUDIO_HTTP_QUEUE_MAX)

    async with audio_broadcast_lock:
        audio_http_clients.add(q)

    async def gen() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await q.get()
                if chunk:
                    yield chunk
        finally:
            async with audio_broadcast_lock:
                audio_http_clients.discard(q)

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Audio-Format": AUDIO_FORMAT,
            "X-Audio-Rate": str(AUDIO_RATE),
            "X-Audio-Channels": str(AUDIO_CHANNELS),
        },
    )


@app.get("/audio/stream")
async def audio_stream(request: Request, authorization: str | None = Header(default=None)):
    return await _audio_stream_impl(request, authorization)


@app.get("/audio/stream/")
async def audio_stream_slash(request: Request, authorization: str | None = Header(default=None)):
    return await _audio_stream_impl(request, authorization)


@app.get("/api/audio/stream")
async def api_audio_stream(request: Request, authorization: str | None = Header(default=None)):
    return await _audio_stream_impl(request, authorization)


@app.get("/api/audio/stream/")
async def api_audio_stream_slash(request: Request, authorization: str | None = Header(default=None)):
    return await _audio_stream_impl(request, authorization)


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    print("[WS] /ws/audio connect attempt")

    if not ws_require_bearer(ws):
        await ws.accept()
        await ws.close(code=4401)
        return

    await ws.accept()
    print("[WS] /ws/audio accepted")

    async with audio_broadcast_lock:
        audio_ws_clients.add(ws)

    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        print("[WS] /ws/audio disconnected")
    except Exception as e:
        print(f"[WS] Unexpected error: {e}")
    finally:
        async with audio_broadcast_lock:
            audio_ws_clients.discard(ws)


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
