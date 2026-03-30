// /public/script.js

let socket;

const CONFIG = {
  temp: { min: 28.0, max: 33.0 },
  hum: { min: 55, max: 65 },
  weight: 14.5,
};

const CONFIG_BASE =
  (window.BEEROI_CONFIG && String(window.BEEROI_CONFIG.PROXY_BASE_URL || "").trim()) || "";

// If you deploy to Render, you usually want CONFIG_BASE="" so it uses window.location.origin.
// Keep FUNNEL_ORIGIN as fallback only (dev/local).
const FUNNEL_ORIGIN =
  (window.__HIVE_FUNNEL_ORIGIN && String(window.__HIVE_FUNNEL_ORIGIN).trim()) ||
  (localStorage.getItem("hive_funnel_origin") || "").trim();

const HTTP_ORIGIN = CONFIG_BASE || window.location.origin || FUNNEL_ORIGIN;

function _buildHttpAndWsUrls(httpOrigin) {
  const httpBase = new URL(httpOrigin);
  const wsBase = new URL(httpOrigin);
  wsBase.protocol = httpBase.protocol === "https:" ? "wss:" : "ws:";

  return {
    RPI_URL: new URL("/api/latest", httpBase).toString(),
    RPI_AUDIO_WS_URL: new URL("/ws/audio", wsBase).toString(),
    RPI_VIDEO_URL: new URL("/video.mjpg", httpBase).toString(),
  };
}

const { RPI_URL, RPI_AUDIO_WS_URL, RPI_VIDEO_URL } = _buildHttpAndWsUrls(HTTP_ORIGIN);

const HIVE_TARE_WEIGHT = 2.0;

const AUDIO_STREAM_FORMAT = {
  sampleRate: 16000,
  channels: 1,
  bitDepth: 16,
};

const AUDIO_VIZ = {
  zoomY: 1.25,
  zoomX: 1.0,
  showAxes: true,
  showGrid: true,
};

function _readAudioVizFromUI() {
  const zy = document.getElementById("audio-zoom-y");
  const zx = document.getElementById("audio-zoom-x");

  if (zy) {
    const v = Number(zy.value);
    if (!Number.isNaN(v) && v > 0) AUDIO_VIZ.zoomY = v;
  }
  if (zx) {
    const v = Number(zx.value);
    if (!Number.isNaN(v) && v > 0) AUDIO_VIZ.zoomX = v;
  }
}

let SETTINGS = { refreshRate: 2000, sensitivity: 8, audioEnabled: true };
let hiveChart, detailedChart, updateIntervalId, alarmInterval = null;
let activeOscillators = [],
  specAnimationId = null;

let tempHistory = [],
  humHistory = [],
  weightHistory = [],
  timeLabels = [];
let allLogs = [],
  audioLogs = [],
  videoLogs = [];
const MAX_HISTORY = 20;

let currentTemp = 0,
  currentHum = 0,
  currentWeight = 0;

let audioContext = null;
let audioSocket = null;
let isListening = false;
let audioMasterGain = null;
let audioNextPlayTime = 0;
let audioWaveData = null;
let audioDrawMode = "waiting";

let latestAudioClass = "normal";
let latestAudioConf = 0;
let latestAudioScores = [];
let latestCameraClass = "bee";
let latestCameraConf = 0;
let latestCameraDetections = [];
let latestColonyState = "normal_activity";
let latestAudioAlert = false;
let latestLogOnly = false;
let latestCameraAlert = false;
let latestCameraLogOnly = false;
let latestLastAudioTs = 0;
let latestLastVideoTs = 0;

let isFetchingLatest = false;
let lastAlertKey = "";
let alertAcknowledged = false;

const AUDIO_RING_SECONDS = 12;
let audioRing = null;
let audioRingWrite = 0;
let audioRingFilled = false;

let WAVE_VIEW_SECONDS = 5.0;
let WAVE_GAIN = 1.0;

function _ensureAudioRing() {
  const sr = AUDIO_STREAM_FORMAT.sampleRate || 16000;
  const capacity = Math.max(1, Math.floor(sr * AUDIO_RING_SECONDS));
  if (!audioRing || audioRing.length !== capacity) {
    audioRing = new Float32Array(capacity);
    audioRingWrite = 0;
    audioRingFilled = false;
  }
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

const DEFAULT_PROFILE = {
  name: "Admin",
  role: "Head Keeper",
  credentials: "Authorized Personnel",
  avatar: "https://api.dicebear.com/7.x/avataaars/svg?seed=Felix",
};

const BEE_RESOURCES = {
  pollinators: {
    title: "Forest Pollinators",
    links: [
      { name: "Wikipedia: Stingless Bee", url: "https://en.wikipedia.org/wiki/Stingless_bee", icon: "fas fa-book" },
      {
        name: "Stingless Beekeeping - Filipino Style",
        url: "https://www.ps.org.au/articles/stingless-beekeeping-filipino-style#:~:text=The%20most%20popular%20and%20commonly,pollinate%20mango%20and%20coconut%20crops",
        icon: "fas fa-book",
      },
      { name: "Tetragonula biroi - Philippines", url: "https://www.nativebeehives.com/tetragonula-biroi-philippines/", icon: "fas fa-book" },
    ],
  },
  honey: {
    title: "Medicinal Pot Honey",
    links: [
      { name: "NCBI Study", url: "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8067784/", icon: "fas fa-microscope" },
      { name: "Ark of taste", url: "https://www.fondazioneslowfood.com/en/ark-of-taste-slow-food/honey-of-stingless-bee/", icon: "fas fa-microscope" },
      {
        name: "Philippine Journal of Science",
        url: "https://philjournalsci.dost.gov.ph/effect-of-storage-time-on-the-quality-of-stingless-bee-tetragonula-biroi-friese-honey/",
        icon: "fas fa-microscope",
      },
    ],
  },
  defense: {
    title: "Propolis & Defense",
    links: [
      { name: "Wikipedia: Propolis", url: "https://en.wikipedia.org/wiki/Propolis", icon: "fas fa-shield-alt" },
      {
        name: "Philippine Journal of Science",
        url: "https://philjournalsci.dost.gov.ph/wp-content/uploads/2025/07/inhibitory_activity_of_propolis_from_Phil_stingless_bees_.pdf",
        icon: "fas fa-shield-alt",
      },
      {
        name: "ResearchGate",
        url: "https://www.researchgate.net/publication/376721862_ADAPTIVE_DEFENCE_STRATEGIES_OF_THE_STINGLESS_BEE_TETRAGONULA_IRIDIPENNIS_SMITH_AGAINST_NEST_INTRUDERS_IN_A_NEWLY_DIVIDED_COLONY#:~:text=division%20guard%20bee%20ac%EE%9E%9F,These",
        icon: "fas fa-shield-alt",
      },
    ],
  },
};

function loadSettings() {
  const saved = JSON.parse(localStorage.getItem("hive_settings"));
  if (saved) SETTINGS = { ...SETTINGS, ...saved };

  const refreshEl = document.getElementById("setting-refresh");
  if (refreshEl) refreshEl.value = SETTINGS.refreshRate;

  const audioEl = document.getElementById("setting-audio");
  if (audioEl) audioEl.checked = SETTINGS.audioEnabled;

  const sensitivityEl = document.getElementById("setting-sensitivity");
  if (sensitivityEl) sensitivityEl.value = SETTINGS.sensitivity;
}

function loadProfile() {
  const saved = JSON.parse(localStorage.getItem("hive_profile")) || DEFAULT_PROFILE;
  try {
    document.getElementById("sidebar-name").innerText = saved.name;
    document.getElementById("sidebar-role").innerText = saved.role;
    document.getElementById("sidebar-avatar").src = saved.avatar;
    document.getElementById("greeting-text").innerText = `Hello, ${saved.name.split(" ")[0]}! 👋`;
    document.getElementById("nav-mini-avatar").src = saved.avatar;

    const nameInput = document.getElementById("edit-name");
    if (nameInput) nameInput.value = saved.name;

    const roleInput = document.getElementById("edit-role");
    if (roleInput) roleInput.value = saved.role;

    const credInput = document.getElementById("edit-credentials");
    if (credInput) credInput.value = saved.credentials || "";

    const avatarPrev = document.getElementById("avatar-preview");
    if (avatarPrev) avatarPrev.src = saved.avatar;
  } catch (e) {}
}

function updateCamTime() {
  const el = document.getElementById("cam-time");
  if (el) el.innerText = new Date().toLocaleTimeString("en-US", { hour12: false });
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

function hexToRgba(hex, alpha = 0.15) {
  if (!hex || !hex.startsWith("#")) return `rgba(255, 179, 0, ${alpha})`;
  let c = hex.replace("#", "");
  if (c.length === 3) c = c
    .split("")
    .map((ch) => ch + ch)
    .join("");
  const num = parseInt(c, 16);
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function playVideo(el, label) {
  console.log("Selected video item:", label || "LIVE FEED");
}

function pushAudioLog(eventType, stressLevel = "Normal") {
  const entry = { timestamp: new Date().toLocaleString(), eventType, stressLevel };
  audioLogs.unshift(entry);
  if (audioLogs.length > 20) audioLogs.pop();

  const audioModal = document.getElementById("audio-modal");
  if (audioModal && !audioModal.classList.contains("hidden")) populateAudioLogs();
}

function pushVideoLog(eventType, status = "Normal") {
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

function getCameraStatusElement() {
  return document.querySelector(".card-cam .status-text");
}

function updateCameraCardStatus(text, color = "") {
  const camStatus = getCameraStatusElement();
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
  return "normal";
}

function normalizeCameraClass(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "human" || raw === "person" || raw === "hand") return "person";
  if (raw === "vertebrate" || raw === "animal" || raw === "lizard" || raw === "frog" || raw === "bird" || raw === "gecko" || raw === "rodent")
    return "vertebrate";
  if (raw === "other_insect" || raw === "other insect" || raw === "insect" || raw === "ant" || raw === "wasp" || raw === "fly" || raw === "beetle" || raw === "moth")
    return "other_insect";
  if (raw === "tb_cluster") return "tb_cluster";
  if (raw === "t_biroi" || raw === "tetragonula biroi") return "t_biroi";
  if (raw === "hive_entrance" || raw === "entrance") return "hive_entrance";
  if (raw === "no_detection") return "no_detection";
  return raw || "bee";
}

function prettyAudioClass(label) {
  const normalized = normalizeAudioClass(label);
  if (normalized === "intruded") return "Intruded";
  if (normalized === "swarming") return "Swarming";
  if (normalized === "queenless") return "Queenless";
  if (normalized === "queenright") return "Queenright";
  if (normalized === "false") return "False Trigger";
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
  if (normalized === "bee") return "Bee";
  return normalized ? normalized.replaceAll("_", " ").replace(/\b\w/g, (m) => m.toUpperCase()) : "Unknown";
}

function getAudioColor(label) {
  const normalized = normalizeAudioClass(label);
  if (normalized === "intruded") return "#ff4757";
  if (normalized === "swarming") return "#f7b731";
  if (normalized === "queenless" || normalized === "queenright" || normalized === "false") return "#9b59b6";
  return "#2ecc71";
}

function getCameraColor(label) {
  const normalized = normalizeCameraClass(label);
  if (normalized === "person" || normalized === "other_insect") return "#ff4757";
  if (normalized === "tb_cluster" || normalized === "t_biroi" || normalized === "vertebrate") return "#9b59b6";
  return "#2ecc71";
}

function prettyColonyState(state) {
  const s = String(state || "normal_activity");
  return s.replaceAll("_", " ").replace(/\b\w/g, (m) => m.toUpperCase());
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

  if (latestAudioAlert) updateAudioBadge("Alert", "badge-warning");
  else if (latestLogOnly) updateAudioBadge("Logs Only", "badge-logonly");
  else updateAudioBadge("Listening", "");

  if (latestCameraAlert) {
    updateCameraBadge("● Alert", "badge-warning blink-red");
  } else if (latestCameraLogOnly) {
    updateCameraBadge("Logs Only", "badge-logonly");
  } else if (normalizedCamera === "no_detection") {
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
  if (audioAiNote) audioAiNote.innerText = latestAudioAlert ? "Alert-worthy acoustic pattern detected" : latestLogOnly ? "Acoustic event logged without alarm" : "Always-on acoustic monitoring active";
  if (audioAlertPill) audioAlertPill.innerText = latestAudioAlert ? "Alert" : latestLogOnly ? "Log Only" : "No Alert";
  if (videoAiTop) videoAiTop.innerText = `${prettyCameraClass(latestCameraClass)} • ${formatPercent(latestCameraConf)}`;
  if (videoAiNote) {
    if (latestCameraAlert) videoAiNote.innerText = "Alert + log triggered by video detection";
    else if (latestCameraLogOnly) videoAiNote.innerText = "Video detection logged without alarm";
    else if (latestCameraDetections.length) videoAiNote.innerText = `${latestCameraDetections.length} object(s) detected at entrance`;
    else videoAiNote.innerText = "Entrance object detection standby";
  }
  if (videoAlertPill) videoAlertPill.innerText = latestCameraAlert ? "Alert" : latestCameraLogOnly ? "Log Only" : "Watching";

  updateAudioModalStatus();
  updateVideoModalStatus();
}

function shouldRaiseAlert(audioClass, cameraClass, colonyState = "", audioAlert = false, cameraAlert = false) {
  const normalizedAudio = normalizeAudioClass(audioClass);
  const state = String(colonyState || "").toLowerCase();

  if (audioAlert) return true;
  if (cameraAlert) return true;
  if (normalizedAudio === "intruded" || normalizedAudio === "swarming") return true;
  if (state.includes("intrusion") || state.includes("swarming")) return true;
  return false;
}

function maybeTriggerAIAlert(audioClass, cameraClass, colonyState = "", audioAlert = false, cameraAlert = false) {
  const normalizedAudio = normalizeAudioClass(audioClass);
  const normalizedCamera = normalizeCameraClass(cameraClass);
  const state = String(colonyState || "").toLowerCase();

  let message = "";
  let title = "🚨 HIVE ALERT!";

  if (normalizedAudio === "intruded" || state.includes("intrusion")) {
    message = "⚠️ INTRUSION DETECTED!";
    title = "🚨 INTRUSION DETECTED!";
  } else if (normalizedCamera === "person" && cameraAlert) {
    message = "🚨 PERSON DETECTED NEAR HIVE!";
    title = "🚨 PERSON DETECTED!";
  } else if (normalizedCamera === "other_insect" && cameraAlert) {
    message = "⚠️ OTHER INSECT DETECTED!";
    title = "⚠️ OTHER INSECT DETECTED!";
  } else if (normalizedAudio === "swarming" || state.includes("swarming")) {
    message = "🐝 SWARMING / READY FOR SPLIT!";
    title = "🐝 SWARMING DETECTED!";
  } else if (audioAlert) {
    message = "⚠️ HIVE AI ALERT TRIGGERED!";
  }

  if (!message) return;

  const alertKey = `${normalizedAudio}|${normalizedCamera}|${state}|${message}`;
  if (lastAlertKey === alertKey) return;
  lastAlertKey = alertKey;

  showAcousticAlert(message, title);
}

function ensureAudioContext() {
  if (!audioContext) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    audioContext = new Ctx();
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
  return ctx;
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
  if (alertMode) alertMode.innerText = latestAudioAlert ? "Alert" : latestLogOnly ? "Log Only" : "No Alert";
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

  let metaParts = [];
  metaParts.push(`Confidence: ${top ? formatPercent(top.conf) : formatPercent(latestCameraConf)}`);
  if (latestCameraAlert) metaParts.push("Mode: Alert + Log");
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

function _pushHistoryPoint(timeLabel, tNum, hNum, wNum) {
  timeLabels.push(timeLabel);
  tempHistory.push(tNum);
  humHistory.push(hNum);
  weightHistory.push(wNum);

  if (timeLabels.length > MAX_HISTORY) {
    timeLabels.shift();
    tempHistory.shift();
    humHistory.shift();
    weightHistory.shift();
  }
}

async function updateData() {
  if (isFetchingLatest) return;
  isFetchingLatest = true;

  let t = "--";
  let h = "--";
  let rawW = 0;

  let audioClass = latestAudioClass || "normal";
  let cameraClass = latestCameraClass || "bee";
  let colonyState = latestColonyState || "normal_activity";
  let audioConf = latestAudioConf || 0;
  let cameraConf = latestCameraConf || 0;
  let audioScores = Array.isArray(latestAudioScores) ? latestAudioScores : [];
  let cameraDetections = Array.isArray(latestCameraDetections) ? latestCameraDetections : [];
  let audioAlert = !!latestAudioAlert;
  let logOnly = !!latestLogOnly;
  let cameraAlert = !!latestCameraAlert;
  let cameraLogOnly = !!latestCameraLogOnly;
  let lastAudioTs = Number(latestLastAudioTs || 0);
  let lastVideoTs = Number(latestLastVideoTs || 0);

  let isOffline = false;

  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);

    let response;
    try {
      response = await fetch(RPI_URL, { signal: controller.signal, cache: "no-store" });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) throw new Error(`RPi Error: HTTP ${response.status}`);

    const data = await response.json();

    const extracted = _extractTempHumWeight(data);
    t = extracted.temp !== null && extracted.temp !== undefined ? Number.parseFloat(extracted.temp).toFixed(1) : "--";
    h = extracted.hum !== null && extracted.hum !== undefined ? Number.parseFloat(extracted.hum).toFixed(0) : "--";
    rawW = extracted.weight !== null && extracted.weight !== undefined ? Number.parseFloat(extracted.weight) : 0;

    audioClass = normalizeAudioClass(data.audio_class ?? data.sound_status ?? "normal");
    cameraClass = normalizeCameraClass(data.camera_class ?? data.vision_class ?? data.detected_object ?? "bee");

    colonyState = data.colony_state || colonyState;
    audioConf = Number(data.audio_conf || 0);
    cameraConf = Number(data.camera_conf || 0);
    audioScores = Array.isArray(data.audio_scores) ? data.audio_scores : [];
    cameraDetections = Array.isArray(data.camera_detections) ? data.camera_detections : [];
    audioAlert = Boolean(data.audio_alert);
    logOnly = Boolean(data.log_only);
    cameraAlert = Boolean(data.camera_alert);
    cameraLogOnly = Boolean(data.camera_log_only);
    lastAudioTs = Number(data.last_audio_analysis_ts || 0);
    lastVideoTs = Number(data.last_video_analysis_ts || 0);

    latestAudioClass = audioClass;
    latestAudioConf = audioConf;
    latestAudioScores = audioScores;
    latestCameraClass = cameraClass;
    latestCameraConf = cameraConf;
    latestCameraDetections = cameraDetections;
    latestColonyState = colonyState;
    latestAudioAlert = audioAlert;
    latestLogOnly = logOnly;
    latestCameraAlert = cameraAlert;
    latestCameraLogOnly = cameraLogOnly;
    latestLastAudioTs = lastAudioTs;
    latestLastVideoTs = lastVideoTs;
  } catch (error) {
    isOffline = true;
    if (error?.name === "AbortError") console.warn("updateData timed out");
    else console.error("Connection Failed:", error);
    t = "--";
    h = "--";
    rawW = 0;
  } finally {
    isFetchingLatest = false;
  }

  let netW = 0,
    displayW = "--";
  if (!Number.isNaN(rawW) && !isOffline) {
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

  applyAIStatuses(audioClass, cameraClass, colonyState, audioAlert, logOnly, cameraAlert, cameraLogOnly);

  if (tempDisplayEl) {
    const tNum = Number.parseFloat(t);
    if (!isOffline && Number.isFinite(tNum) && tNum > CONFIG.temp.max) {
      tempDisplayEl.style.color = "#ff4757";
      if (!alarmInterval) showAcousticAlert(`🔥 HIGH HEAT: ${t}°C`, "🔥 HIGH TEMPERATURE!");
    } else {
      tempDisplayEl.style.color = "";
    }
  }

  if (!isOffline) {
    const tNum = Number.parseFloat(t);
    const hNum = Number.parseFloat(h);

    if (Number.isFinite(tNum) && Number.isFinite(hNum)) {
      const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      _pushHistoryPoint(time, tNum, hNum, netW);
    }

    if (hiveChart) {
      hiveChart.data.labels = timeLabels;
      hiveChart.data.datasets[0].data = tempHistory;
      hiveChart.data.datasets[1].data = humHistory;
      hiveChart.data.datasets[2].data = weightHistory;
      hiveChart.update("none");
    }

    const detailedModal = document.getElementById("detailed-graph-modal");
    if (detailedChart && detailedModal && !detailedModal.classList.contains("hidden")) {
      const title = document.getElementById("detailed-graph-title").innerText;
      if (title.includes("Temperature")) updateDetailedChart(tempHistory, timeLabels);
      else if (title.includes("Humidity")) updateDetailedChart(humHistory, timeLabels);
      else if (title.includes("Weight")) updateDetailedChart(weightHistory, timeLabels);
    }

    if (shouldRaiseAlert(audioClass, cameraClass, colonyState, audioAlert, cameraAlert)) {
      if (!alarmInterval) maybeTriggerAIAlert(audioClass, cameraClass, colonyState, audioAlert, cameraAlert);
    }
  }

  const highestCameraConf = getHighestCameraDetection(cameraDetections)?.conf || cameraConf || 0;
  const highestConf = Math.max(audioConf, highestCameraConf);

  if (!isOffline && Math.random() > 0.8) {
    let logStatus = "Normal";
    let logEvent = `Audio: ${prettyAudioClass(audioClass)} | Camera: ${prettyCameraClass(cameraClass)}`;

    if (audioAlert || cameraAlert) logStatus = "Warning";
    else if (logOnly || cameraLogOnly) logStatus = "Logged";

    if (audioClass === "swarming") {
      logStatus = "Warning";
      logEvent = "Audio: Swarming / Ready for Split";
    }
    if (audioClass === "queenless") {
      logStatus = "Logged";
      logEvent = "Audio: Queenless state detected";
    }
    if (audioClass === "queenright") {
      logStatus = "Logged";
      logEvent = "Audio: Queenright state detected";
    }
    if (audioClass === "false") {
      logStatus = "Logged";
      logEvent = "Audio: False trigger recorded";
    }

    if (cameraClass === "person") {
      logStatus = "Warning";
      logEvent = "Video: Person detected";
    }
    if (cameraClass === "other_insect") {
      logStatus = "Warning";
      logEvent = "Video: Other insect detected";
    }
    if (cameraClass === "tb_cluster") {
      logStatus = "Logged";
      logEvent = "Video: TB cluster detected";
    }
    if (cameraClass === "t_biroi") {
      logStatus = "Logged";
      logEvent = "Video: T. biroi detected";
    }
    if (cameraClass === "vertebrate") {
      logStatus = "Logged";
      logEvent = "Video: Vertebrate detected";
    }

    const tNum = Number.parseFloat(t);
    if (Number.isFinite(tNum) && tNum > CONFIG.temp.max) {
      logStatus = "Warning";
      logEvent = "Env: High Temp";
    }

    const statusClass = logStatus === "Normal" ? "badge-normal" : logStatus === "Logged" ? "badge-logonly" : "badge-warning";

    const tbody = document.querySelector("#logs-table tbody");
    if (tbody) {
      const row = `
        <tr>
          <td>${new Date().toLocaleTimeString()}</td>
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
  }

  updateAudioModalStatus();
  updateVideoModalStatus();

  if (cameraDetections.length) {
    const topDet = getHighestCameraDetection(cameraDetections);
    if (topDet) {
      const normalized = normalizeCameraClass(topDet.class);
      if (normalized === "person" || normalized === "other_insect") {
        pushVideoLog(`${prettyCameraClass(topDet.class)} detected`, "Warning");
      } else if (normalized === "tb_cluster" || normalized === "t_biroi" || normalized === "vertebrate") {
        pushVideoLog(`${prettyCameraClass(topDet.class)} logged`, "Logged");
      }
    }
  }

  if (audioAlert) pushAudioLog(`${prettyAudioClass(audioClass)} detected`, "High");
  else if (logOnly) pushAudioLog(`${prettyAudioClass(audioClass)} logged`, "Normal");
}

function startDataUpdates() {
  if (updateIntervalId) clearInterval(updateIntervalId);
  const rate = SETTINGS.refreshRate || 2000;
  updateIntervalId = setInterval(updateData, rate);
}

function manualRefresh() {
  const icon = document.querySelector(".refresh-btn-global i");
  if (icon) icon.classList.add("fa-spin");

  updateData().then(() => {
    setTimeout(() => {
      if (icon) icon.classList.remove("fa-spin");
    }, 800);
  });
}

function toggleDashboardMenu() {
  const menu = document.getElementById("dashboard-mobile-dropdown");
  if (menu) menu.classList.toggle("hidden");
}

function saveSettings() {
  const refreshEl = document.getElementById("setting-refresh");
  const audioEl = document.getElementById("setting-audio");
  const sensitivityEl = document.getElementById("setting-sensitivity");

  if (refreshEl) SETTINGS.refreshRate = parseInt(refreshEl.value, 10);
  if (audioEl) SETTINGS.audioEnabled = audioEl.checked;
  if (sensitivityEl) SETTINGS.sensitivity = parseInt(sensitivityEl.value, 10);

  localStorage.setItem("hive_settings", JSON.stringify(SETTINGS));
  startDataUpdates();
  alert("✅ Settings Saved!");
}

function resetSettings() {
  localStorage.removeItem("hive_settings");
  location.reload();
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
}

function openDetailedGraphOnCardClick(type) {
  const modal = document.getElementById("detailed-graph-modal");
  if (!modal) return;

  modal.classList.remove("hidden");

  let title, color, dataArray;
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

  document.getElementById("detailed-graph-title").innerText = title;
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
  if (nums.length === 0) return;

  const sum = nums.reduce((a, b) => a + b, 0);
  const avg = (sum / nums.length).toFixed(2);

  document.getElementById("graph-current").innerText = nums[nums.length - 1].toFixed(2);
  document.getElementById("graph-max").innerText = Math.max(...nums).toFixed(2);
  document.getElementById("graph-min").innerText = Math.min(...nums).toFixed(2);
  document.getElementById("graph-avg").innerText = avg;
}

function closeDetailedGraphModal() {
  document.getElementById("detailed-graph-modal").classList.add("hidden");
}

function switchPublicTab(tabId) {
  document.querySelectorAll(".public-section").forEach((sec) => sec.classList.add("hidden"));
  document.getElementById(tabId).classList.remove("hidden");
  if (tabId === "hero") window.scrollTo(0, 0);
}

function toggleMobileMenu() {
  document.getElementById("mobile-public-menu").classList.toggle("hidden");
}

function mobileNavClick(tabId) {
  switchPublicTab(tabId);
  toggleMobileMenu();
}

function loadAlertState() {
  alertAcknowledged = localStorage.getItem("hive_alert_acknowledged") === "true";
}

function setAlertAcknowledged(value) {
  alertAcknowledged = !!value;
  if (alertAcknowledged) localStorage.setItem("hive_alert_acknowledged", "true");
  else localStorage.removeItem("hive_alert_acknowledged");
}

function resetAlertState() {
  setAlertAcknowledged(false);
  lastAlertKey = "";
  document.getElementById("alert-modal")?.classList.add("hidden");
  document.getElementById("alert-notification")?.classList.add("hidden");
  stopAlarmSound();
}

function goToLogin() {
  document.getElementById("landing-view").classList.add("hidden");
  document.getElementById("login-view").classList.remove("hidden");
  document.getElementById("mobile-nav-bar").classList.add("hidden");
}

function backToHome() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("landing-view").classList.remove("hidden");
}

function attemptLogin() {
  const email = document.getElementById("email-input").value.trim().toLowerCase();
  const password = document.getElementById("password-input").value.trim();

  if (email.endsWith("@gmail.com") && password === "hive123") {
    localStorage.setItem("hive_isLoggedIn", "true");
    location.reload();
  } else {
    document.getElementById("login-error").classList.remove("hidden");
  }
}

function logout() {
  localStorage.removeItem("hive_isLoggedIn");
  location.reload();
}

function switchTab(tabId) {
  document.querySelectorAll(".view-section").forEach((sec) => sec.classList.add("hidden"));
  document.getElementById("view-" + tabId).classList.remove("hidden");

  document.querySelectorAll(".nav-menu li, .nav-bottom li, .mobile-nav div").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".nav-item-" + tabId).forEach((item) => item.classList.add("active"));
}

function stopVideoStream() {
  const img = document.getElementById("cctv-feed");
  if (!img) return;
  img.src = "about:blank";
  img.removeAttribute("src");
  img.style.display = "none";
}

function openVideoModal() {
  document.getElementById("video-modal").classList.remove("hidden");

  const img = document.getElementById("cctv-feed");
  if (!img) return;

  img.src = `${RPI_VIDEO_URL}?t=${Date.now()}`;
  img.style.display = "block";

  const err = document.getElementById("video-error-msg");
  if (err) err.remove();

  updateVideoModalStatus();
  populateVideoLogs();
}

function closeVideoModal() {
  document.getElementById("video-modal").classList.add("hidden");
  stopVideoStream();
  stopAudio();
}

async function toggleAudio() {
  const statusLabel = document.getElementById("audio-level-label");

  try {
    if (!isListening) {
      await ensureAudioContextRunning();
      startAudioSocket();
      if (statusLabel) statusLabel.innerText = "Status: Connecting...";
    } else {
      stopAudio();
    }
  } catch (err) {
    if (statusLabel) statusLabel.innerText = "Status: Unable to start";
    pushAudioLog("Live audio failed to start", "Warning");
    alert("Please interact with the page first to enable live audio.");
  }
}

let _wsRetryTimer = null;
let _wsRetryCount = 0;

function _scheduleWsRetry() {
  if (_wsRetryTimer) return;
  const delay = Math.min(1000 * 2 ** _wsRetryCount, 15000);
  _wsRetryCount = Math.min(_wsRetryCount + 1, 6);

  _wsRetryTimer = setTimeout(() => {
    _wsRetryTimer = null;
    startAudioSocket();
  }, delay);
}

function startAudioSocket() {
  if (audioSocket && (audioSocket.readyState === WebSocket.OPEN || audioSocket.readyState === WebSocket.CONNECTING)) return;

  const statusLabel = document.getElementById("audio-level-label");

  audioSocket = new WebSocket(RPI_AUDIO_WS_URL);
  audioSocket.binaryType = "arraybuffer";

  const connectTimeoutMs = 15000;
  const connectTimer = setTimeout(() => {
    if (audioSocket && audioSocket.readyState !== WebSocket.OPEN) {
      if (statusLabel) statusLabel.innerText = "Status: WS timeout";
      try {
        audioSocket.close();
      } catch (e) {}
    }
  }, connectTimeoutMs);

  audioSocket.onopen = () => {
    clearTimeout(connectTimer);
    _wsRetryCount = 0;

    _ensureAudioRing();

    isListening = true;
    audioDrawMode = "live";
    updateAudioButtonState(true);
    updateAudioCardStatus("Live", "#2ecc71");

    if (statusLabel) statusLabel.innerText = "Status: Live";
    pushAudioLog("Live audio stream connected", "Normal");

    try {
      audioSocket.send("start");
    } catch (e) {}
  };

  audioSocket.onmessage = (event) => {
    if (event.data) playRawAudioBuffer(event.data);
  };

  audioSocket.onerror = () => {
    clearTimeout(connectTimer);
    audioDrawMode = "error";
    if (statusLabel) statusLabel.innerText = "Status: WS error";
    updateAudioCardStatus("Error", "#ff4757");
    pushAudioLog("Live audio stream error", "Warning");
  };

  audioSocket.onclose = () => {
    clearTimeout(connectTimer);
    const diedDuringConnect = !isListening;

    isListening = false;
    audioSocket = null;
    audioDrawMode = "waiting";
    updateAudioButtonState(false);
    updateAudioCardStatus(prettyAudioClass(latestAudioClass), getAudioColor(latestAudioClass));
    if (statusLabel) statusLabel.innerText = "Status: Ready";
    pushAudioLog("Live audio stream disconnected", "Normal");

    if (diedDuringConnect) _scheduleWsRetry();
  };
}

function playRawAudioBuffer(data) {
  const ctx = ensureAudioContext();
  if (ctx.state === "suspended") ctx.resume();

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

  const inRate = AUDIO_STREAM_FORMAT.sampleRate;
  const outRate = ctx.sampleRate;
  const playData = resampleLinear(monoData, inRate, outRate);

  audioWaveData = playData;
  _pushToAudioRing(playData);

  const buffer = ctx.createBuffer(1, playData.length, outRate);
  buffer.getChannelData(0).set(playData);

  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioMasterGain);
  source.onended = () => {
    try {
      source.disconnect();
    } catch (e) {}
  };

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
  if (statusLabel) statusLabel.innerText = `Status: Live • Level ${(peak * 100).toFixed(0)}%`;
}

function stopAudio() {
  if (_wsRetryTimer) {
    clearTimeout(_wsRetryTimer);
    _wsRetryTimer = null;
  }
  _wsRetryCount = 0;

  if (audioSocket) {
    try {
      audioSocket.onopen = null;
      audioSocket.onmessage = null;
      audioSocket.onerror = null;
      audioSocket.onclose = null;
      audioSocket.close();
    } catch (e) {}
    audioSocket = null;
  }

  const audioTag = document.getElementById("live-audio-player");
  if (audioTag) {
    audioTag.pause();
    audioTag.src = "";
    audioTag.load();
  }

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
      err.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Stream Offline. Retrying...`;
      this.parentElement.appendChild(err);
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

function openAudioModal() {
  const modal = document.getElementById("audio-modal");
  if (!modal) return;

  modal.classList.remove("hidden");
  populateAudioLogs();
  updateAudioModalStatus();

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

function populateAudioLogs() {
  const container = document.getElementById("audio-events-list");
  if (!container) return;

  container.innerHTML = "";

  if (!audioLogs || audioLogs.length === 0) {
    container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px;">No audio events recorded</div>';
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

function animateSpectrogram(canvas) {
  if (!canvas || !canvas.parentElement) return;

  _readAudioVizFromUI();

  const zoomX = Math.max(0.25, Number(AUDIO_VIZ.zoomX) || 1.0);
  const zoomY = Math.max(0.25, Number(AUDIO_VIZ.zoomY) || 1.0);

  WAVE_VIEW_SECONDS = Math.min(Math.max(5.0 / zoomX, 0.25), AUDIO_RING_SECONDS);
  WAVE_GAIN = Math.min(Math.max(zoomY, 0.25), 10.0);

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
  ctx.fillStyle = "#000000";
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
      let localMin = 1,
        localMax = -1;

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
      let localMin = 1,
        localMax = -1;

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

    ctx.fillStyle = "rgba(255,255,255,0.75)";
    ctx.font = "11px Roboto Mono, monospace";

    const yTicks = [-1, -0.5, 0, 0.5, 1];
    for (const tTick of yTicks) {
      const yy = y0 + plotH / 2 - (tTick * plotH) / 2;

      ctx.strokeStyle = "rgba(255,255,255,0.25)";
      ctx.beginPath();
      ctx.moveTo(x0 - 5, yy);
      ctx.lineTo(x0, yy);
      ctx.stroke();

      ctx.fillStyle = "rgba(255,255,255,0.70)";
      ctx.fillText(tTick.toFixed(1), 10, yy + 4);
    }

    const totalSec = WAVE_VIEW_SECONDS;
    const tickCount = 6;
    for (let i = 0; i < tickCount; i++) {
      const frac = i / (tickCount - 1);
      const xx = x0 + frac * plotW;
      const sec = frac * totalSec;

      ctx.strokeStyle = "rgba(255,255,255,0.25)";
      ctx.beginPath();
      ctx.moveTo(xx, y1);
      ctx.lineTo(xx, y1 + 5);
      ctx.stroke();

      ctx.fillStyle = "rgba(255,255,255,0.70)";
      ctx.fillText(`${sec.toFixed(2)}s`, xx - 14, y1 + 18);
    }

    ctx.fillStyle = "rgba(255,255,255,0.75)";
    ctx.font = "12px Poppins, sans-serif";
    ctx.fillText("Time (s)", x0 + plotW / 2 - 28, height - 10);

    ctx.save();
    ctx.translate(18, y0 + plotH / 2 + 28);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("Amplitude", 0, 0);
    ctx.restore();
  }

  specAnimationId = requestAnimationFrame(() => animateSpectrogram(canvas));
}

function closeAlertModal() {
  setAlertAcknowledged(true);
  document.getElementById("alert-modal")?.classList.add("hidden");
  document.getElementById("alert-notification")?.classList.add("hidden");
  stopAlarmSound();
}

function viewAudioLogs() {
  closeAlertModal();
  openAudioModal();
}

function showAcousticAlert(message, title = "🚨 HIVE ALERT!") {
  const notif = document.getElementById("alert-notification");
  const modal = document.getElementById("alert-modal");
  const msg = document.getElementById("alert-modal-message");
  const ts = document.getElementById("alert-timestamp");
  const alertMessage = document.getElementById("alert-message");
  const alertTitle = document.getElementById("alert-modal-title");

  if (alertAcknowledged) {
    if (notif) notif.classList.add("hidden");
    if (modal) modal.classList.add("hidden");
    stopAlarmSound();
    return;
  }

  if (notif) notif.classList.remove("hidden");
  if (modal) modal.classList.remove("hidden");
  if (msg) msg.innerText = message;
  if (ts) ts.innerText = `⏰ ${new Date().toLocaleTimeString()}`;
  if (alertMessage) alertMessage.innerText = message;
  if (alertTitle) alertTitle.innerText = title;

  if (SETTINGS.audioEnabled) playAlarmSound();
}

function playAlarmSound() {
  if (alarmInterval) return;

  const playPulse = () => {
    try {
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ac.createOscillator();
      const g = ac.createGain();

      osc.connect(g);
      g.connect(ac.destination);

      osc.frequency.value = 800;
      g.gain.setValueAtTime(0.3, ac.currentTime);

      osc.start();
      osc.stop(ac.currentTime + 0.2);

      activeOscillators.push(osc);
      setTimeout(() => {
        activeOscillators = [];
      }, 300);
    } catch (e) {}
  };

  playPulse();
  alarmInterval = setInterval(playPulse, 1000);
}

function stopAlarmSound() {
  if (alarmInterval) {
    clearInterval(alarmInterval);
    alarmInterval = null;
  }

  activeOscillators.forEach((osc) => {
    try {
      osc.stop();
    } catch (e) {}
  });
  activeOscillators = [];
}

function openLinksModal(key) {
  const data = BEE_RESOURCES[key];
  if (!data) return;

  document.getElementById("links-modal-title").innerText = data.title;
  const list = document.getElementById("links-list-container");
  list.innerHTML = "";

  data.links.forEach((l) => {
    const a = document.createElement("a");
    a.href = l.url;
    a.target = "_blank";
    a.className = "resource-item";
    a.innerHTML = `<div class="res-info"><h4>${l.name}</h4><p>Click to view</p></div><i class="${l.icon} res-icon"></i>`;
    list.appendChild(a);
  });

  document.getElementById("links-modal").classList.remove("hidden");
}

function closeLinksModal() {
  document.getElementById("links-modal").classList.add("hidden");
}

function previewImage(input) {
  if (input.files && input.files[0]) {
    const reader = new FileReader();
    reader.onload = function (e) {
      document.getElementById("avatar-preview").src = e.target.result;
    };
    reader.readAsDataURL(input.files[0]);
  }
}

function openSensorModal() {}
function closeSensorModal() {
  document.getElementById("sensor-modal").classList.add("hidden");
}

document.addEventListener("DOMContentLoaded", () => {
  const landingSwarm = document.getElementById("landing-swarm");
  if (landingSwarm) createSwarm(landingSwarm, "landing-bee", 30);

  const dashboardSwarm = document.getElementById("dashboard-swarm");
  if (dashboardSwarm) createSwarm(dashboardSwarm, "dashboard-bee", 20);

  const bee = document.getElementById("bee-tracker");
  if (bee) {
    document.addEventListener("mousemove", (e) => {
      const x = (window.innerWidth - e.pageX) / 25;
      const y = (window.innerHeight - e.pageY) / 25;
      bee.style.transform = `translateX(${x}px) translateY(${y}px)`;
    });
  }

  loadSettings();
  loadAlertState();
  updateAudioButtonState(false);
  updateAudioCardStatus("Normal", "#2ecc71");
  updateAudioBadge("Listening");
  updateCameraCardStatus("Bee", "#2ecc71");
  updateCameraBadge("● Live", "blink-red");
  updateAudioModalStatus();
  updateVideoModalStatus();
  populateVideoLogs();

  const zoomX = document.getElementById("audio-zoom-x");
  const zoomY = document.getElementById("audio-zoom-y");
  if (zoomX) zoomX.addEventListener("input", _readAudioVizFromUI);
  if (zoomY) zoomY.addEventListener("input", _readAudioVizFromUI);

  if (localStorage.getItem("hive_isLoggedIn") === "true") {
    document.getElementById("landing-view").classList.add("hidden");
    document.getElementById("dashboard-view").classList.remove("hidden");
    document.getElementById("mobile-nav-bar").classList.remove("hidden");

    loadProfile();
    initChart();
    startDataUpdates();
    setInterval(updateCamTime, 1000);
    updateData();
    setupVideoErrorHandling();
  }

  window.addEventListener("beforeunload", () => {
    stopAudio();
    stopVideoStream();
  });
});

function saveProfile() {
  const updatedProfile = {
    name: document.getElementById("edit-name").value || "Glenda",
    role: document.getElementById("edit-role").value || "4th Year ECE Student",
    credentials: document.getElementById("edit-credentials").value || "Authorized Personnel",
    avatar: document.getElementById("avatar-preview").src,
  };

  localStorage.setItem("hive_profile", JSON.stringify(updatedProfile));
  loadProfile();
  alert("✅ Changes Saved! Profile updated across the dashboard.");
}

function deleteVideoLogs() {
  videoLogs = [];
  populateVideoLogs();
}

function deleteAudioLogs() {
  audioLogs = [];
  populateAudioLogs();
}
