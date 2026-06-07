import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from downloader import run_download, COOKIES_PATH

app = FastAPI(title="GoAudioGo", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

jobs: Dict[str, Dict[str, Any]] = {}

FILE_TTL_SECONDS = int(os.environ.get("FILE_TTL", 2 * 3600))  # 2 hours default


class DownloadRequest(BaseModel):
    url: str


# ── API routes (must be defined before static mount) ────────────────────────

@app.get("/ping")
async def ping():
    return {"status": "ok"}


def _fmt_duration(seconds) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@app.get("/api/search")
async def search_youtube(q: str, limit: int = 8):
    q = q.strip()
    if not q:
        return []

    loop = asyncio.get_event_loop()

    def do_search():
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        if COOKIES_PATH.exists():
            ydl_opts["cookiefile"] = str(COOKIES_PATH)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
            entries = raw.get("entries", []) if raw else []
            return [
                {
                    "id": e["id"],
                    "title": e.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                    "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
                    "duration": _fmt_duration(e.get("duration")),
                    "channel": e.get("channel") or e.get("uploader", ""),
                }
                for e in entries
                if e.get("id")
            ]

    try:
        return await loop.run_in_executor(None, do_search)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cookies/status")
async def cookies_status():
    return {"configured": COOKIES_PATH.exists()}


@app.post("/api/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Archivo vacío")
    COOKIES_PATH.write_bytes(content)
    return {"status": "ok"}


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL requerida")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "url": url,
        "status": "pending",
        "progress": 0,
        "title": "",
        "file": None,
        "files": [],
        "error": None,
        "is_playlist": False,
    }

    asyncio.create_task(run_download(url, job_id, jobs, DOWNLOADS_DIR))
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job no encontrado")

    async def event_stream():
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job no encontrado'})}\n\n"
                break

            payload = {k: v for k, v in job.items() if k != "file"}
            yield f"data: {json.dumps(payload)}\n\n"

            if job["status"] in ("done", "error"):
                break

            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Descarga no completada")

    file_path = job.get("file")
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    path = Path(file_path)
    media_type = "audio/mpeg" if path.suffix == ".mp3" else "application/zip"
    return FileResponse(
        str(path),
        filename=path.name,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ── Static frontend (served last so API routes take priority) ────────────────
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── Background cleanup ───────────────────────────────────────────────────────

async def _cleanup_loop():
    while True:
        await asyncio.sleep(30 * 60)  # every 30 minutes
        cutoff = time.time() - FILE_TTL_SECONDS
        for job_dir in DOWNLOADS_DIR.iterdir():
            if job_dir.is_dir() and job_dir.stat().st_mtime < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
                jobs.pop(job_dir.name, None)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_loop())


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"GoAudioGo → http://localhost:{port}")
    uvicorn.run("main:app", host=host, port=port, reload=False)
