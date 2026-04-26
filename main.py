import uuid
import os
import threading
import time
import subprocess
import uvicorn
import urllib.request
import urllib.parse
import urllib.error
import json
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# ---------- TELEGRAM CREDENTIALS ----------
# For production, it is highly recommended to store these as Environment Variables on Render
BOT_TOKEN = "7937328325:AAEOJ3XiyvwZNCVReclkEfpj80BmJoMyngw"
CHAT_ID = "6931296977"

# ---------- PATHS ----------
FFMPEG = "./ffmpeg" if os.path.exists("./ffmpeg") else "ffmpeg"
FFPROBE = "./ffprobe" if os.path.exists("./ffprobe") else "ffprobe"

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

def download_via_telegram(url, dest):
    """Fallback: Asks Telegram to fetch the URL, bypass the block, and serve the file."""
    # 1. Ask Telegram to fetch the video and send it to your chat
    send_api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'video': url}).encode('utf-8')
    req = urllib.request.Request(send_api, data=data)
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_info = e.read().decode()
        raise Exception(f"Telegram API blocked the fetch (File > 20MB or Dead Link): {error_info}")
    
    message_id = res_data['result']['message_id']
    
    # Check if Telegram saw it as a video or a generic document
    if 'video' in res_data['result']:
        file_id = res_data['result']['video']['file_id']
    elif 'document' in res_data['result']:
        file_id = res_data['result']['document']['file_id']
    else:
        raise Exception("Telegram downloaded the URL but could not process it as a video.")

    # 2. Ask Telegram for the direct download path
    get_file_api = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    try:
        with urllib.request.urlopen(get_file_api) as response:
            file_data = json.loads(response.read().decode())
            file_path = file_data['result']['file_path']
    except Exception as e:
        raise Exception(f"Failed to get file path from Telegram: {str(e)}")

    # 3. Download the actual file from Telegram's high-speed servers
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    run_cmd(["curl", "-f", "-L", "-o", dest, "--silent", "--show-error", download_url], timeout=180)

    # 4. Clean up your chat instantly so it doesn't get spammed
    delete_api = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    del_data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'message_id': message_id}).encode('utf-8')
    try:
        urllib.request.urlopen(urllib.request.Request(delete_api, data=del_data))
    except:
        pass

def is_valid_video(path):
    """Checks if the downloaded file is actually a playable video file."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return bool(r.stdout.strip())
    except:
        return False

def smart_download(url, dest, label="URL"):
    """Downloads file and uses the Telegram Bulletproof Bypass if blocked."""
    # 1. Try initial direct download (fastest route if no block)
    try:
        run_cmd(["curl", "-f", "-L", "-o", dest, "--silent", "--show-error", url], timeout=180)
    except subprocess.CalledProcessError:
        pass 
    
    # 2. Check if we got a valid video
    if os.path.exists(dest) and is_valid_video(dest):
        return 
        
    # 3. If blocked or invalid, trigger Telegram Bypass
    try:
        if os.path.exists(dest):
            os.remove(dest) # Remove fake blocked file
        
        download_via_telegram(url, dest)
        
        if os.path.exists(dest) and is_valid_video(dest):
            return 
    except Exception as bypass_error:
        raise Exception(f"{label} Telegram bypass failed -> {str(bypass_error)}")
            
    # 4. If everything fails
    raise Exception(f"{label} error: The link is dead or the file is too large for Telegram to fetch.")

def ffprobe_has_audio(path):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return bool(r.stdout.strip())
    except:
        return False

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

        smart_download(main_url, main_path, "MAIN_URL")
        smart_download(meme_url, meme_path, "MEME_URL")

        has_audio_0 = ffprobe_has_audio(main_path)
        has_audio_1 = ffprobe_has_audio(meme_path)

        filter_complex = (
            "[0:v]scale=1080:-2[vid];" 
            "[1:v]scale=1080:-2[meme];"
            "color=s=1080x1920:c=black[bg];"
            "[bg][meme]overlay=x=0:y=0[temp];"
            "[temp][vid]overlay=x=0:y=(1920-h)/2+250[v]"
        )

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

    except subprocess.CalledProcessError as e:
        error_msg = e.stdout if e.stdout else str(e)
        if len(error_msg) > 1000:
            error_msg = "..." + error_msg[-1000:]
        TASKS[task_id].update({"status": "failed", "error": f"FFmpeg Error: {error_msg}"})
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
    return {"service": "mix-insta-api", "status": "running", "bypass": "telegram-enabled"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
