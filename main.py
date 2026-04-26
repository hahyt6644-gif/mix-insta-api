import uuid
import os
import threading
import time
import subprocess
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# ---------- PATHS ----------
# Set to "ffmpeg" if using Render's system packages, or "./ffmpeg" if using binaries in repo
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

UPLOAD_DIR = "Upload"
MEME_DIR = "meme"
OUTPUT_DIR = "."
RETENTION_TIME = 600  # 10 minutes

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MEME_DIR, exist_ok=True)

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

def upload_to_catbox(url):
    """Fallback: Asks Catbox to mirror the file if the original server blocks us."""
    try:
        cmd = [
            "curl", "-s", 
            "-F", "reqtype=urlupload", 
            "-F", "userhash=", 
            "-F", f"url={url}", 
            "https://catbox.moe/user/api.php"
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
        catbox_url = result.stdout.strip()
        if catbox_url.startswith("https://"):
            return catbox_url
    except:
        pass
    return None

def download(url, dest):
    """Downloads file with automatic Catbox bypass for Error 52."""
    try:
        run_cmd(["curl", "-L", "-o", dest, "--silent", "--show-error", url], timeout=180)
    except subprocess.CalledProcessError as e:
        if e.returncode == 52:
            catbox_url = upload_to_catbox(url)
            if catbox_url:
                run_cmd(["curl", "-L", "-o", dest, "--silent", "--show-error", catbox_url], timeout=180)
            else:
                raise Exception(f"Catbox bypass failed for {url}")
        else:
            raise e

def ffprobe_has_audio(path):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return bool(r.stdout.decode().strip())

# ---------- CLEANUP WORKER ----------
def cleanup_worker():
    while True:
        now = time.time()
        to_delete = []
        for task_id, info in list(TASKS.items()):
            if info.get("status") == "done" and info.get("completed_at"):
                if now - info["completed_at"] > RETENTION_TIME:
                    file_path = os.path.join(OUTPUT_DIR, info["file"])
                    if os.path.exists(file_path):
                        try: os.remove(file_path)
                        except: pass
                    to_delete.append(task_id)
            elif info.get("status") == "failed" and now - info["created"] > RETENTION_TIME:
                to_delete.append(task_id)
        for tid in to_delete:
            del TASKS[tid]
        time.sleep(30)

threading.Thread(target=cleanup_worker, daemon=True).start()

# ---------- WORKER ----------
def process_task(task_id, main_url, meme_url):
    main_path = f"{UPLOAD_DIR}/main_{task_id}.mp4"
    meme_path = f"{MEME_DIR}/meme_{task_id}.mp4"
    output = f"final_{task_id}.mp4"
    output_full_path = os.path.join(OUTPUT_DIR, output)

    try:
        TASKS[task_id]["status"] = "processing"

        download(main_url, main_path)
        download(meme_url, meme_path)

        has_audio_0 = ffprobe_has_audio(main_path)
        has_audio_1 = ffprobe_has_audio(meme_path)

        # LAYOUT: Meme at Y=0, Main Video shifted down by 250px
        filter_complex = (
            "[0:v]scale=1080:-1[vid];" 
            "[1:v]scale=1080:-1[meme];"
            "color=s=1080x1920:c=black[bg];"
            "[bg][meme]overlay=0:0[temp];"
            "[temp][vid]overlay=0:((1920-h)/2)+250[v]"
        )

        # Dynamic Audio Mixing
        audio_part = ""
        if has_audio_0 and has_audio_1:
            filter_complex += ";[0:a][1:a]amix=inputs=2:duration=first[a]"
            audio_part = "[a]"
        elif has_audio_0:
            audio_part = "0:a"
        elif has_audio_1:
            audio_part = "1:a"

        cmd = [FFMPEG, "-y", "-i", main_path, "-i", meme_path, "-filter_complex", filter_complex, "-map", "[v]"]
        if audio_part:
            cmd.extend(["-map", audio_part, "-c:a", "aac"])
        
        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", output_full_path])

        run_cmd(cmd)

        TASKS[task_id].update({"status": "done", "file": output, "completed_at": time.time()})

    except Exception as e:
        TASKS[task_id].update({"status": "failed", "error": str(e)})
    finally:
        for f in (main_path, meme_path):
            if os.path.exists(f): os.remove(f)

# ---------- API ----------
@app.post("/start")
def start(job: Job):
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "queued", "created": time.time()}
    threading.Thread(target=process_task, args=(task_id, job.main_url, job.meme_url), daemon=True).start()
    return {"status": "queued", "task_id": task_id}

@app.get("/status/{task_id}")
def status(task_id: str, request: Request):
    task = TASKS.get(task_id)
    if not task: return {"status": "not_found"}
    if task.get("status") == "done":
        base = str(request.base_url).rstrip("/")
        task["download_url"] = f"{base}/files/{task['file']}"
        elapsed = time.time() - task.get("completed_at", 0)
        task["seconds_until_deletion"] = max(0, int(RETENTION_TIME - elapsed))
    return task

@app.get("/")
def home():
    return {"service": "mix-insta-api", "status": "running", "bypass": "catbox-enabled"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
