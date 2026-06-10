import uuid, os, threading, time, subprocess, json, requests, uvicorn, concurrent.futures
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# ---------- PATHS ----------
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
RETENTION_TIME = 600

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")

TASKS = {}
JOB_LOCK = threading.Semaphore(4)

class Job(BaseModel):
    main_url: str
    ref_audio_url: str
    voice_api: str
    new_avatar_url: str

def run_cmd(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)

def smart_download(url, dest):
    run_cmd(["curl", "-L", "-o", dest, "--silent", url])

def get_dimensions(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", path], stdout=subprocess.PIPE, text=True)
    data = json.loads(r.stdout)
    return int(data["streams"][0]["width"]), int(data["streams"][0]["height"])

def merge_avatar_and_audio(video_path, avatar_path, audio_path, main_w, main_h, output_path):
    # Fix: Overlay loop set to entire duration
    overlay_y = main_h - (main_h // 4)
    cmd = [
        FFMPEG, "-y", "-i", video_path, "-stream_loop", "-1", "-i", avatar_path, "-i", audio_path,
        "-filter_complex", f"[0:v]scale={main_w}:{main_h}[main];[main][1:v]overlay=0:{overlay_y}:shortest=0[outv]",
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_path
    ]
    run_cmd(cmd)

def process_task(task_id, main_url, ref_audio_url, voice_api, new_avatar_url):
    start_time = time.time()
    task_folder = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_folder, exist_ok=True)
    
    # Paths
    main_video = os.path.join(task_folder, "main.mp4")
    raw_avatar = os.path.join(task_folder, "avatar.mp4")
    final_output = os.path.join(OUTPUT_DIR, f"final_{task_id}.mp4")

    try:
        TASKS[task_id]["status"] = "processing"
        
        # Parallel download
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(smart_download, main_url, main_video)
            executor.submit(smart_download, new_avatar_url, raw_avatar)
            
        w, h = get_dimensions(main_video)
        merge_avatar_and_audio(main_video, raw_avatar, ref_audio_url, w, h, final_output)
        
        total_time = time.time() - start_time
        print(f"TASK {task_id} COMPLETED IN {total_time:.2f}s")
        
        TASKS[task_id].update({"status": "done", "file": f"final_{task_id}.mp4", "completed_at": time.time()})
    except Exception as e:
        TASKS[task_id].update({"status": "failed", "error": str(e)})

@app.post("/start")
def start(job: Job):
    task_id = uuid.uuid4().hex[:21]
    TASKS[task_id] = {"status": "queued"}
    threading.Thread(target=process_task, args=(task_id, job.main_url, job.ref_audio_url, job.voice_api, job.new_avatar_url), daemon=True).start()
    return {"task_id": task_id}

@app.get("/status/{task_id}")
def status(task_id: str, request: Request):
    return TASKS.get(task_id, {"status": "not_found"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
