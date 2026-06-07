import asyncio
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Any

import yt_dlp

# pipx instala en /root/.local/bin — ruta explícita como fallback
SPOTDL_BIN = shutil.which("spotdl") or "/root/.local/bin/spotdl"
COOKIES_PATH = Path(os.environ.get("COOKIES_PATH", Path(__file__).parent.parent / "cookies.txt"))


def is_spotify_url(url: str) -> bool:
    return "open.spotify.com" in url


async def run_download(url: str, job_id: str, jobs: Dict[str, Any], downloads_dir: Path):
    job_dir = downloads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    jobs[job_id]["status"] = "downloading"

    try:
        if is_spotify_url(url):
            await _download_spotify(url, job_id, jobs, job_dir)
        else:
            await _download_ytdlp(url, job_id, jobs, job_dir)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


async def _download_ytdlp(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    loop = asyncio.get_event_loop()

    def progress_hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total and total > 0:
                jobs[job_id]["progress"] = min(int((downloaded / total) * 80), 80)
            info = d.get("info_dict", {})
            if info.get("title") and not jobs[job_id]["title"]:
                jobs[job_id]["title"] = info["title"]
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 80
            jobs[job_id]["status"] = "converting"

    ydl_opts = {
        # m4a primero (compatible con iOS/Android client), luego webm, luego lo que haya
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        # Probar iOS → Android → web en orden; iOS/Android evitan el bot-check en muchos casos
        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "ios", "android", "mweb", "web"]}},
    }

    if COOKIES_PATH.exists():
        ydl_opts["cookiefile"] = str(COOKIES_PATH)

    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                if info.get("_type") == "playlist":
                    jobs[job_id]["is_playlist"] = True
                    jobs[job_id]["title"] = info.get("title", "Playlist")
                elif not jobs[job_id]["title"]:
                    jobs[job_id]["title"] = info.get("title", "Audio")

    await loop.run_in_executor(None, do_download)

    jobs[job_id]["progress"] = 95
    mp3_files = sorted(job_dir.glob("*.mp3"))

    if not mp3_files:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = (
            "YouTube bloqueó la descarga. Sube tus cookies en ⚙ Cookies de YouTube."
            if "youtube" in url or "youtu.be" in url
            else "No se generó el MP3. Comprueba que la URL sea válida."
        )
        return

    if len(mp3_files) == 1:
        jobs[job_id]["file"] = str(mp3_files[0])
    else:
        title = jobs[job_id].get("title", "playlist")
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_")[:60]
        zip_path = job_dir / f"{safe_title}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in mp3_files:
                zf.write(f, f.name)
        jobs[job_id]["file"] = str(zip_path)
        jobs[job_id]["files"] = [f.name for f in mp3_files]

    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"


async def _download_spotify(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    jobs[job_id]["title"] = "Buscando en Spotify..."

    # {title}.{output-ext} para que spotdl guarde como nombre-cancion.mp3 en job_dir
    output_template = str(job_dir / "{title}.{output-ext}")

    proc = await asyncio.create_subprocess_exec(
        SPOTDL_BIN,
        url,
        "--output", output_template,
        "--format", "mp3",
        "--bitrate", "192k",
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

    mp3_files = sorted(job_dir.glob("*.mp3"))

    if not mp3_files:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "spotdl no encontró el archivo. Verifica que la URL de Spotify sea pública."
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
