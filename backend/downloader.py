import asyncio
import json
import os
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Any

import yt_dlp

SPOTDL_BIN = shutil.which("spotdl") or "/root/.local/bin/spotdl"
COOKIES_PATH = Path(os.environ.get("COOKIES_PATH", Path(__file__).parent.parent / "cookies.txt"))

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.adminforge.de",
]


def is_spotify_url(url: str) -> bool:
    return "open.spotify.com" in url


def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _extract_video_id(url: str) -> str | None:
    m = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


def _piped_get_audio(video_id: str) -> tuple[str, str]:
    """Llama a la API de Piped para obtener la URL del stream de audio. Prueba varias instancias."""
    for base in PIPED_INSTANCES:
        try:
            req = urllib.request.Request(
                f"{base}/streams/{video_id}",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read())
            title = data.get("title", "audio")
            streams = [s for s in data.get("audioStreams", []) if s.get("url")]
            if streams:
                best = max(streams, key=lambda s: s.get("bitrate", 0))
                print(f"[piped] OK {base} — {title}")
                return best["url"], title
        except Exception as exc:
            print(f"[piped] {base} falló: {exc}")
    return "", ""


async def run_download(url: str, job_id: str, jobs: Dict[str, Any], downloads_dir: Path, title: str = ""):
    job_dir = downloads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    jobs[job_id]["status"] = "downloading"

    try:
        if is_spotify_url(url):
            await _download_spotdl(url, job_id, jobs, job_dir, title)
        elif is_youtube_url(url):
            await _download_youtube(url, job_id, jobs, job_dir, title)
        else:
            await _download_ytdlp(url, job_id, jobs, job_dir)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


async def _download_youtube(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path, title: str = ""):
    video_id = _extract_video_id(url)
    if not video_id:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "URL de YouTube inválida"
        return

    jobs[job_id]["title"] = title or "Obteniendo audio..."

    loop = asyncio.get_event_loop()
    stream_url, stream_title = await loop.run_in_executor(None, _piped_get_audio, video_id)

    if not stream_url:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "No se pudo obtener el audio. Intenta de nuevo en unos segundos."
        return

    display_title = title or stream_title or "audio"
    jobs[job_id]["title"] = display_title
    jobs[job_id]["status"] = "converting"
    jobs[job_id]["progress"] = 40

    safe_name = re.sub(r'[^\w\s\-]', '', display_title).strip()[:80] or "audio"
    output_path = job_dir / f"{safe_name}.mp3"

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-i", stream_url,
        "-vn", "-acodec", "libmp3lame", "-ab", "192k", "-ar", "44100",
        str(output_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0 or not output_path.exists():
        err = stderr.decode("utf-8", errors="replace")[-300:]
        print(f"[ffmpeg error] {err}")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "Error al convertir el audio. Intenta con otra canción."
        return

    jobs[job_id]["file"] = str(output_path)
    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"


async def _download_spotdl(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path, title: str = ""):
    jobs[job_id]["title"] = "Buscando en Spotify..."

    output_template = str(job_dir / "{title}.{output-ext}")
    cmd = [SPOTDL_BIN, url, "--output", output_template, "--format", "mp3", "--bitrate", "192k"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_lines = []
    progress = 10

    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        stdout_lines.append(line)
        if any(k in line for k in ("Downloading", "Downloaded", "Processing")):
            progress = min(progress + 15, 85)
            jobs[job_id]["progress"] = progress
        if '"' in line:
            parts = line.split('"')
            if len(parts) >= 2 and parts[1]:
                jobs[job_id]["title"] = parts[1]

    stderr_out = (await proc.stderr.read()).decode("utf-8", errors="replace")
    await proc.wait()

    if proc.returncode != 0:
        detail = (stderr_out or "\n".join(stdout_lines))[:300]
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"Error de spotdl: {detail}"
        return

    mp3_files = sorted(job_dir.rglob("*.mp3"))

    if not mp3_files:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "No se encontró la canción en Spotify."
        return

    if len(mp3_files) == 1:
        jobs[job_id]["file"] = str(mp3_files[0])
        jobs[job_id]["title"] = mp3_files[0].stem
    else:
        zip_path = job_dir / "spotify.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in mp3_files:
                zf.write(f, f.name)
        jobs[job_id]["file"] = str(zip_path)
        jobs[job_id]["files"] = [f.name for f in mp3_files]
        jobs[job_id]["title"] = f"{len(mp3_files)} canciones"

    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"


async def _download_ytdlp(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    """Para SoundCloud, Twitch, Vimeo y otros (no YouTube ni Spotify)."""
    loop = asyncio.get_event_loop()

    def progress_hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                jobs[job_id]["progress"] = min(int((downloaded / total) * 80), 80)
            info = d.get("info_dict", {})
            if info.get("title") and not jobs[job_id]["title"]:
                jobs[job_id]["title"] = info["title"]
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 80
            jobs[job_id]["status"] = "converting"

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and not jobs[job_id]["title"]:
                jobs[job_id]["title"] = info.get("title", "Audio")

    await loop.run_in_executor(None, do_download)

    mp3_files = sorted(job_dir.glob("*.mp3"))
    if not mp3_files:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "No se generó el MP3. Comprueba que la URL sea válida."
        return

    jobs[job_id]["file"] = str(mp3_files[0])
    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"
