import uuid
import os
import threading
import time
import subprocess
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# ---------- PATHS ----------
FFMPEG = "./ffmpeg"
FFPROBE = "./ffprobe"

UPLOAD_DIR = "Upload"
MEME_DIR = "meme"
OUTPUT_DIR = "."

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MEME_DIR, exist_ok=True)

# Serve static files at /files/*
app.mount("/files", StaticFiles(directory=".", html=False), name="files")

# ---------- TASK STORE ----------
TASKS = {}

# ---------- INPUT MODEL ----------
class Job(BaseModel):
    main_url: str
    meme_url: str

# ---------- HELPERS ----------
def run_cmd(cmd, timeout=300):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=True
    )

def download(url, dest):
    run_cmd(["curl", "-L", "-o", dest, "--silent", "--show-error", url], timeout=180)

def ffprobe_has_audio(path):
    r = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return bool(r.stdout.decode().strip())

# ---------- WORKER ----------
def process_task(task_id, main_url, meme_url):
    try:
        TASKS[task_id]["status"] = "processing"

        main_path = f"{UPLOAD_DIR}/main_{task_id}.mp4"
        meme_path = f"{MEME_DIR}/meme_{task_id}.mp4"
        fixed_meme = f"{MEME_DIR}/meme_fixed_{task_id}.mp4"
        output = f"{OUTPUT_DIR}/final_{task_id}.mp4"

        # Download inputs
        download(main_url, main_path)
        download(meme_url, meme_path)

        # Get width
        width = subprocess.check_output([
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width",
            "-of", "csv=p=0",
            main_path
        ]).decode().strip() or "720"

        # Scale meme to match width
        run_cmd([
            FFMPEG, "-y",
            "-i", meme_path,
            "-vf", f"scale={width}:-2",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            fixed_meme
        ])

        has_audio = ffprobe_has_audio(main_path)

        # Overlay meme at bottom
        filter_complex = "[0:v][1:v]overlay=0:main_h-overlay_h:format=auto[v]"

        cmd = [
            FFMPEG, "-y",
            "-i", main_path,
            "-i", fixed_meme,
            "-filter_complex", filter_complex,
            "-map", "[v]"
        ]

        if has_audio:
            cmd += ["-map", "0:a", "-c:a", "aac"]

        cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            output
        ]

        run_cmd(cmd)

        # Cleanup
        for f in (main_path, meme_path, fixed_meme):
            if os.path.exists(f):
                os.remove(f)

        TASKS[task_id].update({
            "status": "done",
            "file": os.path.basename(output),
            "has_audio": has_audio,
            "file_size_bytes": os.path.getsize(output)
        })

    except Exception as e:
        TASKS[task_id].update({
            "status": "failed",
            "error": str(e)
        })

# ---------- API ----------
@app.post("/start")
def start(job: Job):
    task_id = str(uuid.uuid4())

    TASKS[task_id] = {
        "status": "queued",
        "created": time.time()
    }

    threading.Thread(
        target=process_task,
        args=(task_id, job.main_url, job.meme_url),
        daemon=True
    ).start()

    return {"status": "queued", "task_id": task_id}


@app.get("/status/{task_id}")
def status(task_id: str, request: Request):
    task = TASKS.get(task_id)
    if not task:
        return {"status": "not_found"}

    if task.get("status") == "done":
        base = str(request.base_url).rstrip("/")
        task["download_url"] = f"{base}/files/{task['file']}"

    return task


@app.get("/")
def home():
    return {"service": "mix-insta-api (python)", "status": "ok"}