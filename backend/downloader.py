import asyncio
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Any

import aiohttp
import yt_dlp

SPOTDL_BIN = shutil.which("spotdl") or "/root/.local/bin/spotdl"
COOKIES_PATH = Path(os.environ.get("COOKIES_PATH", Path(__file__).parent.parent / "cookies.txt"))

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://pipedapi.syncpundit.io",
    "https://api-piped.mha.fi",
    "https://piped-api.lunar.icu",
    "https://watchapi.whatever.social",
    "https://pipedapi.in.projectsegfau.lt",
    "https://pipedapi.drgns.space",
    "https://piped.yt/",
]

INVIDIOUS_INSTANCES = [
    "https://inv.riverside.rocks",
    "https://yt.artemislena.eu",
    "https://iv.ggtyler.dev",
    "https://invidious.privacydev.net",
    "https://invidious.tiekoetter.com",
    "https://invidious.lunar.icu",
    "https://invidious.nerdvpn.de",
    "https://invidious.fdn.fr",
]


def is_spotify_url(url: str) -> bool:
    return "open.spotify.com" in url


def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _extract_video_id(url: str) -> str | None:
    m = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else None


def _clean_title(title: str) -> str:
    cleaned = re.sub(
        r'\s*[\(\[【].*?(?:oficial|official|video|clip|remaster|lyric|audio|4k|hd|hq|mv|live|vevo|ft\.|feat\.).*?[\)\]】]',
        '', title, flags=re.IGNORECASE
    )
    cleaned = re.sub(r'\s*[\(\[【][^\)\]】]{0,15}[\)\]】]$', '', cleaned)
    return cleaned.strip() or title


async def _try_piped(session: aiohttp.ClientSession, base: str, video_id: str) -> tuple[str, str]:
    try:
        async with session.get(f"{base}/streams/{video_id}", timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return "", ""
            data = await r.json(content_type=None)
        title = data.get("title", "audio")
        streams = [s for s in data.get("audioStreams", []) if s.get("url")]
        if streams:
            best = max(streams, key=lambda s: s.get("bitrate", 0))
            return best["url"], title
    except Exception:
        pass
    return "", ""


async def _try_invidious(session: aiohttp.ClientSession, base: str, video_id: str) -> tuple[str, str]:
    try:
        async with session.get(
            f"{base}/api/v1/videos/{video_id}?fields=adaptiveFormats,title",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return "", ""
            data = await r.json(content_type=None)
        title = data.get("title", "audio")
        formats = [f for f in data.get("adaptiveFormats", []) if f.get("type", "").startswith("audio")]
        if formats:
            best = max(formats, key=lambda f: int(f.get("bitrate", 0)))
            url = best.get("url", "")
            if url:
                return url, title
    except Exception:
        pass
    return "", ""


async def _get_audio_stream(video_id: str) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GoAudioGo/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        piped_tasks = [_try_piped(session, base, video_id) for base in PIPED_INSTANCES]
        invidious_tasks = [_try_invidious(session, base, video_id) for base in INVIDIOUS_INSTANCES]
        results = await asyncio.gather(*piped_tasks, *invidious_tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, tuple) and r[0]:
            print(f"[stream] OK: {r[1]}")
            return r
    return "", ""


async def run_download(url: str, job_id: str, jobs: Dict[str, Any], downloads_dir: Path, title: str = ""):
    job_dir = downloads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    jobs[job_id]["status"] = "downloading"

    try:
        if is_spotify_url(url):
            await _download_spotdl(url, job_id, jobs, job_dir)
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

    # ── Paso 1: Piped / Invidious en paralelo ──────────────────────────────────
    stream_url, stream_title = await _get_audio_stream(video_id)

    if stream_url:
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

        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 500_000:
            jobs[job_id]["file"] = str(output_path)
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "done"
            return

        err = stderr.decode("utf-8", errors="replace")[-200:]
        print(f"[piped→ffmpeg] falló o archivo muy pequeño: {err}")

    # ── Paso 2: yt-dlp directo (con cookies si hay) ────────────────────────────
    print(f"[fallback-ytdlp] video_id={video_id}")
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 10
    try:
        before = set(job_dir.glob("*.mp3"))
        await _run_ytdlp_youtube(url, job_id, jobs, job_dir)
        after = set(job_dir.glob("*.mp3"))
        new_files = sorted(after - before)
        if new_files:
            jobs[job_id]["file"] = str(new_files[0])
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "done"
            return
        if jobs[job_id].get("status") == "done":
            return
    except Exception as exc:
        print(f"[ytdlp] error: {exc}")
        jobs[job_id]["status"] = "downloading"

    # ── Paso 3: SoundCloud (último recurso) ────────────────────────────────────
    if title:
        clean = _clean_title(title)
        print(f"[fallback-soundcloud] '{clean}'")
        jobs[job_id]["title"] = "Buscando en SoundCloud..."
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["progress"] = 5
        try:
            before = set(job_dir.glob("*.mp3"))
            await _download_ytdlp(f"scsearch1:{clean}", job_id, jobs, job_dir)
            after = set(job_dir.glob("*.mp3"))
            new_files = sorted(after - before)
            if new_files:
                mp3 = new_files[0]
                if mp3.stat().st_size < 500_000:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = (
                        "Solo hay preview de 30s en SoundCloud. "
                        "Sube cookies actualizadas de YouTube o pega un link de Spotify."
                    )
                else:
                    jobs[job_id]["file"] = str(mp3)
                    jobs[job_id]["progress"] = 100
                    jobs[job_id]["status"] = "done"
            return
        except Exception as exc:
            print(f"[soundcloud] error: {exc}")

    jobs[job_id]["status"] = "error"
    jobs[job_id]["error"] = (
        "No se pudo descargar. Prueba: 1) actualizar cookies de YouTube, "
        "2) pegar un link de Spotify, o 3) buscar en SoundCloud directamente."
    )


async def _run_ytdlp_youtube(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    """yt-dlp directo contra YouTube, con cookies si están disponibles."""
    loop = asyncio.get_event_loop()

    def progress_hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                jobs[job_id]["progress"] = min(int((downloaded / total) * 80), 80)
            info = d.get("info_dict", {})
            if info.get("title") and not jobs[job_id].get("title"):
                jobs[job_id]["title"] = info["title"]
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 80
            jobs[job_id]["status"] = "converting"

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }

    if COOKIES_PATH.exists():
        ydl_opts["cookiefile"] = str(COOKIES_PATH)
        print(f"[ytdlp-yt] con cookies ({COOKIES_PATH.stat().st_size} bytes)")
    else:
        print("[ytdlp-yt] sin cookies")

    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and not jobs[job_id].get("title"):
                jobs[job_id]["title"] = info.get("title", "Audio")

    await loop.run_in_executor(None, do_download)


async def _download_spotdl(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
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
    """Para SoundCloud, Twitch, Vimeo y búsquedas scsearch:"""
    loop = asyncio.get_event_loop()

    def progress_hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                jobs[job_id]["progress"] = min(int((downloaded / total) * 80), 80)
            info = d.get("info_dict", {})
            if info.get("title") and not jobs[job_id].get("title"):
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
        "noplaylist": True,
    }

    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and not jobs[job_id].get("title"):
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
