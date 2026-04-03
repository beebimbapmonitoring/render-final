let socket;

const CONFIG = {
  temp: { min: 28.0, max: 32.0 },
  hum: { min: 55, max: 65 },
  weight: 14.5,
};

function normalizeBaseUrl(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

const CONFIG_BASE = normalizeBaseUrl(
  window.BEEROI_CONFIG && window.BEEROI_CONFIG.PROXY_BASE_URL
);

const FUNNEL_ORIGIN = normalizeBaseUrl(
  window.__HIVE_FUNNEL_ORIGIN || localStorage.getItem("hive_funnel_origin") || ""
);

const SAME_ORIGIN = normalizeBaseUrl(window.location.origin || "");

const HTTP_ORIGIN = CONFIG_BASE || FUNNEL_ORIGIN || SAME_ORIGIN;

const WS_ORIGIN = HTTP_ORIGIN
  ? HTTP_ORIGIN.replace(/^https:\/\//, "wss://").replace(/^http:\/\//, "ws://")
  : (window.location.protocol === "https:" ? "wss://" : "ws://") + window.location.host;

const RPI_URL = `${HTTP_ORIGIN}/api/latest`;
const RPI_AUDIO_WS_URL = `${WS_ORIGIN}/ws/audio`;
const RPI_AUDIO_HTTP_URL = `${HTTP_ORIGIN}/audio/stream`;
const RPI_AUDIO_HTTP_API_URL = `${HTTP_ORIGIN}/api/audio/stream`;
const RPI_VIDEO_URL = `${HTTP_ORIGIN}/video.mjpg`;
const RPI_VIDEO_OPEN_URL = `${HTTP_ORIGIN}/api/video/open`;
const RPI_VIDEO_CLOSE_URL = `${HTTP_ORIGIN}/api/video/close`;
const RPI_ALERT_ACK_URL = `${HTTP_ORIGIN}/api/alert/ack`;
const RPI_ALERT_RESET_URL = `${HTTP_ORIGIN}/api/alert/reset`;

const HIVE_TARE_WEIGHT = 2.0;

const AUDIO_STREAM_FORMAT = {
  sampleRate: 16000,
  channels: 1,
  bitDepth: 16,
  format: "S16_LE",
};

const AUDIO_VIZ = {
  zoomY: 1.25,
  zoomX: 1.0,
  showAxes: true,
  showGrid: true,
};

function _safeJsonParse(value, fallback = null) {
  try {
    if (value == null || value === "") return fallback;
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function getApiToken() {
  const token =
    (window.BEEROI_CONFIG && String(window.BEEROI_CONFIG.PI_API_TOKEN || "").trim()) ||
    (window.__HIVE_PI_API_TOKEN && String(window.__HIVE_PI_API_TOKEN).trim()) ||
    (localStorage.getItem("hive_pi_api_token") || "").trim();
  return token || "";
}

function buildAuthHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getApiToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function apiFetch(url, options = {}) {
  const mergedHeaders = buildAuthHeaders(options.headers || {});
  return fetch(url, { ...options, headers: mergedHeaders });
}

async function apiPost(url, body) {
  const options = {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  };
  return apiFetch(url, options);
}

let SETTINGS = {
  refreshRate: 2000,
  sensitivity: 8,
  audioEnabled: true,
  chartHistory: 20,
  autoOpenVideoOnAlert: true,
  audioZoomX: 1.0,
  audioZoomY: 1.25,
  audioShowAxes: true,
  audioShowGrid: true,
  use24h: true,
};

let hiveChart;
let detailedChart;
let updateIntervalId;
let alarmInterval = null;
let specAnimationId = null;
let activeOscillators = [];

let tempHistory = [];
let humHistory = [];
let weightHistory = [];
let timeLabels = [];
let allLogs = [];
let audioLogs = [];
let videoLogs = [];
let MAX_HISTORY = 20;

let currentTemp = 0;
let currentHum = 0;
let currentWeight = 0;

let audioContext = null;
let audioSocket = null;
let audioPingInterval = null;
let isListening = false;
let audioMasterGain = null;
let audioNextPlayTime = 0;
let audioWaveData = null;
let audioDrawMode = "waiting";

let audioFetchController = null;
let audioFetchRunning = false;
let audioReader = null;
let activeAudioStreamUrl = null;
let audioStopRequested = false;
let audioReconnectTimer = null;
let audioReconnectAttempts = 0;
let audioDesired = false;

let latestAudioClass = "normal";
let latestAudioConf = 0;
let latestAudioScores = [];
let latestCameraClass = "idle";
let latestCameraConf = 0;
let latestCameraDetections = [];
let latestColonyState = "normal_activity";
let latestAudioAlert = false;
let latestLogOnly = false;
let latestCameraAlert = false;
let latestCameraLogOnly = false;
let latestLastAudioTs = 0;
let latestLastVideoTs = 0;

let backendAlertActive = false;
let backendAlertAcknowledged = false;
let backendAlertLatched = false;
let backendAlertSource = null;
let backendTriggerAudioClass = null;
let backendTriggeredAt = 0;
let backendManualVideoActive = false;
let backendAudioVideoTriggerActive = false;
let backendAudioVideoTriggerClass = null;
let backendAudioVideoTriggeredAt = 0;
let backendCameraBackend = "--";
let backendVideoAiAlwaysOn = false;

let isFetchingLatest = false;
let lastAlertKey = "";
let alertAcknowledged = false;
let lastResetUiTs = 0;

const AUDIO_RING_SECONDS = 12;
let audioRing = null;
let audioRingWrite = 0;
let audioRingFilled = false;

let WAVE_VIEW_SECONDS = 5.0;
let WAVE_GAIN = 1.0;

let lastVideoLogKey = "";
let lastAudioLogKey = "";

const DEFAULT_PROFILE = {
  name: "Admin",
  role: "Head Keeper",
  credentials: "Authorized Personnel",
  avatar: "https://api.dicebear.com/7.x/avataaars/svg?seed=Felix",
};

function formatClock(dateObj = new Date(), withSeconds = false) {
  const options = SETTINGS.use24h
    ? { hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}), hour12: false }
    : { hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}), hour12: true };
  return dateObj.toLocaleTimeString([], options);
}

function _ensureAudioRing() {
  const sr = AUDIO_STREAM_FORMAT.sampleRate || 16000;
  const capacity = Math.max(1, Math.floor(sr * AUDIO_RING_SECONDS));
  if (!audioRing || audioRing.length !== capacity) {
    audioRing = new Float32Array(capacity);
    audioRingWrite = 0;
    audioRingFilled = false;
  }
}

function _clearAudioRing() {
  _ensureAudioRing();
  audioRing.fill(0);
  audioRingWrite = 0;
  audioRingFilled = false;
}

function _pushToAudioRing(monoFloat32) {
  _ensureAudioRing();
  const cap = audioRing.length;
  for (let i = 0; i < monoFloat32.length; i++) {
    audioRing[audioRingWrite] = monoFloat32[i];
    audioRingWrite++;
    if (audioRingWrite >= cap) {
      audioRingWrite = 0;
      audioRingFilled = true;
    }
  }
}

function applyAudioStreamHeaders(headers) {
  if (!headers) return;

  const rate = Number(headers.get("X-Audio-Rate") || headers.get("x-audio-rate") || AUDIO_STREAM_FORMAT.sampleRate);
  const channels = Number(headers.get("X-Audio-Channels") || headers.get("x-audio-channels") || AUDIO_STREAM_FORMAT.channels);
  const format = String(headers.get("X-Audio-Format") || headers.get("x-audio-format") || AUDIO_STREAM_FORMAT.format || "S16_LE");

  if (Number.isFinite(rate) && rate > 0) AUDIO_STREAM_FORMAT.sampleRate = rate;
  if (Number.isFinite(channels) && channels > 0) AUDIO_STREAM_FORMAT.channels = channels;
  AUDIO_STREAM_FORMAT.format = format;

  _ensureAudioRing();
  updateAudioParamLabels();
}

function loadSettings() {
  const saved = _safeJsonParse(localStorage.getItem("hive_settings"), null);
  if (saved && typeof saved === "object") SETTINGS = { ...SETTINGS, ...saved };

  MAX_HISTORY = Number(SETTINGS.chartHistory) || 20;

  const mapping = [
    ["setting-refresh", SETTINGS.refreshRate],
    ["setting-sensitivity", SETTINGS.sensitivity],
    ["setting-history", SETTINGS.chartHistory],
    ["setting-audio-zoom-x", SETTINGS.audioZoomX],
    ["setting-audio-zoom-y", SETTINGS.audioZoomY],
  ];

  mapping.forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.value = value;
  });

  const checks = [
    ["setting-audio", SETTINGS.audioEnabled],
    ["setting-auto-video", SETTINGS.autoOpenVideoOnAlert],
    ["setting-audio-axes", SETTINGS.audioShowAxes],
    ["setting-audio-grid", SETTINGS.audioShowGrid],
    ["setting-24h", SETTINGS.use24h],
  ];

  checks.forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.checked = !!value;
  });

  AUDIO_VIZ.zoomX = Number(SETTINGS.audioZoomX) || 1.0;
  AUDIO_VIZ.zoomY = Number(SETTINGS.audioZoomY) || 1.25;
  AUDIO_VIZ.showAxes = !!SETTINGS.audioShowAxes;
  AUDIO_VIZ.showGrid = !!SETTINGS.audioShowGrid;
}

function applyAudioSettingsToModal() {
  const zx = document.getElementById("audio-zoom-x");
  const zy = document.getElementById("audio-zoom-y");
  const ax = document.getElementById("audio-show-axes");
  const gr = document.getElementById("audio-show-grid");

  if (zx) zx.value = SETTINGS.audioZoomX;
  if (zy) zy.value = SETTINGS.audioZoomY;
  if (ax) ax.checked = SETTINGS.audioShowAxes;
  if (gr) gr.checked = SETTINGS.audioShowGrid;

  _readAudioVizFromUI();
  updateAudioParamLabels();
}

function saveSettings() {
  const refreshEl = document.getElementById("setting-refresh");
  const audioEl = document.getElementById("setting-audio");
  const sensitivityEl = document.getElementById("setting-sensitivity");
  const historyEl = document.getElementById("setting-history");
  const autoVideoEl = document.getElementById("setting-auto-video");
  const audioZoomXEl = document.getElementById("setting-audio-zoom-x");
  const audioZoomYEl = document.getElementById("setting-audio-zoom-y");
  const audioAxesEl = document.getElementById("setting-audio-axes");
  const audioGridEl = document.getElementById("setting-audio-grid");
  const use24hEl = document.getElementById("setting-24h");

  if (refreshEl) SETTINGS.refreshRate = parseInt(refreshEl.value, 10);
  if (audioEl) SETTINGS.audioEnabled = audioEl.checked;
  if (sensitivityEl) SETTINGS.sensitivity = parseInt(sensitivityEl.value, 10);
  if (historyEl) SETTINGS.chartHistory = parseInt(historyEl.value, 10);
  if (autoVideoEl) SETTINGS.autoOpenVideoOnAlert = autoVideoEl.checked;
  if (audioZoomXEl) SETTINGS.audioZoomX = parseFloat(audioZoomXEl.value);
  if (audioZoomYEl) SETTINGS.audioZoomY = parseFloat(audioZoomYEl.value);
  if (audioAxesEl) SETTINGS.audioShowAxes = audioAxesEl.checked;
  if (audioGridEl) SETTINGS.audioShowGrid = audioGridEl.checked;
  if (use24hEl) SETTINGS.use24h = use24hEl.checked;

  MAX_HISTORY = Number(SETTINGS.chartHistory) || 20;

  AUDIO_VIZ.zoomX = SETTINGS.audioZoomX;
  AUDIO_VIZ.zoomY = SETTINGS.audioZoomY;
  AUDIO_VIZ.showAxes = SETTINGS.audioShowAxes;
  AUDIO_VIZ.showGrid = SETTINGS.audioShowGrid;

  localStorage.setItem("hive_settings", JSON.stringify(SETTINGS));
  applyAudioSettingsToModal();
  startDataUpdates();
  updateCamTime();
  alert("✅ Settings Saved!");
}

function resetSettings() {
  localStorage.removeItem("hive_settings");
  location.reload();
}

function loadProfile() {
  const saved = _safeJsonParse(localStorage.getItem("hive_profile"), DEFAULT_PROFILE) || DEFAULT_PROFILE;
  const greetingText = document.getElementById("greeting-text");
  const locationDisplay = document.getElementById("location-display");
  if (greetingText) greetingText.innerText = `Hello, ${saved.name.split(" ")[0]}! 👋`;
  if (locationDisplay) locationDisplay.innerText = saved.credentials || "Authorized Personnel";
}

function updateCamTime() {
  const el = document.getElementById("cam-time");
  if (el) el.innerText = formatClock(new Date(), true);
}

function createSwarm(container, className, count) {
  if (!container) return;
  for (let i = 0; i < count; i++) {
    const b = document.createElement("div");
    b.className = className;
    b.style.left = Math.random() * 100 + "vw";
    b.style.top = Math.random() * 100 + "vh";
    b.style.animation = `flyAround ${5 + Math.random() * 15}s infinite linear`;
    container.appendChild(b);
  }
}

function exportData() {
  if (allLogs.length === 0) return alert("⚠️ No logs to export!");

  let csvContent = "Timestamp,Event,Value,Status\n";
  allLogs.forEach((log) => {
    csvContent += `"${log.timestamp}","${log.eventType}","${log.value}","${log.status}"\n`;
  });

  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.setAttribute("href", url);
  link.setAttribute("download", `hive_data_${new Date().toISOString().slice(0, 10)}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function updateAudioButtonState(listening) {
  const btnText = document.getElementById("audio-btn-text");
  const audioIcon = document.getElementById("audio-icon");
  const statusLabel = document.getElementById("audio-level-label");

  if (btnText) btnText.innerText = listening ? "STOP LIVE" : "LISTEN LIVE";
  if (audioIcon) audioIcon.className = listening ? "fas fa-volume-up" : "fas fa-volume-mute";
  if (statusLabel && !listening) statusLabel.innerText = "Status: Ready";
}

function updateAudioCardStatus(text, color = "") {
  const audioDisplay = document.getElementById("audio-display");
  if (!audioDisplay) return;
  audioDisplay.innerText = text;
  audioDisplay.style.color = color;
}

function updateAudioBadge(text, extraClass = "") {
  const badge = document.getElementById("audio-badge");
  if (!badge) return;
  badge.className = `badge ${extraClass}`.trim();
  badge.innerText = text;
}

function updateCameraCardStatus(text, color = "") {
  const camStatus = document.querySelector(".card-cam .status-text");
  if (!camStatus) return;
  camStatus.innerText = text;
  camStatus.style.color = color;
}

function updateCameraBadge(text, extraClass = "") {
  const badge = document.getElementById("cam-badge");
  if (!badge) return;
  badge.className = `badge ${extraClass}`.trim();
  badge.innerText = text;
}

function normalizeAudioClass(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "intruded" || raw === "intrusion" || raw === "aggressive") return "intruded";
  if (raw === "swarming" || raw === "swarm" || raw === "ready_for_split") return "swarming";
  if (raw === "queenless") return "queenless";
  if (raw === "queenright") return "queenright";
  if (raw === "false") return "false";
  if (raw === "stress") return "stress";
  return "normal";
}

function normalizeCameraClass(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "human" || raw === "person" || raw === "hand") return "person";
  if (["vertebrate", "animal", "lizard", "frog", "bird", "gecko", "rodent"].includes(raw)) return "vertebrate";
  if (["other_insect", "other insect", "insect", "ant", "wasp", "fly", "beetle", "moth"].includes(raw)) return "other_insect";
  if (raw === "tb_cluster") return "tb_cluster";
  if (raw === "t_biroi" || raw === "tetragonula biroi") return "t_biroi";
  if (raw === "hive_entrance" || raw === "entrance") return "hive_entrance";
  if (raw === "no_detection") return "no_detection";
  if (raw === "idle") return "idle";
  return raw || "bee";
}

function prettyAudioClass(label) {
  const normalized = normalizeAudioClass(label);
  if (normalized === "intruded") return "Intruded";
  if (normalized === "swarming") return "Swarming";
  if (normalized === "queenless") return "Queenless";
  if (normalized === "queenright") return "Queenright";
  if (normalized === "false") return "False Trigger";
  if (normalized === "stress") return "Stress";
  return "Normal";
}

function prettyCameraClass(label) {
  const normalized = normalizeCameraClass(label);
  if (normalized === "other_insect") return "Other Insect";
  if (normalized === "vertebrate") return "Vertebrate";
  if (normalized === "person") return "Person";
  if (normalized === "tb_cluster") return "TB Cluster";
  if (normalized === "t_biroi") return "T. biroi";
  if (normalized === "hive_entrance") return "Hive Entrance";
  if (normalized === "no_detection") return "No Detection";
  if (normalized === "idle") return "Idle";
  if (normalized === "bee") return "Bee";
  return normalized.replaceAll("_", " ").replace(/\b\w/g, (m) => m.toUpperCase());
}

function getAudioColor(label) {
  const normalized = normalizeAudioClass(label);
  if (normalized === "intruded") return "#ff4757";
  if (normalized === "swarming") return "#f7b731";
  if (["queenless", "queenright", "false", "stress"].includes(normalized)) return "#9b59b6";
  return "#2ecc71";
}

function getCameraColor(label) {
  const normalized = normalizeCameraClass(label);
  if (normalized === "person" || normalized === "other_insect") return "#ff4757";
  if (["tb_cluster", "t_biroi", "vertebrate"].includes(normalized)) return "#9b59b6";
  if (normalized === "idle") return "#95a5a6";
  return "#2ecc71";
}

function prettyColonyState(state) {
  return String(state || "normal_activity")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (m) => m.toUpperCase());
}

function formatPercent(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v)) return "0%";
  return `${(v * 100).toFixed(0)}%`;
}

function formatTimestamp(ts) {
  const n = Number(ts || 0);
  if (!n) return "Waiting for analysis...";
  return `Last analysis: ${new Date(n * 1000).toLocaleString()}`;
}

function updateResetAlertButtonState() {
  const indicator = document.getElementById("reset-alert-indicator");
  const indicatorText = document.getElementById("reset-alert-indicator-text");
  const buttonText = document.getElementById("reset-alert-btn-text");

  if (!indicator || !indicatorText || !buttonText) return;

  indicator.classList.remove("reset-idle", "reset-ready", "reset-latched", "reset-working");

  const justReset = Date.now() - lastResetUiTs < 2500;

  if (justReset) {
    indicator.classList.add("reset-working");
    indicatorText.innerText = "Reset sent";
    buttonText.innerText = "Resetting...";
    return;
  }

  if (backendAlertLatched || backendAlertAcknowledged) {
    indicator.classList.add("reset-latched");
    indicatorText.innerText = "Reset needed";
    buttonText.innerText = "Reset Alert";
    return;
  }

  if (backendAlertActive) {
    indicator.classList.add("reset-ready");
    indicatorText.innerText = "Alert active";
    buttonText.innerText = "Reset Alert";
    return;
  }

  indicator.classList.add("reset-idle");
  indicatorText.innerText = "Idle";
  buttonText.innerText = "Reset Alert";
}

function updateBackendStateCards() {
  const alertState = document.getElementById("backend-alert-state");
  const alertMeta = document.getElementById("backend-alert-meta");
  const avState = document.getElementById("audio-video-trigger-state");
  const avMeta = document.getElementById("audio-video-trigger-meta");
  const camState = document.getElementById("camera-backend-state");
  const camMeta = document.getElementById("camera-backend-meta");

  if (alertState) {
    if (backendAlertLatched || backendAlertAcknowledged) alertState.innerText = "Acknowledged / Latched";
    else if (backendAlertActive) alertState.innerText = "Active";
    else alertState.innerText = "Idle";
  }

  if (alertMeta) {
    if (backendAlertLatched || backendAlertAcknowledged) {
      alertMeta.innerText = "No alarm will retrigger until reset";
    } else if (backendAlertActive) {
      alertMeta.innerText = `Source: ${backendAlertSource || "unknown"} • Trigger: ${backendTriggerAudioClass || "n/a"}`;
    } else {
      alertMeta.innerText = "No active backend alert";
    }
  }

  if (avState) avState.innerText = backendAudioVideoTriggerActive ? "Active" : "Inactive";
  if (avMeta) {
    avMeta.innerText = backendAudioVideoTriggerActive
      ? `Class: ${backendAudioVideoTriggerClass || "unknown"}`
      : "Waiting for audio trigger";
  }

  if (camState) camState.innerText = backendCameraBackend || "--";
  if (camMeta) camMeta.innerText = backendVideoAiAlwaysOn ? "Video AI always on" : "Video AI conditional";
}

function applyAIStatuses(audioClass, cameraClass, colonyState = "", audioAlert = false, logOnly = false, cameraAlert = false, cameraLogOnly = false) {
  const normalizedAudio = normalizeAudioClass(audioClass);
  const normalizedCamera = normalizeCameraClass(cameraClass);

  latestAudioClass = normalizedAudio;
  latestCameraClass = normalizedCamera;
  latestColonyState = colonyState || latestColonyState;
  latestAudioAlert = !!audioAlert;
  latestLogOnly = !!logOnly;
  latestCameraAlert = !!cameraAlert;
  latestCameraLogOnly = !!cameraLogOnly;

  if (!isListening) updateAudioCardStatus(prettyAudioClass(normalizedAudio), getAudioColor(normalizedAudio));
  updateCameraCardStatus(prettyCameraClass(normalizedCamera), getCameraColor(normalizedCamera));

  if (backendAlertLatched || backendAlertAcknowledged) updateAudioBadge("Ack Latched", "badge-logonly");
  else if (latestAudioAlert) updateAudioBadge("Alert", "badge-warning");
  else if (latestLogOnly) updateAudioBadge("Logs Only", "badge-logonly");
  else updateAudioBadge(isListening ? "Listening" : "Ready", "");

  if (backendAlertLatched || backendAlertAcknowledged) {
    updateCameraBadge("Ack Latched", "badge-logonly");
  } else if (latestCameraAlert) {
    updateCameraBadge("● Alert", "badge-warning blink-red");
  } else if (latestCameraLogOnly) {
    updateCameraBadge("Logs Only", "badge-logonly");
  } else if (normalizedCamera === "no_detection" || normalizedCamera === "idle") {
    updateCameraBadge("Watching", "");
  } else {
    updateCameraBadge("● Live", "blink-red");
  }

  const colonyStateDisplay = document.getElementById("colony-state-display");
  const colonyStatePill = document.getElementById("colony-state-pill");
  const analysisTimeDisplay = document.getElementById("analysis-time-display");
  const audioAiTop = document.getElementById("audio-ai-top");
  const audioAiNote = document.getElementById("audio-ai-note");
  const audioAlertPill = document.getElementById("audio-alert-pill");
  const videoAiTop = document.getElementById("video-ai-top");
  const videoAiNote = document.getElementById("video-ai-note");
  const videoAlertPill = document.getElementById("video-alert-pill");

  if (colonyStateDisplay) colonyStateDisplay.innerText = prettyColonyState(latestColonyState);
  if (colonyStatePill) colonyStatePill.innerText = latestColonyState || "normal_activity";
  if (analysisTimeDisplay) analysisTimeDisplay.innerText = formatTimestamp(Math.max(latestLastAudioTs, latestLastVideoTs));
  if (audioAiTop) audioAiTop.innerText = `${prettyAudioClass(latestAudioClass)} • ${formatPercent(latestAudioConf)}`;
  if (audioAiNote) {
    audioAiNote.innerText = backendAlertLatched
      ? "Alert acknowledged. Reset required before next alarm"
      : latestAudioAlert
        ? "Alert-worthy acoustic pattern detected"
        : latestLogOnly
          ? "Acoustic event logged without alarm"
          : "Always-on acoustic monitoring active";
  }
  if (audioAlertPill) {
    audioAlertPill.innerText = backendAlertLatched ? "Latched" : latestAudioAlert ? "Alert" : latestLogOnly ? "Log Only" : "No Alert";
  }

  if (videoAiTop) videoAiTop.innerText = `${prettyCameraClass(latestCameraClass)} • ${formatPercent(latestCameraConf)}`;
  if (videoAiNote) {
    if (backendAlertLatched) videoAiNote.innerText = "Alert latched. Reset required for next alarm";
    else if (latestCameraAlert) videoAiNote.innerText = "Alert + log triggered by video detection";
    else if (latestCameraLogOnly) videoAiNote.innerText = "Video detection logged without alarm";
    else if (latestCameraDetections.length) videoAiNote.innerText = `${latestCameraDetections.length} object(s) detected at entrance`;
    else videoAiNote.innerText = "Entrance object detection standby";
  }
  if (videoAlertPill) {
    videoAlertPill.innerText = backendAlertLatched ? "Latched" : latestCameraAlert ? "Alert" : latestCameraLogOnly ? "Log Only" : "Watching";
  }

  updateAudioModalStatus();
  updateVideoModalStatus();
  updateBackendStateCards();
  updateResetAlertButtonState();
}

function getHighestCameraDetection(detections) {
  if (!Array.isArray(detections) || !detections.length) return null;
  return [...detections].sort((a, b) => Number(b.conf || 0) - Number(a.conf || 0))[0];
}

function updateAudioModalStatus() {
  const topClass = document.getElementById("audio-modal-top-class");
  const topConf = document.getElementById("audio-modal-top-conf");
  const alertMode = document.getElementById("audio-modal-alert-mode");
  const colonyNote = document.getElementById("audio-modal-colony-note");
  const scoreList = document.getElementById("audio-score-list");

  if (topClass) topClass.innerText = prettyAudioClass(latestAudioClass);
  if (topConf) topConf.innerText = `Confidence: ${formatPercent(latestAudioConf)}`;
  if (alertMode) {
    alertMode.innerText = backendAlertLatched ? "Latched / Ack" : latestAudioAlert ? "Alert" : latestLogOnly ? "Log Only" : "No Alert";
  }
  if (colonyNote) colonyNote.innerText = `Colony state: ${latestColonyState || "normal_activity"}`;

  if (!scoreList) return;

  if (!latestAudioScores.length) {
    scoreList.innerHTML = '<div class="empty-detection">Waiting for audio classification...</div>';
    return;
  }

  scoreList.innerHTML = latestAudioScores
    .map((item) => {
      const conf = Number(item.conf || 0);
      return `
        <div class="score-item">
          <div class="score-head">
            <span>${prettyAudioClass(item.class)}</span>
            <strong>${formatPercent(conf)}</strong>
          </div>
          <div class="score-bar">
            <div class="score-fill" style="width:${Math.max(0, Math.min(100, conf * 100))}%"></div>
          </div>
        </div>
      `;
    })
    .join("");
}

function updateVideoModalStatus() {
  const topDetection = document.getElementById("video-top-detection");
  const topMeta = document.getElementById("video-top-meta");
  const detectionList = document.getElementById("video-detection-list");

  const top = getHighestCameraDetection(latestCameraDetections);

  if (topDetection) topDetection.innerText = top ? prettyCameraClass(top.class) : prettyCameraClass(latestCameraClass);

  const metaParts = [];
  metaParts.push(`Confidence: ${top ? formatPercent(top.conf) : formatPercent(latestCameraConf)}`);
  if (backendAlertLatched) metaParts.push("Mode: Latched");
  else if (latestCameraAlert) metaParts.push("Mode: Alert + Log");
  else if (latestCameraLogOnly) metaParts.push("Mode: Log Only");
  else metaParts.push("Mode: Monitor");

  if (topMeta) topMeta.innerText = metaParts.join(" • ");

  if (!detectionList) return;

  if (!latestCameraDetections.length) {
    detectionList.innerHTML = '<div class="empty-detection">Waiting for detections...</div>';
    return;
  }

  detectionList.innerHTML = latestCameraDetections
    .map((det) => {
      const bbox = Array.isArray(det.bbox) ? det.bbox.join(", ") : "--";
      const cls = normalizeCameraClass(det.class);
      const mode =
        cls === "person" || cls === "other_insect"
          ? "Alert + Log"
          : cls === "tb_cluster" || cls === "t_biroi" || cls === "vertebrate"
            ? "Log Only"
            : "Monitor";
      return `
        <div class="detection-chip">
          <div class="det-main">
            <strong>${prettyCameraClass(det.class)}</strong>
            <span>${formatPercent(det.conf)}</span>
          </div>
          <small>${mode} • BBox: ${bbox}</small>
        </div>
      `;
    })
    .join("");
}

function updateAudioParamLabels() {
  const rate = document.getElementById("audio-param-rate");
  const channels = document.getElementById("audio-param-channels");
  const bitdepth = document.getElementById("audio-param-bitdepth");
  const view = document.getElementById("audio-param-view");

  if (rate) rate.innerText = `${AUDIO_STREAM_FORMAT.sampleRate} Hz`;
  if (channels) channels.innerText = `${AUDIO_STREAM_FORMAT.channels}`;
  if (bitdepth) bitdepth.innerText = `${AUDIO_STREAM_FORMAT.bitDepth}-bit`;
  if (view) view.innerText = `${WAVE_VIEW_SECONDS.toFixed(1)}s`;
}

function _readAudioVizFromUI() {
  const zy = document.getElementById("audio-zoom-y");
  const zx = document.getElementById("audio-zoom-x");
  const ax = document.getElementById("audio-show-axes");
  const gr = document.getElementById("audio-show-grid");

  if (zy) {
    const v = Number(zy.value);
    if (!Number.isNaN(v) && v > 0) AUDIO_VIZ.zoomY = v;
  }
  if (zx) {
    const v = Number(zx.value);
    if (!Number.isNaN(v) && v > 0) AUDIO_VIZ.zoomX = v;
  }
  if (ax) AUDIO_VIZ.showAxes = !!ax.checked;
  if (gr) AUDIO_VIZ.showGrid = !!gr.checked;

  SETTINGS.audioZoomX = AUDIO_VIZ.zoomX;
  SETTINGS.audioZoomY = AUDIO_VIZ.zoomY;
  SETTINGS.audioShowAxes = AUDIO_VIZ.showAxes;
  SETTINGS.audioShowGrid = AUDIO_VIZ.showGrid;
  localStorage.setItem("hive_settings", JSON.stringify(SETTINGS));
}

function hexToRgba(hex, alpha = 0.15) {
  if (!hex || !hex.startsWith("#")) return `rgba(255, 179, 0, ${alpha})`;
  let c = hex.replace("#", "");
  if (c.length === 3) c = c.split("").map((ch) => ch + ch).join("");
  const num = parseInt(c, 16);
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function initChart() {
  const canvas = document.getElementById("hiveChart");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  hiveChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Temp", data: [], borderColor: "#ff7675", tension: 0.4, pointRadius: 0, yAxisID: "y" },
        { label: "Hum", data: [], borderColor: "#74b9ff", tension: 0.4, pointRadius: 0, yAxisID: "y" },
        { label: "Weight", data: [], borderColor: "#ffeaa7", tension: 0.4, pointRadius: 0, yAxisID: "y1" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: true, position: "bottom", labels: { boxWidth: 10 } } },
      scales: {
        x: { display: false },
        y: { display: false, position: "left" },
        y1: { display: false, position: "right", grid: { drawOnChartArea: false } },
      },
      animation: false,
    },
  });
}

function openDetailedGraphOnCardClick(type) {
  const modal = document.getElementById("detailed-graph-modal");
  if (!modal) return;
  modal.classList.remove("hidden");

  let title;
  let color;
  let dataArray;

  if (type === "temp") {
    title = "Temperature Trend";
    color = "#ff7675";
    dataArray = tempHistory;
  } else if (type === "humidity") {
    title = "Humidity Trend";
    color = "#74b9ff";
    dataArray = humHistory;
  } else if (type === "weight") {
    title = "Weight Trend";
    color = "#ffeaa7";
    dataArray = weightHistory;
  } else {
    return;
  }

  const titleEl = document.getElementById("detailed-graph-title");
  if (titleEl) titleEl.innerText = title;

  if (detailedChart) detailedChart.destroy();

  const canvas = document.getElementById("detailedChart");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  detailedChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: timeLabels,
      datasets: [
        {
          label: title,
          data: dataArray,
          borderColor: color,
          backgroundColor: hexToRgba(color, 0.15),
          fill: true,
          tension: 0.4,
          pointRadius: 4,
          pointBackgroundColor: color,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: false } },
    },
  });

  updateDetailedStats(dataArray);
}

function updateDetailedChart(dataArray, labels) {
  if (!detailedChart) return;
  detailedChart.data.labels = labels;
  detailedChart.data.datasets[0].data = dataArray;
  detailedChart.update("none");
  updateDetailedStats(dataArray);
}

function updateDetailedStats(dataArray) {
  if (!dataArray || dataArray.length === 0) return;
  const nums = dataArray.map((n) => parseFloat(n)).filter((n) => !isNaN(n));
  if (!nums.length) return;

  const sum = nums.reduce((a, b) => a + b, 0);
  const avg = (sum / nums.length).toFixed(2);

  const map = {
    "graph-current": nums[nums.length - 1].toFixed(2),
    "graph-max": Math.max(...nums).toFixed(2),
    "graph-min": Math.min(...nums).toFixed(2),
    "graph-avg": avg,
  };

  Object.entries(map).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.innerText = value;
  });
}

function closeDetailedGraphModal() {
  document.getElementById("detailed-graph-modal")?.classList.add("hidden");
}

function pushAudioLog(eventType, stressLevel = "Normal") {
  const key = `${eventType}|${stressLevel}`;
  if (key === lastAudioLogKey) return;
  lastAudioLogKey = key;
  setTimeout(() => {
    if (lastAudioLogKey === key) lastAudioLogKey = "";
  }, 1500);

  const entry = { timestamp: new Date().toLocaleString(), eventType, stressLevel };
  audioLogs.unshift(entry);
  if (audioLogs.length > 20) audioLogs.pop();

  const audioModal = document.getElementById("audio-modal");
  if (audioModal && !audioModal.classList.contains("hidden")) populateAudioLogs();
}

function pushVideoLog(eventType, status = "Normal") {
  const key = `${eventType}|${status}`;
  if (key === lastVideoLogKey) return;
  lastVideoLogKey = key;
  setTimeout(() => {
    if (lastVideoLogKey === key) lastVideoLogKey = "";
  }, 1500);

  const entry = { timestamp: new Date().toLocaleString(), eventType, status };
  videoLogs.unshift(entry);
  if (videoLogs.length > 20) videoLogs.pop();
  populateVideoLogs();
}

function populateVideoLogs() {
  const container = document.getElementById("video-events-list");
  if (!container) return;

  container.innerHTML = "";

  const liveItem = document.createElement("div");
  liveItem.className = "log-item active";
  liveItem.innerHTML = "<span>Live</span>";
  liveItem.onclick = () => playVideo(liveItem, "LIVE FEED");
  container.appendChild(liveItem);

  if (!videoLogs.length) return;

  videoLogs.forEach((log) => {
    const div = document.createElement("div");
    div.className = "log-item";
    div.innerHTML = `
      <div style="font-weight:700; margin-bottom:4px;">${log.eventType}</div>
      <div style="font-size:11px; opacity:0.75;">${log.timestamp} • ${log.status}</div>
    `;
    container.appendChild(div);
  });
}

function populateAudioLogs() {
  const container = document.getElementById("audio-events-list");
  if (!container) return;

  container.innerHTML = "";

  if (!audioLogs.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;">No audio events recorded</div>';
    return;
  }

  audioLogs.forEach((log) => {
    const div = document.createElement("div");
    div.className = "audio-event-item";
    div.innerHTML = `
      <div class="timestamp">${log.timestamp}</div>
      <div class="event-type">
        <span>${log.eventType}</span>
        <span class="stress-indicator ${log.stressLevel === "Normal" ? "stress-normal" : "stress-high"}">${log.stressLevel}</span>
      </div>
    `;
    container.appendChild(div);
  });
}

function playVideo(el, label) {
  console.log("Selected video item:", label || "LIVE FEED");
}

function deleteVideoLogs() {
  videoLogs = [];
  populateVideoLogs();
}

function deleteAudioLogs() {
  audioLogs = [];
  populateAudioLogs();
}

function _extractTempHumWeight(data) {
  const temp = data?.temp_c ?? data?.temp ?? data?.temperature ?? null;
  const hum = data?.hum_pct ?? data?.hum ?? data?.humidity ?? null;
  let weight = data?.weight_kg ?? data?.weight ?? null;
  const weight_g = data?.weight_g ?? null;

  if ((weight === null || weight === undefined) && weight_g !== null && weight_g !== undefined) {
    const g = Number(weight_g);
    if (!Number.isNaN(g)) weight = g / 1000.0;
  }
  return { temp, hum, weight };
}

async function updateData() {
  if (isFetchingLatest) return;
  isFetchingLatest = true;

  let t = "--";
  let h = "--";
  let rawW = 0;
  let isOffline = false;

  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);

    const response = await apiFetch(RPI_URL, {
      signal: controller.signal,
      cache: "no-store",
    });

    clearTimeout(timeoutId);
    if (!response.ok) throw new Error(`RPi Error: ${response.status}`);

    const data = await response.json();

    const extracted = _extractTempHumWeight(data);
    t = extracted.temp !== null ? parseFloat(extracted.temp).toFixed(1) : "--";
    h = extracted.hum !== null ? parseFloat(extracted.hum).toFixed(0) : "--";
    rawW = extracted.weight !== null ? parseFloat(extracted.weight) : 0;

    latestAudioClass = normalizeAudioClass(data.audio_class || "normal");
    latestAudioConf = Number(data.audio_conf || 0);
    latestAudioScores = Array.isArray(data.audio_scores) ? data.audio_scores : [];
    latestAudioAlert = Boolean(data.audio_alert);
    latestLogOnly = Boolean(data.log_only);

    latestCameraClass = normalizeCameraClass(data.camera_class || "idle");
    latestCameraConf = Number(data.camera_conf || 0);
    latestCameraDetections = Array.isArray(data.camera_detections) ? data.camera_detections : [];
    latestCameraAlert = Boolean(data.camera_alert);
    latestCameraLogOnly = Boolean(data.camera_log_only);

    latestColonyState = data.colony_state || "normal_activity";
    latestLastAudioTs = Number(data.last_audio_analysis_ts || 0);
    latestLastVideoTs = Number(data.last_video_analysis_ts || 0);

    backendAlertActive = Boolean(data.alert_active);
    backendAlertAcknowledged = Boolean(data.alert_acknowledged);
    backendAlertLatched = Boolean(data.alert_latched);
    backendAlertSource = data.alert_source || null;
    backendTriggerAudioClass = data.trigger_audio_class || null;
    backendTriggeredAt = Number(data.triggered_at || 0);
    backendManualVideoActive = Boolean(data.manual_video_active);
    backendAudioVideoTriggerActive = Boolean(data.audio_video_trigger_active);
    backendAudioVideoTriggerClass = data.audio_video_trigger_class || null;
    backendAudioVideoTriggeredAt = Number(data.audio_video_triggered_at || 0);
    backendCameraBackend = data.camera_backend || "--";
    backendVideoAiAlwaysOn = Boolean(data.video_ai_always_on);

    alertAcknowledged = backendAlertAcknowledged || backendAlertLatched;
  } catch (error) {
    const isAbort = error && (error.name === "AbortError" || String(error).includes("AbortError"));
    if (!isAbort) console.error("Connection Failed:", error);
    isOffline = true;
  } finally {
    isFetchingLatest = false;
  }

  let netW = 0;
  let displayW = "--";
  if (!isNaN(rawW) && !isOffline) {
    netW = rawW - HIVE_TARE_WEIGHT;
    if (netW < 0) netW = 0;
    displayW = netW.toFixed(2);
  }

  currentTemp = t;
  currentHum = h;
  currentWeight = displayW;

  const tempDisplayEl = document.getElementById("temp-display");
  const humDisplayEl = document.getElementById("hum-display");
  const weightDisplayEl = document.getElementById("weight-display");

  if (tempDisplayEl) tempDisplayEl.innerText = `${t}°C`;
  if (humDisplayEl) humDisplayEl.innerText = `${h}%`;
  if (weightDisplayEl) weightDisplayEl.innerText = `${displayW} kg`;

  applyAIStatuses(
    latestAudioClass,
    latestCameraClass,
    latestColonyState,
    latestAudioAlert,
    latestLogOnly,
    latestCameraAlert,
    latestCameraLogOnly
  );

  if (tempDisplayEl) {
    if (!isOffline && parseFloat(t) > CONFIG.temp.max && !alertAcknowledged) {
      tempDisplayEl.style.color = "#ff4757";
      if (!alarmInterval && !backendAlertLatched) showAcousticAlert(`🔥 HIGH HEAT: ${t}°C`, "🔥 HIGH TEMPERATURE!");
    } else {
      tempDisplayEl.style.color = "";
    }
  }

  if (!isOffline) {
    const time = formatClock(new Date(), false);

    timeLabels.push(time);
    tempHistory.push(parseFloat(t));
    humHistory.push(parseFloat(h));
    weightHistory.push(netW);

    while (timeLabels.length > MAX_HISTORY) {
      timeLabels.shift();
      tempHistory.shift();
      humHistory.shift();
      weightHistory.shift();
    }

    if (hiveChart) {
      hiveChart.data.labels = timeLabels;
      hiveChart.data.datasets[0].data = tempHistory;
      hiveChart.data.datasets[1].data = humHistory;
      hiveChart.data.datasets[2].data = weightHistory;
      hiveChart.update("none");
    }

    const detailedModal = document.getElementById("detailed-graph-modal");
    const detailedTitle = document.getElementById("detailed-graph-title");
    if (detailedChart && detailedModal && detailedTitle && !detailedModal.classList.contains("hidden")) {
      const title = detailedTitle.innerText;
      if (title.includes("Temperature")) updateDetailedChart(tempHistory, timeLabels);
      else if (title.includes("Humidity")) updateDetailedChart(humHistory, timeLabels);
      else if (title.includes("Weight")) updateDetailedChart(weightHistory, timeLabels);
    }

    if (backendAlertActive && !alertAcknowledged) {
      maybeTriggerAIAlert();
      if (SETTINGS.autoOpenVideoOnAlert && !backendManualVideoActive) {
        const videoModal = document.getElementById("video-modal");
        if (videoModal && videoModal.classList.contains("hidden")) {
          openVideoModal();
        }
      }
    }
  }

  const highestCameraConf = getHighestCameraDetection(latestCameraDetections)?.conf || latestCameraConf || 0;
  const highestConf = Math.max(latestAudioConf, highestCameraConf);

  if (!isOffline && Math.random() > 0.8) {
    let logStatus = "Normal";
    let logEvent = `Audio: ${prettyAudioClass(latestAudioClass)} | Camera: ${prettyCameraClass(latestCameraClass)}`;

    if (backendAlertLatched) {
      logStatus = "Logged";
      logEvent = `Alert acknowledged / latched (${prettyAudioClass(latestAudioClass)})`;
    } else if (latestAudioAlert || latestCameraAlert || backendAlertActive) {
      logStatus = "Warning";
    } else if (latestLogOnly || latestCameraLogOnly) {
      logStatus = "Logged";
    }

    if (parseFloat(t) > CONFIG.temp.max && !alertAcknowledged) {
      logStatus = "Warning";
      logEvent = "Env: High Temp";
    }

    const statusClass = logStatus === "Normal" ? "badge-normal" : logStatus === "Logged" ? "badge-logonly" : "badge-warning";
    const tbody = document.querySelector("#logs-table tbody");

    if (tbody) {
      const row = `
        <tr>
          <td>${formatClock(new Date(), true)}</td>
          <td>${logEvent}</td>
          <td>${t}°C / ${h}% / ${displayW}kg</td>
          <td class="conf-text">${highestConf > 0 ? `${(highestConf * 100).toFixed(0)}%` : "0%"}</td>
          <td><span class="log-badge ${statusClass}">${logStatus}</span></td>
        </tr>
      `;
      tbody.insertAdjacentHTML("afterbegin", row);
      if (tbody.rows.length > 20) tbody.deleteRow(20);
    }

    allLogs.unshift({
      timestamp: new Date().toLocaleString(),
      eventType: logEvent,
      value: `${t}°C | ${displayW}kg`,
      status: logStatus,
    });
    if (allLogs.length > 100) allLogs.pop();
  }

  if (latestCameraDetections.length) {
    const topDet = getHighestCameraDetection(latestCameraDetections);
    if (topDet) {
      const normalized = normalizeCameraClass(topDet.class);
      if (normalized === "person" || normalized === "other_insect") {
        pushVideoLog(`${prettyCameraClass(topDet.class)} detected`, backendAlertLatched ? "Logged" : "Warning");
      } else if (normalized === "tb_cluster" || normalized === "t_biroi" || normalized === "vertebrate") {
        pushVideoLog(`${prettyCameraClass(topDet.class)} logged`, "Logged");
      }
    }
  }

  if (latestAudioAlert && !backendAlertLatched) {
    pushAudioLog(`${prettyAudioClass(latestAudioClass)} detected`, "High");
  } else if (latestLogOnly || backendAlertLatched) {
    pushAudioLog(`${prettyAudioClass(latestAudioClass)} logged`, "Normal");
  }
}

function maybeTriggerAIAlert() {
  if (alertAcknowledged || backendAlertLatched || !backendAlertActive) return;

  let message = "⚠️ HIVE AI ALERT TRIGGERED!";
  let title = "🚨 HIVE ALERT!";

  const audioClass = normalizeAudioClass(backendTriggerAudioClass || latestAudioClass);
  const cameraClass = normalizeCameraClass(backendTriggerAudioClass || latestCameraClass);
  const state = String(latestColonyState || "").toLowerCase();

  if (audioClass === "intruded" || state.includes("intrusion")) {
    message = "⚠️ INTRUSION DETECTED!";
    title = "🚨 INTRUSION DETECTED!";
  } else if (audioClass === "swarming" || state.includes("swarming")) {
    message = "🐝 SWARMING / READY FOR SPLIT!";
    title = "🐝 SWARMING DETECTED!";
  } else if (cameraClass === "person" && backendAlertSource === "video") {
    message = "🚨 PERSON DETECTED NEAR HIVE!";
    title = "🚨 PERSON DETECTED!";
  } else if (cameraClass === "other_insect" && backendAlertSource === "video") {
    message = "⚠️ OTHER INSECT DETECTED!";
    title = "⚠️ OTHER INSECT DETECTED!";
  }

  const alertKey = `${backendAlertSource}|${backendTriggerAudioClass}|${backendTriggeredAt}|${message}`;
  if (lastAlertKey === alertKey) return;
  lastAlertKey = alertKey;

  showAcousticAlert(message, title);
}

function showAcousticAlert(message, title = "🚨 HIVE ALERT!") {
  if (alertAcknowledged || backendAlertLatched) return;

  const modal = document.getElementById("alert-modal");
  const notification = document.getElementById("alert-notification");
  const messageEl = document.getElementById("alert-message");
  const titleEl = document.getElementById("alert-title");
  const stateEl = document.getElementById("alert-state-strip-text");

  if (messageEl) messageEl.innerText = message;
  if (titleEl) titleEl.innerText = title;
  if (stateEl) stateEl.innerText = "Backend alert active • acknowledge will latch until reset";

  modal?.classList.remove("hidden");

  if (notification) {
    notification.innerText = message;
    notification.classList.remove("hidden");
  }

  if (SETTINGS.audioEnabled) playAlarmSound();
}

function loadAlertState() {
  alertAcknowledged = localStorage.getItem("hive_alert_acknowledged") === "true";
}

function setAlertAcknowledged(value) {
  alertAcknowledged = !!value;
  if (alertAcknowledged) localStorage.setItem("hive_alert_acknowledged", "true");
  else localStorage.removeItem("hive_alert_acknowledged");
}

async function sendAlertAcknowledge() {
  try {
    await apiPost(RPI_ALERT_ACK_URL);
  } catch (err) {
    console.warn("Failed to acknowledge backend alert:", err);
  }
}

async function sendAlertReset() {
  try {
    await apiPost(RPI_ALERT_RESET_URL);
  } catch (err) {
    console.warn("Failed to reset backend alert:", err);
  }
}

function acknowledgeAlert() {
  setAlertAcknowledged(true);
  backendAlertAcknowledged = true;
  backendAlertLatched = true;
  backendAlertActive = false;
  document.getElementById("alert-modal")?.classList.add("hidden");
  document.getElementById("alert-notification")?.classList.add("hidden");
  stopAlarmSound();
  updateResetAlertButtonState();
  updateBackendStateCards();
  sendAlertAcknowledge().finally(() => manualRefresh());
}

function resetAlertState() {
  lastResetUiTs = Date.now();
  setAlertAcknowledged(false);
  lastAlertKey = "";
  backendAlertAcknowledged = false;
  backendAlertLatched = false;
  backendAlertActive = false;
  document.getElementById("alert-modal")?.classList.add("hidden");
  document.getElementById("alert-notification")?.classList.add("hidden");
  stopAlarmSound();
  updateResetAlertButtonState();
  sendAlertReset().finally(() => {
    setTimeout(() => {
      manualRefresh();
      updateResetAlertButtonState();
    }, 500);
  });
}

function viewAlertDetails() {
  document.getElementById("alert-modal")?.classList.add("hidden");
  switchTab("logs");
}

function ensureAudioContext() {
  if (!audioContext) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    audioContext = new Ctx({ latencyHint: "interactive" });
    audioMasterGain = audioContext.createGain();
    audioMasterGain.gain.value = 2.5;
    audioMasterGain.connect(audioContext.destination);
    audioNextPlayTime = audioContext.currentTime + 0.05;
  }
  return audioContext;
}

async function ensureAudioContextRunning() {
  const ctx = ensureAudioContext();
  if (ctx.state === "suspended") await ctx.resume();
  if (!audioMasterGain) {
    audioMasterGain = ctx.createGain();
    audioMasterGain.gain.value = 2.5;
    audioMasterGain.connect(ctx.destination);
  }
  return ctx;
}

function playAlarmSound() {
  if (alarmInterval || alertAcknowledged || backendAlertLatched) return;

  const ctx = ensureAudioContext();

  const playBeep = () => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.value = 0.05;

    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.25);
    activeOscillators.push(osc);
  };

  playBeep();
  alarmInterval = setInterval(() => {
    if (alertAcknowledged || backendAlertLatched) {
      stopAlarmSound();
      return;
    }
    playBeep();
  }, 700);
}

function stopAlarmSound() {
  if (alarmInterval) {
    clearInterval(alarmInterval);
    alarmInterval = null;
  }

  activeOscillators.forEach((osc) => {
    try {
      osc.stop();
    } catch { }
  });
  activeOscillators = [];
}

function resampleLinear(input, inRate, outRate) {
  if (inRate === outRate) return input;
  const ratio = outRate / inRate;
  const outLen = Math.max(1, Math.floor(input.length * ratio));
  const output = new Float32Array(outLen);

  for (let i = 0; i < outLen; i++) {
    const srcIndex = i / ratio;
    const i0 = Math.floor(srcIndex);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = srcIndex - i0;
    output[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return output;
}

async function toggleAudio() {
  const statusLabel = document.getElementById("audio-level-label");

  try {
    if (!audioDesired) {
      audioDesired = true;
      audioStopRequested = false;
      audioReconnectAttempts = 0;

      if (audioReconnectTimer) {
        clearTimeout(audioReconnectTimer);
        audioReconnectTimer = null;
      }

      await ensureAudioContextRunning();
      audioNextPlayTime = audioContext.currentTime + 0.05;
      startAudioSocket();
      if (statusLabel) statusLabel.innerText = "Status: Connecting...";
    } else {
      stopAudio();
    }
  } catch (err) {
    audioDesired = false;
    if (statusLabel) statusLabel.innerText = "Status: Unable to start";
    pushAudioLog("Live audio failed to start", "Warning");
    alert("Please interact with the page first to enable live audio.");
  }
}

function _scheduleAudioReconnect(reason = "") {
  if (!audioDesired || audioStopRequested || !navigator.onLine || document.hidden || audioReconnectTimer) return;

  const statusLabel = document.getElementById("audio-level-label");
  const attempt = Math.max(0, audioReconnectAttempts);
  const base = Math.min(60000, 2000 * Math.pow(2, Math.min(attempt, 6)));
  const jitter = Math.floor(Math.random() * 800);
  const delay = base + jitter;

  audioReconnectAttempts += 1;

  if (statusLabel) {
    const why = reason ? ` • ${reason}` : "";
    statusLabel.innerText = `Status: Reconnecting in ${Math.ceil(delay / 1000)}s${why}`;
  }

  audioReconnectTimer = setTimeout(() => {
    audioReconnectTimer = null;
    if (!audioDesired || audioStopRequested || !navigator.onLine || document.hidden) return;
    startAudioSocket();
  }, delay);
}

function startAudioSocket() {
  startAudioHttpStream();
}

function getAudioStreamCandidates() {
  const candidates = [RPI_AUDIO_HTTP_URL, RPI_AUDIO_HTTP_API_URL];
  if (activeAudioStreamUrl && candidates.includes(activeAudioStreamUrl)) {
    return [activeAudioStreamUrl, ...candidates.filter((c) => c !== activeAudioStreamUrl)];
  }
  return candidates;
}

async function openAudioHttpResponse(signal) {
  const candidates = getAudioStreamCandidates();
  let lastError = null;

  for (const url of candidates) {
    try {
      const resp = await apiFetch(url, {
        method: "GET",
        cache: "no-store",
        signal,
      });

      if (resp.ok && resp.body) {
        activeAudioStreamUrl = url;
        applyAudioStreamHeaders(resp.headers);
        return resp;
      }

      lastError = new Error(`HTTP audio failed: ${resp.status} (${url})`);
      if (resp.status !== 404 && resp.status !== 405) break;
    } catch (err) {
      lastError = err;
      if (signal.aborted) throw err;
    }
  }

  throw lastError || new Error("Unable to open audio stream");
}

async function startAudioHttpStream() {
  if (audioFetchRunning) return;

  const statusLabel = document.getElementById("audio-level-label");
  audioFetchRunning = true;

  if (audioFetchController) {
    try {
      audioFetchController.abort();
    } catch { }
  }

  audioFetchController = new AbortController();
  _ensureAudioRing();

  isListening = true;
  audioDrawMode = "live";
  updateAudioButtonState(true);
  updateAudioCardStatus("Live", "#2ecc71");

  if (statusLabel) statusLabel.innerText = "Status: Connecting...";
  pushAudioLog("Live audio (HTTP) connecting", "Normal");

  try {
    const resp = await openAudioHttpResponse(audioFetchController.signal);

    audioReconnectAttempts = 0;
    if (audioReconnectTimer) {
      clearTimeout(audioReconnectTimer);
      audioReconnectTimer = null;
    }

    pushAudioLog("Live audio (HTTP) connected", "Normal");
    if (statusLabel) statusLabel.innerText = `Status: Live • ${AUDIO_STREAM_FORMAT.sampleRate} Hz`;

    audioReader = resp.body.getReader();

    while (audioDesired && !audioStopRequested) {
      const { value, done } = await audioReader.read();
      if (done) break;
      if (!value || !value.byteLength) continue;

      const ab = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
      playRawAudioBuffer(ab);
    }

    if (audioDesired && !audioStopRequested) {
      pushAudioLog("Live audio (HTTP) ended", "Warning");
      _scheduleAudioReconnect("stream ended");
    }
  } catch (err) {
    if (!audioStopRequested) {
      console.warn("HTTP audio stream error:", err);
      pushAudioLog("Live audio (HTTP) error", "Warning");
      audioDrawMode = "error";
      updateAudioCardStatus("Error", "#ff4757");
      if (statusLabel) statusLabel.innerText = "Status: HTTP error";
      _scheduleAudioReconnect("http error");
    }
  } finally {
    if (audioReader) {
      try {
        await audioReader.cancel();
      } catch { }
      try {
        audioReader.releaseLock();
      } catch { }
      audioReader = null;
    }

    audioFetchRunning = false;
    if (!audioDesired) {
      isListening = false;
      audioDrawMode = "waiting";
    }
  }
}

function playRawAudioBuffer(data) {
  const ctx = ensureAudioContext();
  if (ctx.state === "suspended") ctx.resume().catch(() => { });

  if (!audioMasterGain) {
    audioMasterGain = ctx.createGain();
    audioMasterGain.gain.value = 2.5;
    audioMasterGain.connect(ctx.destination);
  }

  const view = new DataView(data);
  const bytesPerSample = 2;
  const sampleCount = Math.floor(view.byteLength / bytesPerSample);
  if (!sampleCount) return;

  let peak = 0;
  const monoData = new Float32Array(sampleCount);

  for (let i = 0; i < sampleCount; i++) {
    const s = view.getInt16(i * bytesPerSample, true) / 32768;
    monoData[i] = s;
    const abs = Math.abs(s);
    if (abs > peak) peak = abs;
  }

  const inRate = AUDIO_STREAM_FORMAT.sampleRate || 16000;
  const outRate = ctx.sampleRate;
  const playData = resampleLinear(monoData, inRate, outRate);

  audioWaveData = playData;
  _pushToAudioRing(playData);

  const buffer = ctx.createBuffer(1, playData.length, outRate);
  buffer.getChannelData(0).set(playData);

  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioMasterGain);

  const safeLead = 0.05;
  const maxLagSeconds = 0.18;

  if (audioNextPlayTime < ctx.currentTime + safeLead) {
    audioNextPlayTime = ctx.currentTime + safeLead;
  }
  if (audioNextPlayTime - ctx.currentTime > maxLagSeconds) {
    audioNextPlayTime = ctx.currentTime + safeLead;
  }

  source.start(audioNextPlayTime);
  audioNextPlayTime += buffer.duration;

  const statusLabel = document.getElementById("audio-level-label");
  if (statusLabel) statusLabel.innerText = `Status: Live • ${inRate} Hz • Level ${(peak * 100).toFixed(0)}%`;
}

function stopAudio() {
  audioDesired = false;
  audioStopRequested = true;

  if (audioReconnectTimer) {
    clearTimeout(audioReconnectTimer);
    audioReconnectTimer = null;
  }
  audioReconnectAttempts = 0;

  if (audioReader) {
    try {
      audioReader.cancel();
    } catch { }
    try {
      audioReader.releaseLock();
    } catch { }
    audioReader = null;
  }

  if (audioFetchController) {
    try {
      audioFetchController.abort();
    } catch { }
    audioFetchController = null;
  }
  audioFetchRunning = false;

  if (audioPingInterval) {
    clearInterval(audioPingInterval);
    audioPingInterval = null;
  }

  if (audioSocket) {
    try {
      audioSocket.close();
    } catch { }
    audioSocket = null;
  }

  const audioTag = document.getElementById("live-audio-player");
  if (audioTag) {
    audioTag.pause();
    audioTag.src = "";
    audioTag.load();
  }

  const ctx = audioContext;
  audioNextPlayTime = ctx ? ctx.currentTime + 0.05 : 0;

  _clearAudioRing();
  isListening = false;
  audioDrawMode = "waiting";
  audioWaveData = null;
  updateAudioButtonState(false);
  updateAudioCardStatus(prettyAudioClass(latestAudioClass), getAudioColor(latestAudioClass));

  const statusLabel = document.getElementById("audio-level-label");
  if (statusLabel) statusLabel.innerText = "Status: Ready";
}

function setupVideoErrorHandling() {
  const img = document.getElementById("cctv-feed");
  if (!img) return;

  img.onerror = function () {
    this.style.display = "none";

    let err = document.getElementById("video-error-msg");
    if (!err) {
      err = document.createElement("p");
      err.id = "video-error-msg";
      err.style.color = "#ff4757";
      err.style.textAlign = "center";
      err.style.marginTop = "20px";
      err.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Camera not ready. Waiting / retrying...`;
      this.parentElement?.appendChild(err);
    }

    setTimeout(() => {
      const modal = document.getElementById("video-modal");
      if (modal && !modal.classList.contains("hidden")) {
        this.src = `${RPI_VIDEO_URL}?t=${Date.now()}`;
      }
    }, 3000);
  };

  img.onload = function () {
    this.style.display = "block";
    const err = document.getElementById("video-error-msg");
    if (err) err.remove();
  };
}

async function notifyVideoOpen() {
  try {
    await apiPost(RPI_VIDEO_OPEN_URL);
  } catch (err) {
    console.warn("Failed to notify backend video open:", err);
  }
}

async function notifyVideoClose() {
  try {
    await apiPost(RPI_VIDEO_CLOSE_URL);
  } catch (err) {
    console.warn("Failed to notify backend video close:", err);
  }
}

function stopVideoStream() {
  const img = document.getElementById("cctv-feed");
  if (!img) return;
  img.src = "about:blank";
  img.removeAttribute("src");
  img.style.display = "none";
}

async function openVideoModal() {
  document.getElementById("video-modal")?.classList.remove("hidden");
  updateCameraBadge("Opening...", "");
  updateCameraCardStatus("Opening...", "#f7b731");

  await notifyVideoOpen();

  const img = document.getElementById("cctv-feed");
  if (!img) return;

  img.src = `${RPI_VIDEO_URL}?t=${Date.now()}`;
  img.style.display = "block";

  const err = document.getElementById("video-error-msg");
  if (err) err.remove();

  updateVideoModalStatus();
  populateVideoLogs();
  updateData();
}

async function closeVideoModal() {
  document.getElementById("video-modal")?.classList.add("hidden");
  stopVideoStream();
  stopAudio();
  await notifyVideoClose();
  updateData();
}

function openAudioModal() {
  const modal = document.getElementById("audio-modal");
  if (!modal) return;

  modal.classList.remove("hidden");
  applyAudioSettingsToModal();
  populateAudioLogs();
  updateAudioModalStatus();
  updateAudioParamLabels();

  if (specAnimationId) cancelAnimationFrame(specAnimationId);
  setTimeout(() => {
    const canvas = document.getElementById("spectrogramCanvas");
    if (canvas) animateSpectrogram(canvas);
  }, 100);
}

function closeAudioModal() {
  const modal = document.getElementById("audio-modal");
  if (!modal) return;

  modal.classList.add("hidden");
  if (specAnimationId) cancelAnimationFrame(specAnimationId);
  stopAudio();
}

function animateSpectrogram(canvas) {
  if (!canvas || !canvas.parentElement) return;

  _readAudioVizFromUI();

  const zoomX = Math.max(0.25, Number(AUDIO_VIZ.zoomX) || 1.0);
  const zoomY = Math.max(0.25, Number(AUDIO_VIZ.zoomY) || 1.0);

  WAVE_VIEW_SECONDS = Math.min(Math.max(5.0 / zoomX, 0.25), AUDIO_RING_SECONDS);
  WAVE_GAIN = Math.min(Math.max(zoomY, 0.25), 10.0);
  updateAudioParamLabels();

  const rect = canvas.parentElement.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width || 640));
  const height = Math.max(180, Math.floor(rect.height || 240));

  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }

  const ctx = canvas.getContext("2d");
  const padL = AUDIO_VIZ.showAxes ? 60 : 12;
  const padR = 12;
  const padT = 12;
  const padB = AUDIO_VIZ.showAxes ? 38 : 18;

  const plotW = Math.max(10, width - padL - padR);
  const plotH = Math.max(10, height - padT - padB);
  const x0 = padL;
  const y0 = padT;
  const x1 = padL + plotW;
  const y1 = padT + plotH;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, width, height);

  ctx.save();
  ctx.beginPath();
  ctx.rect(x0, y0, plotW, plotH);
  ctx.clip();

  if (AUDIO_VIZ.showGrid) {
    for (let x = x0; x <= x1; x += 40) {
      ctx.strokeStyle = "rgba(255,255,255,0.06)";
      ctx.beginPath();
      ctx.moveTo(x, y0);
      ctx.lineTo(x, y1);
      ctx.stroke();
    }
    for (let y = y0; y <= y1; y += 30) {
      ctx.strokeStyle = "rgba(255,255,255,0.06)";
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
    }
  }

  const sr = AUDIO_STREAM_FORMAT.sampleRate || 16000;
  _ensureAudioRing();

  const haveRing = audioRing && (audioRingFilled || audioRingWrite > 0);
  if (haveRing) {
    const viewSamples = Math.max(1, Math.floor(WAVE_VIEW_SECONDS * sr));
    const cap = audioRing.length;

    let endIdx = audioRingWrite - 1;
    if (endIdx < 0) endIdx += cap;

    let startIdx = endIdx - viewSamples + 1;
    while (startIdx < 0) startIdx += cap;

    const samplesPerPixel = Math.max(1, Math.floor(viewSamples / plotW));

    ctx.lineWidth = 1.8;
    ctx.strokeStyle = "rgba(90, 160, 255, 0.95)";
    ctx.fillStyle = "rgba(90, 160, 255, 0.25)";

    const midY = y0 + plotH / 2;
    ctx.beginPath();
    let peak = 0;

    for (let px = 0; px < plotW; px++) {
      const baseOffset = px * samplesPerPixel;
      let localMin = 1;
      let localMax = -1;

      for (let k = 0; k < samplesPerPixel; k++) {
        const offset = baseOffset + k;
        if (offset >= viewSamples) break;
        let idx = startIdx + offset;
        if (idx >= cap) idx -= cap;
        const s = audioRing[idx] * WAVE_GAIN;
        if (s > localMax) localMax = s;
        if (s < localMin) localMin = s;
      }

      localMax = Math.max(-1, Math.min(1, localMax));
      localMin = Math.max(-1, Math.min(1, localMin));
      peak = Math.max(peak, Math.abs(localMax), Math.abs(localMin));

      const x = x0 + px;
      const yUpper = midY - localMax * (plotH * 0.48);
      if (px === 0) ctx.moveTo(x, yUpper);
      else ctx.lineTo(x, yUpper);
    }

    for (let px = plotW - 1; px >= 0; px--) {
      const baseOffset = px * samplesPerPixel;
      let localMin = 1;
      let localMax = -1;

      for (let k = 0; k < samplesPerPixel; k++) {
        const offset = baseOffset + k;
        if (offset >= viewSamples) break;
        let idx = startIdx + offset;
        if (idx >= cap) idx -= cap;
        const s = audioRing[idx] * WAVE_GAIN;
        if (s > localMax) localMax = s;
        if (s < localMin) localMin = s;
      }

      localMax = Math.max(-1, Math.min(1, localMax));
      localMin = Math.max(-1, Math.min(1, localMin));

      const x = x0 + px;
      const yLower = midY - localMin * (plotH * 0.48);
      ctx.lineTo(x, yLower);
    }

    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = "rgba(255,255,255,0.65)";
    ctx.font = "11px Roboto Mono, monospace";
    ctx.fillText(`Peak ${(peak * 100).toFixed(0)}%`, x0 + 10, y0 + 16);
  } else {
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.font = "16px Poppins, sans-serif";
    const msgX = x0 + 10;
    const msgY = y0 + plotH / 2;

    if (audioDrawMode === "error") ctx.fillText("Audio stream error", msgX, msgY);
    else if (isListening) ctx.fillText("Connected... waiting for audio data", msgX, msgY);
    else ctx.fillText("Press LISTEN LIVE to start", msgX, msgY);
  }

  ctx.restore();

  if (AUDIO_VIZ.showAxes) {
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1;

    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x0, y1);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(x0, y1);
    ctx.lineTo(x1, y1);
    ctx.stroke();

    ctx.fillStyle = "rgba(255,255,255,0.65)";
    ctx.font = "11px Roboto Mono, monospace";
    ctx.fillText("Amp", 18, y0 + 10);
    ctx.fillText(`${WAVE_VIEW_SECONDS.toFixed(1)}s`, x1 - 42, y1 + 18);
  }

  specAnimationId = requestAnimationFrame(() => animateSpectrogram(canvas));
}

function startDataUpdates() {
  if (updateIntervalId) clearInterval(updateIntervalId);
  const rate = SETTINGS.refreshRate || 2000;
  updateIntervalId = setInterval(updateData, rate);
}

function manualRefresh() {
  const icon = document.querySelector(".refresh-btn-global i");
  if (icon) icon.classList.add("fa-spin");

  updateData().finally(() => {
    setTimeout(() => {
      if (icon) icon.classList.remove("fa-spin");
    }, 700);
  });
}

function toggleDashboardMenu() {
  document.getElementById("dashboard-mobile-dropdown")?.classList.toggle("hidden");
}

function switchTab(tabId) {
  document.querySelectorAll(".view-section").forEach((sec) => sec.classList.add("hidden"));
  document.getElementById(`view-${tabId}`)?.classList.remove("hidden");

  document.querySelectorAll(".nav-menu li, .nav-bottom li, .mobile-nav div").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(`.nav-item-${tabId}`).forEach((item) => item.classList.add("active"));
}

function reloadDashboard() {
  location.reload();
}

document.addEventListener("DOMContentLoaded", () => {
  const dashboardSwarm = document.getElementById("dashboard-swarm");
  if (dashboardSwarm) createSwarm(dashboardSwarm, "dashboard-bee", 20);

  loadSettings();
  loadAlertState();
  loadProfile();
  initChart();
  startDataUpdates();
  updateCamTime();
  setInterval(updateCamTime, 1000);
  setupVideoErrorHandling();
  switchTab("dashboard");
  applyAudioSettingsToModal();
  updateAudioParamLabels();
  updateBackendStateCards();
  updateResetAlertButtonState();
  updateAudioButtonState(false);
  updateAudioCardStatus("Normal", "#2ecc71");
  updateAudioBadge("Ready");
  updateCameraCardStatus("Idle", "#95a5a6");
  updateCameraBadge("Watching");
  populateVideoLogs();
  populateAudioLogs();
  updateAudioModalStatus();
  updateVideoModalStatus();
  updateData();

  const listeners = [
    "audio-zoom-x",
    "audio-zoom-y",
    "audio-show-axes",
    "audio-show-grid",
  ];

  listeners.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", () => {
      _readAudioVizFromUI();
      updateAudioParamLabels();
    });
    if (el) el.addEventListener("change", () => {
      _readAudioVizFromUI();
      updateAudioParamLabels();
    });
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (audioDesired) stopAudio();
      stopAlarmSound();
    }
  });

  window.addEventListener("online", () => {
    if (audioDesired && !audioFetchRunning) startAudioSocket();
  });

  window.addEventListener("beforeunload", () => {
    stopAudio();
    stopAlarmSound();
    stopVideoStream();
  });
});
