import asyncio
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


# ── cobalt.tools ──────────────────────────────────────────────────────────────

async def _try_cobalt(youtube_url: str) -> tuple[str, str]:
    """Pide a cobalt.tools el MP3 de un vídeo de YouTube.
    cobalt descarga desde sus propios servidores (no datacenter), así que
    no le afecta el bloqueo de IPs de Render.
    Devuelve (url_descarga, nombre_archivo) o ("", "").
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.cobalt.tools/",
                json={
                    "url": youtube_url,
                    "downloadMode": "audio",
                    "audioFormat": "mp3",
                    "audioBitrate": "192",
                    "filenameStyle": "basic",
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; GoAudioGo/1.0)",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                print(f"[cobalt] HTTP {r.status}")
                if r.status not in (200, 201):
                    text = await r.text()
                    print(f"[cobalt] body: {text[:200]}")
                    return "", ""
                data = await r.json(content_type=None)

        status = data.get("status", "")
        dl_url = data.get("url", "")
        filename = data.get("filename", "audio.mp3")

        if status in ("tunnel", "redirect", "stream") and dl_url:
            print(f"[cobalt] OK {status}: {filename}")
            return dl_url, filename

        print(f"[cobalt] no usable: status={status} data={str(data)[:300]}")
    except Exception as exc:
        print(f"[cobalt] excepción: {exc}")
    return "", ""


async def _download_from_cobalt_url(
    dl_url: str, filename: str, job_id: str, jobs: Dict[str, Any], job_dir: Path
) -> bool:
    """Descarga el fichero que devuelve cobalt, actualiza progreso. True = éxito."""
    safe_stem = re.sub(r'[^\w\s\-]', '', Path(filename).stem).strip()[:80] or "audio"
    output_path = job_dir / f"{safe_stem}.mp3"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                dl_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; GoAudioGo/1.0)"},
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                if r.status != 200:
                    print(f"[cobalt-dl] HTTP {r.status}")
                    return False

                total = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                with open(output_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(16384):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            jobs[job_id]["progress"] = min(int(downloaded / total * 90), 90)

        size = output_path.stat().st_size if output_path.exists() else 0
        if size < 500_000:
            print(f"[cobalt-dl] archivo muy pequeño: {size} bytes")
            if output_path.exists():
                output_path.unlink()
            return False

        jobs[job_id]["file"] = str(output_path)
        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = "done"
        return True

    except Exception as exc:
        print(f"[cobalt-dl] error: {exc}")
        if output_path.exists():
            output_path.unlink()
        return False


# ── Piped / Invidious ─────────────────────────────────────────────────────────

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
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return "", ""
            data = await r.json(content_type=None)
        title = data.get("title", "audio")
        formats = [f for f in data.get("adaptiveFormats", []) if f.get("type", "").startswith("audio")]
        if formats:
            best = max(formats, key=lambda f: int(f.get("bitrate", 0)))
            u = best.get("url", "")
            if u:
                return u, title
    except Exception:
        pass
    return "", ""


async def _get_piped_stream(video_id: str) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GoAudioGo/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = (
            [_try_piped(session, b, video_id) for b in PIPED_INSTANCES] +
            [_try_invidious(session, b, video_id) for b in INVIDIOUS_INSTANCES]
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, tuple) and r[0]:
            print(f"[piped] OK: {r[1]}")
            return r
    return "", ""


# ── Entrada principal ─────────────────────────────────────────────────────────

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
            await _download_generic(url, job_id, jobs, job_dir)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


async def _download_youtube(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path, title: str = ""):
    video_id = _extract_video_id(url)
    if not video_id:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "URL de YouTube inválida"
        return

    display_title = title or "Obteniendo audio..."
    jobs[job_id]["title"] = display_title

    # ── 1. cobalt.tools ────────────────────────────────────────────────────────
    # cobalt descarga desde sus propios servidores → no le afecta el bloqueo de Render
    print(f"[youtube] intentando cobalt para {video_id}")
    cobalt_url, cobalt_filename = await _try_cobalt(url)
    if cobalt_url:
        stem = cobalt_filename.replace(".mp3", "").strip() or _clean_title(title) or "audio"
        jobs[job_id]["title"] = stem
        jobs[job_id]["status"] = "downloading"
        if await _download_from_cobalt_url(cobalt_url, f"{stem}.mp3", job_id, jobs, job_dir):
            return
        print("[cobalt] descarga falló, probando Piped/Invidious...")

    # ── 2. Piped / Invidious → ffmpeg ──────────────────────────────────────────
    jobs[job_id]["title"] = display_title
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 5
    stream_url, stream_title = await _get_piped_stream(video_id)

    if stream_url:
        disp = title or stream_title or "audio"
        jobs[job_id]["title"] = disp
        jobs[job_id]["status"] = "converting"
        jobs[job_id]["progress"] = 40

        safe = re.sub(r'[^\w\s\-]', '', disp).strip()[:80] or "audio"
        out = job_dir / f"{safe}.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "-i", stream_url,
            "-vn", "-acodec", "libmp3lame", "-ab", "192k", "-ar", "44100",
            str(out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 500_000:
            jobs[job_id]["file"] = str(out)
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "done"
            return
        print(f"[piped→ffmpeg] falló: {stderr.decode(errors='replace')[-200:]}")

    # ── 3. yt-dlp directo con cookies ──────────────────────────────────────────
    print(f"[youtube] fallback yt-dlp video_id={video_id}")
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 5
    try:
        before = set(job_dir.glob("*.mp3"))
        await _run_ytdlp_youtube(url, job_id, jobs, job_dir)
        after = set(job_dir.glob("*.mp3"))
        new = sorted(after - before)
        if new:
            jobs[job_id]["file"] = str(new[0])
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "done"
            return
        if jobs[job_id].get("status") == "done":
            return
    except Exception as exc:
        print(f"[ytdlp] error: {exc}")
        jobs[job_id]["status"] = "downloading"

    # ── 4. SoundCloud (último recurso) ─────────────────────────────────────────
    if title:
        clean = _clean_title(title)
        print(f"[soundcloud] búsqueda: '{clean}'")
        jobs[job_id]["title"] = f"Buscando '{clean}' en SoundCloud..."
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["progress"] = 5
        try:
            before = set(job_dir.glob("*.mp3"))
            await _download_generic(f"scsearch1:{clean}", job_id, jobs, job_dir)
            after = set(job_dir.glob("*.mp3"))
            new = sorted(after - before)
            if new:
                mp3 = new[0]
                if mp3.stat().st_size < 500_000:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = (
                        "YouTube está bloqueado desde el servidor y SoundCloud solo tiene "
                        "un preview de 30s de esta canción. Prueba pegando un link de Spotify."
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
        "No se pudo descargar desde YouTube (IP bloqueada). "
        "Pega un link de Spotify para esta canción."
    )


async def _run_ytdlp_youtube(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    loop = asyncio.get_event_loop()

    def progress_hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            dl = d.get("downloaded_bytes", 0)
            if total:
                jobs[job_id]["progress"] = min(int(dl / total * 80), 80)
            info = d.get("info_dict", {})
            if info.get("title") and not jobs[job_id].get("title"):
                jobs[job_id]["title"] = info["title"]
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 80
            jobs[job_id]["status"] = "converting"

    ydl_opts: dict = {
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

    def do_dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and not jobs[job_id].get("title"):
                jobs[job_id]["title"] = info.get("title", "Audio")

    await loop.run_in_executor(None, do_dl)


# ── Spotify ───────────────────────────────────────────────────────────────────

async def _download_spotdl(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    jobs[job_id]["title"] = "Buscando en Spotify..."

    cmd = [
        SPOTDL_BIN, url,
        "--output", str(job_dir / "{title}.{output-ext}"),
        "--format", "mp3",
        "--bitrate", "192k",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    progress = 10
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
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
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"Error de spotdl: {stderr_out[:300]}"
        return

    mp3s = sorted(job_dir.rglob("*.mp3"))
    if not mp3s:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "No se encontró la canción en Spotify."
        return

    if len(mp3s) == 1:
        jobs[job_id]["file"] = str(mp3s[0])
        jobs[job_id]["title"] = mp3s[0].stem
    else:
        zip_path = job_dir / "spotify.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in mp3s:
                zf.write(f, f.name)
        jobs[job_id]["file"] = str(zip_path)
        jobs[job_id]["files"] = [f.name for f in mp3s]
        jobs[job_id]["title"] = f"{len(mp3s)} canciones"

    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"


# ── SoundCloud / genérico ─────────────────────────────────────────────────────

async def _download_generic(url: str, job_id: str, jobs: Dict[str, Any], job_dir: Path):
    loop = asyncio.get_event_loop()

    def progress_hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            dl = d.get("downloaded_bytes", 0)
            if total:
                jobs[job_id]["progress"] = min(int(dl / total * 80), 80)
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

    def do_dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and not jobs[job_id].get("title"):
                jobs[job_id]["title"] = info.get("title", "Audio")

    await loop.run_in_executor(None, do_dl)

    mp3s = sorted(job_dir.glob("*.mp3"))
    if not mp3s:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "No se generó el MP3. Comprueba que la URL sea válida."
        return

    jobs[job_id]["file"] = str(mp3s[0])
    jobs[job_id]["progress"] = 100
    jobs[job_id]["status"] = "done"
