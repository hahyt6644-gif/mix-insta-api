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
# For Render/Linux, 'ffmpeg' is usually in the PATH. 
# If you are including the binaries in your repo, keep them as "./ffmpeg"
FFMPEG = "./ffmpeg"
FFPROBE = "./ffprobe"

UPLOAD_DIR = "Upload"
MEME_DIR = "meme"
OUTPUT_DIR = "."
RETENTION_TIME = 600  # 10 minutes in seconds

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

# ---------- CLEANUP WORKER ----------
def cleanup_worker():
    """Background thread that deletes final videos after 10 minutes."""
    while True:
        now = time.time()
        to_delete = []
        
        # Check all tasks
        for task_id, info in list(TASKS.items()):
            # If the video was finished more than 10 minutes ago
            if info.get("status") == "done" and info.get("completed_at"):
                if now - info["completed_at"] > RETENTION_TIME:
                    file_path = os.path.join(OUTPUT_DIR, info["file"])
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except:
                            pass
                    to_delete.append(task_id)
            
            # Also clean up failed tasks after 10 mins
            elif info.get("status") == "failed" and now - info["created"] > RETENTION_TIME:
                to_delete.append(task_id)
        
        for tid in to_delete:
            del TASKS[tid]
            
        time.sleep(30) # Run check every 30 seconds

# Start the cleanup thread
threading.Thread(target=cleanup_worker, daemon=True).start()

# ---------- WORKER ----------
def process_task(task_id, main_url, meme_url):
    main_path = f"{UPLOAD_DIR}/main_{task_id}.mp4"
    meme_path = f"{MEME_DIR}/meme_{task_id}.mp4"
    output = f"final_{task_id}.mp4"
    output_full_path = os.path.join(OUTPUT_DIR, output)

    try:
        TASKS[task_id]["status"] = "processing"

        # 1. Download inputs
        download(main_url, main_path)
        download(meme_url, meme_path)

        # 2. FFmpeg Filter Logic for 9:16 Shorts (1080x1920)
        # [0:v] is Main, [1:v] is Meme
        # We scale both to 1080 width, then overlay them on a black 1080x1920 canvas
        filter_complex = (
            # Scale main video to 1080 width, keep aspect ratio
            "[0:v]scale=1080:-1[vid];" 
            # Scale meme to 1080 width
            "[1:v]scale=1080:-1[meme];"
            # Create a black 9:16 canvas
            "color=s=1080x1920:c=black[bg];"
            # Put main video in the middle (vertically centered)
            "[bg][vid]overlay=0:(1920-h)/2[temp];"
            # Put meme at the very bottom
            "[temp][meme]overlay=0:0[v];"
            
            # Mix audio from both (duration matches the main video)
            "[0:a][1:a]amix=inputs=2:duration=first[a]"
        )

        cmd = [
            FFMPEG, "-y",
            "-i", main_path,
            "-i", meme_path,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest", # Ensures video ends when the main content ends
            output_full_path
        ]

        run_cmd(cmd)

        TASKS[task_id].update({
            "status": "done",
            "file": output,
            "completed_at": time.time()
        })

    except Exception as e:
        TASKS[task_id].update({"status": "failed", "error": str(e)})
    
    finally:
        # Cleanup
        for f in (main_path, meme_path):
            if os.path.exists(f): os.remove(f)

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
        
        # Calculate time left for user to download
        elapsed = time.time() - task.get("completed_at", 0)
        task["seconds_until_deletion"] = max(0, int(RETENTION_TIME - elapsed))

    return task


@app.get("/")
def home():
    return {"service": "mix-insta-api (python)", "status": "ok", "auto_cleanup": "10 minutes"}
