import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAnalytics, logEvent } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-analytics.js";

// En local el backend sirve también el frontend → misma origin, sin prefijo.
// En producción (Firebase Hosting) el frontend está en goaudiogo.web.app
// y el backend en Render → necesitamos la URL completa.
const API_BASE = window.location.hostname === "localhost"
    ? ""
    : "https://goaudiogo.onrender.com";

const firebaseConfig = {
    apiKey: "AIzaSyDrm0512NTZZPSpXsUAzSfBSCSQNlzVN6o",
    authDomain: "goaudiogo.firebaseapp.com",
    projectId: "goaudiogo",
    storageBucket: "goaudiogo.firebasestorage.app",
    messagingSenderId: "804150882587",
    appId: "1:804150882587:web:93c772892ee1063952a3be",
    measurementId: "G-Q0Z997V40V",
};

const fbApp = initializeApp(firebaseConfig);
const analytics = getAnalytics(fbApp);

// ── Platform detection ──────────────────────────────────────────────────────

const PLATFORM_ICONS = {
    "youtube.com":       "▶",
    "youtu.be":          "▶",
    "music.youtube.com": "♪",
    "open.spotify.com":  "♫",
    "soundcloud.com":    "◈",
    "twitch.tv":         "◉",
    "vimeo.com":         "◐",
};

function platformIcon(url) {
    for (const [domain, icon] of Object.entries(PLATFORM_ICONS)) {
        if (url.includes(domain)) return icon;
    }
    return "⬡";
}

function platformName(url) {
    for (const domain of Object.keys(PLATFORM_ICONS)) {
        if (url.includes(domain)) return domain.replace(".com", "").replace("open.", "");
    }
    return "web";
}

// ── DOM refs ────────────────────────────────────────────────────────────────

const urlInput       = document.getElementById("urlInput");
const downloadBtn    = document.getElementById("downloadBtn");
const platformIconEl = document.getElementById("platformIcon");

const progressSection = document.getElementById("progressSection");
const successSection  = document.getElementById("successSection");
const errorSection    = document.getElementById("errorSection");

const trackTitle  = document.getElementById("trackTitle");
const trackStatus = document.getElementById("trackStatus");
const progressFill = document.getElementById("progressFill");
const progressPct  = document.getElementById("progressPct");

const successText = document.getElementById("successText");
const saveBtn     = document.getElementById("saveBtn");

const errorText = document.getElementById("errorText");

// ── State ───────────────────────────────────────────────────────────────────

let currentJobId    = null;
let eventSource     = null;
let downloadDone    = false;

// ── Event listeners ─────────────────────────────────────────────────────────

urlInput.addEventListener("input", () => {
    platformIconEl.textContent = platformIcon(urlInput.value);
});

urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") startDownload();
});

downloadBtn.addEventListener("click", startDownload);

document.getElementById("newBtn").addEventListener("click", reset);
document.getElementById("retryBtn").addEventListener("click", () => {
    const prev = urlInput.value;
    reset();
    urlInput.value = prev;
    platformIconEl.textContent = platformIcon(prev);
});

// ── Core logic ───────────────────────────────────────────────────────────────

function reset() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    currentJobId = null;
    downloadDone = false;

    urlInput.value = "";
    platformIconEl.textContent = "⬡";
    downloadBtn.disabled = false;
    downloadBtn.textContent = "Descargar";

    progressSection.classList.remove("visible");
    successSection.classList.remove("visible");
    errorSection.classList.remove("visible");

    progressFill.classList.remove("indeterminate");
    progressFill.style.width = "0%";
    progressPct.textContent = "0%";
}

async function startDownload() {
    const url = urlInput.value.trim();

    if (!url) {
        urlInput.classList.add("shake");
        setTimeout(() => urlInput.classList.remove("shake"), 400);
        urlInput.focus();
        return;
    }

    downloadDone = false;
    downloadBtn.disabled = true;
    downloadBtn.textContent = "Iniciando...";

    progressSection.classList.add("visible");
    successSection.classList.remove("visible");
    errorSection.classList.remove("visible");

    trackTitle.textContent = "Procesando URL...";
    trackStatus.textContent = "Conectando...";
    progressFill.classList.add("indeterminate");
    progressFill.style.width = "0%";
    progressPct.textContent = "0%";

    logEvent(analytics, "download_started", { platform: platformName(url) });

    try {
        const res = await fetch(`${API_BASE}/api/download`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: "Error desconocido" }));
            throw new Error(err.detail || "Error al iniciar la descarga");
        }

        const { job_id } = await res.json();
        currentJobId = job_id;
        downloadBtn.textContent = "Descargando...";
        listenProgress(job_id);

    } catch (err) {
        showError(err.message);
    }
}

function listenProgress(jobId) {
    if (eventSource) eventSource.close();

    eventSource = new EventSource(`${API_BASE}/api/status/${jobId}`);

    eventSource.onmessage = (e) => {
        let job;
        try { job = JSON.parse(e.data); } catch { return; }

        if (job.error && !job.title) {
            eventSource.close();
            showError(job.error);
            return;
        }

        applyProgress(job);

        if (job.status === "done") {
            eventSource.close();
            progressFill.style.width = "100%";
            progressPct.textContent = "100%";
            setTimeout(() => showSuccess(job, jobId), 500);
            logEvent(analytics, "download_completed", { platform: platformName(job.url || "") });
        } else if (job.status === "error") {
            eventSource.close();
            showError(job.error || "Error desconocido");
            logEvent(analytics, "download_failed");
        }
    };

    eventSource.onerror = () => {
        eventSource.close();
        // Only act if we haven't already shown a result
        if (!downloadDone) {
            showError("Se perdió la conexión con el servidor. ¿Está arrancado?");
        }
    };
}

function applyProgress(job) {
    const pct = job.progress || 0;

    if (pct > 0) {
        progressFill.classList.remove("indeterminate");
        progressFill.style.width = `${pct}%`;
        progressPct.textContent = `${pct}%`;
    }

    if (job.title) trackTitle.textContent = job.title;

    const labels = {
        pending:     "Iniciando...",
        downloading: "Descargando audio...",
        converting:  "Convirtiendo a MP3 (puede tardar en vídeos largos)...",
        done:        "¡Completado!",
        error:       "Error",
    };
    trackStatus.textContent = labels[job.status] || job.status;
}

function showSuccess(job, jobId) {
    downloadDone = true;
    progressSection.classList.remove("visible");
    successSection.classList.add("visible");
    downloadBtn.disabled = false;
    downloadBtn.textContent = "Descargar";

    const isZip = job.files && job.files.length > 1;
    const label = isZip ? `${job.files.length} archivos` : `"${job.title || "audio"}"`;
    successText.textContent = `${label} listo para guardar.`;

    saveBtn.href = `${API_BASE}/api/file/${jobId}`;
    saveBtn.setAttribute("download", "");
    saveBtn.textContent = isZip ? "Guardar ZIP" : "Guardar MP3";
}

function showError(message) {
    downloadDone = true;
    progressSection.classList.remove("visible");
    errorSection.classList.add("visible");
    downloadBtn.disabled = false;
    downloadBtn.textContent = "Descargar";
    errorText.textContent = message;
}

// ── Cookies section ─────────────────────────────────────────────────────────

const cookieToggle   = document.getElementById("cookieToggle");
const cookieChevron  = document.getElementById("cookieChevron");
const cookieBody     = document.getElementById("cookieBody");
const cookieStatusDot = document.getElementById("cookieStatusDot");
const cookieFile     = document.getElementById("cookieFile");
const cookieFilename = document.getElementById("cookieFilename");
const cookieSubmit   = document.getElementById("cookieSubmit");
const cookieFeedback = document.getElementById("cookieFeedback");

cookieToggle.addEventListener("click", () => {
    const open = cookieBody.classList.toggle("visible");
    cookieChevron.classList.toggle("open", open);
});

cookieFile.addEventListener("change", () => {
    const f = cookieFile.files[0];
    cookieFilename.textContent = f ? f.name : "Ningún archivo";
    cookieSubmit.disabled = !f;
});

cookieSubmit.addEventListener("click", async () => {
    const f = cookieFile.files[0];
    if (!f) return;

    cookieSubmit.disabled = true;
    cookieFeedback.textContent = "Subiendo...";
    cookieFeedback.className = "cookie-feedback";

    const form = new FormData();
    form.append("file", f);

    try {
        const res = await fetch(`${API_BASE}/api/cookies`, { method: "POST", body: form });
        if (!res.ok) throw new Error("Error al subir");
        cookieFeedback.textContent = "✓ Cookies guardadas. Intenta descargar de nuevo.";
        cookieFeedback.className = "cookie-feedback ok";
        cookieStatusDot.className = "dot dot-on";
    } catch {
        cookieFeedback.textContent = "✕ Error al subir el archivo.";
        cookieFeedback.className = "cookie-feedback err";
    } finally {
        cookieSubmit.disabled = false;
    }
});

async function checkCookiesStatus() {
    if (!API_BASE) return; // local: no hace falta
    try {
        const res = await fetch(`${API_BASE}/api/cookies/status`);
        const { configured } = await res.json();
        cookieStatusDot.className = `dot ${configured ? "dot-on" : "dot-off"}`;
    } catch { /* ignore */ }
}

checkCookiesStatus();

// Render free tier duerme tras 15 min sin uso.
// Hacemos ping al arrancar la página para que ya esté despierto cuando el usuario lo necesite.
if (API_BASE) {
    fetch(`${API_BASE}/ping`, { signal: AbortSignal.timeout(60_000) }).catch(() => {});
}
