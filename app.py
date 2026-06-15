import uuid
import os
import threading
import time
import subprocess
import json
import requests
import uvicorn
import shutil
import concurrent.futures

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI()

# ---------- PATHS ----------
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
RETENTION_TIME = 600  # How long to keep the task status in RAM after completion

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------- TASK STORE & LOCK ----------
TASKS = {}
JOB_LOCK = threading.Semaphore(4)

class Job(BaseModel):
    main_url: str
    voice_api: str
    script: Optional[str] = None
    avatars: List[str]
    audios: List[str]

# ---------- HELPERS ----------
def run_cmd(cmd, timeout=600):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=True
    )

def smart_download(url, dest):
    run_cmd(["curl", "-L", "-o", dest, "--silent", "--show-error", url])

def get_duration(path):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try: return float(r.stdout.strip())
    except: return 0

def get_dimensions(path):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        data = json.loads(r.stdout)
        return int(data["streams"][0]["width"]), int(data["streams"][0]["height"])
    except:
        return 1080, 1920

def extract_audio(video_path, out_audio):
    run_cmd([FFMPEG, "-y", "-i", video_path, "-vn", "-acodec", "mp3", out_audio])

def transcribe_audio(audio_path):
    with open(audio_path, "rb") as f:
        r = requests.post("https://transcription.itz-dev.workers.dev/", files={"file": f}, timeout=300)
    try: data = r.json()
    except: data = json.loads(r.text.split("~")[0])
    return data.get("text", "")

def generate_voice(voice_api, transcript, ref_audio_path):
    with open(ref_audio_path, "rb") as f:
        files = {"ref_audio": f}
        data = {
            "text": transcript, "language": "Hindi", "speed": "1.0",
            "guidance_scale": "2", "num_step": "32", "denoise": "true",
            "preprocess_prompt": "true", "postprocess_output": "true"
        }
        r = requests.post(voice_api, files=files, data=data, timeout=600)

    if r.status_code != 200:
        error_msg = r.text.strip()[:300]
        raise Exception(f"Voice API failed (Status {r.status_code}). Reason: {error_msg}")

    try: j = r.json()
    except json.JSONDecodeError:
        error_snippet = r.text.strip()[:200]
        raise Exception(f"Voice API returned invalid JSON. Server returned: {error_snippet}")

    if not j.get("success") and "audio_url" not in j:
        raise Exception(f"Voice generation failed: {j}")
    
    return j.get("audio_url")

def speed_audio(input_audio, output_audio, speed):
    run_cmd([FFMPEG, "-y", "-i", input_audio, "-filter:a", f"atempo={speed}", output_audio]) 

def upload_to_tmpfiles(filepath):
    """Uploads the final video to tmpfiles.org and returns the direct DL link."""
    url = "https://tmpfiles.org/api/v1/upload"
    try:
        with open(filepath, "rb") as f:
            # 86400 seconds = exactly 24 hours
            r = requests.post(url, files={"file": f}, data={"expire": 86400})
        if r.status_code == 200:
            original_url = r.json()["data"]["url"]
            # Convert standard link to direct download link
            return original_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except Exception as e:
        print(f"Tmpfiles upload error: {e}")
    return None

# ---------- VIDEO HELPERS ----------
def speed_video(input_video, output_video, speed):
    setpts = 1 / speed
    run_cmd([
        FFMPEG, "-y", "-i", input_video, "-filter:v", f"setpts={setpts}*PTS",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", output_video
    ])

def trim_video(input_path, start, duration, output_path):
    run_cmd([
        FFMPEG, "-y", "-ss", str(start), "-i", input_path, "-t", str(duration),
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", output_path
    ])

def concat_videos(video_list, output_path):
    txt_path = output_path + ".txt"
    with open(txt_path, "w") as f:
        for v in video_list: f.write(f"file '{os.path.abspath(v)}'\n")

    run_cmd([
        FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", txt_path,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", output_path
    ])
    try: os.remove(txt_path)
    except: pass

def pre_scale_avatar(avatar_path, target_w, target_h, output_path):
    run_cmd([
        FFMPEG, "-y", "-i", avatar_path, 
        "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h}",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-r", "30", "-pix_fmt", "yuv420p", output_path
    ])

def merge_avatar_and_audio(video_path, pre_scaled_avatar_path, audio_path, main_w, main_h, output_path):
    overlay_y = main_h // 2
    cmd = [
        FFMPEG, "-y", 
        "-i", video_path, "-stream_loop", "-1", "-i", pre_scaled_avatar_path, "-i", audio_path,
        "-filter_complex", f"[0:v]scale={main_w}:{main_h}[main];[main][1:v]overlay=0:{overlay_y}:shortest=1[outv]",
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", output_path
    ]
    run_cmd(cmd)

def process_duration_logic(sub_id, video_path, voice_audio):
    temp_dir = os.path.join(UPLOAD_DIR, sub_id)
    os.makedirs(temp_dir, exist_ok=True)

    video_duration = get_duration(video_path)
    audio_duration = get_duration(voice_audio)

    if audio_duration > video_duration:
        gap_ratio = audio_duration / video_duration
        vid_speed = 0.85 if gap_ratio >= 1.35 else 0.88 if gap_ratio >= 1.20 else 0.92 if gap_ratio >= 1.10 else 0.95

        slowed_video = os.path.join(temp_dir, "slowed.mp4")
        speed_video(video_path, slowed_video, vid_speed)
        
        slowed_duration = get_duration(slowed_video)
        remaining_gap = audio_duration / slowed_duration
        audio_speed = min(1.10, max(1.0, remaining_gap))

        sped_audio = os.path.join(temp_dir, "audio_sped.mp3")
        speed_audio(voice_audio, sped_audio, audio_speed)
        
        final_audio_duration = get_duration(sped_audio)

        if slowed_duration >= final_audio_duration:
            trimmed = os.path.join(temp_dir, "trimmed.mp4")
            trim_video(slowed_video, 0, final_audio_duration, trimmed)
            return trimmed, sped_audio

        missing = final_audio_duration - slowed_duration
        last5_start = max(0, slowed_duration - 5)

        before_last = os.path.join(temp_dir, "before_last.mp4")
        trim_video(slowed_video, 0, last5_start, before_last)

        filler = os.path.join(temp_dir, "filler.mp4")
        trim_video(slowed_video, 0, missing, filler)

        last5 = os.path.join(temp_dir, "last5.mp4")
        trim_video(slowed_video, last5_start, 5, last5)

        final_video = os.path.join(temp_dir, "video_ready.mp4")
        concat_videos([before_last, filler, last5], final_video)
        return final_video, sped_audio

    else:
        ratio = video_duration / audio_duration
        vid_speed = 1.10 if ratio >= 1.30 else 1.08 if ratio >= 1.20 else 1.05 if ratio >= 1.10 else 1.02

        sped_video = os.path.join(temp_dir, "sped_video.mp4")
        speed_video(video_path, sped_video, vid_speed)
        sped_duration = get_duration(sped_video)

        final_video = os.path.join(temp_dir, "trimmed_final.mp4")

        if sped_duration > audio_duration:
            trim_video(sped_video, 0, audio_duration, final_video)
        else:
            final_video = sped_video

        return final_video, voice_audio
       
# ---------- CLEANUP ----------
def cleanup_worker():
    while True:
        now = time.time()
        to_delete = []
        for task_id, info in list(TASKS.items()):
            if info.get("status") == "done" and info.get("completed_at"):
                if now - info["completed_at"] > RETENTION_TIME:
                    to_delete.append(task_id)
        for tid in to_delete: 
            del TASKS[tid]
        time.sleep(30)

threading.Thread(target=cleanup_worker, daemon=True).start()

# ---------- WORKER ----------
def process_task(task_id, main_url, voice_api, script, avatars, audios):
    start_time = time.time()  
    task_folder = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_folder, exist_ok=True)

    main_video = os.path.join(task_folder, "main.mp4")
    main_extracted_audio = os.path.join(task_folder, "main_extract.mp3")

    try:
        TASKS[task_id]["status"] = "waiting_in_queue"

        with JOB_LOCK:
            TASKS[task_id]["status"] = "processing"
            TASKS[task_id]["results"] = []

            # Step 1: Download main video once
            smart_download(main_url, main_video)

            # Step 2: Extract text (or use provided script)
            if script:
                transcript = script
            else:
                extract_audio(main_video, main_extracted_audio)
                transcript = transcribe_audio(main_extracted_audio)
            
            TASKS[task_id]["transcript"] = transcript

            width, height = get_dimensions(main_video)
            main_w = width if width % 2 == 0 else width - 1
            main_h = height if height % 2 == 0 else height - 1

            target_avatar_w = main_w
            target_avatar_h = main_h // 2
            target_avatar_w = target_avatar_w if target_avatar_w % 2 == 0 else target_avatar_w - 1
            target_avatar_h = target_avatar_h if target_avatar_h % 2 == 0 else target_avatar_h - 1

            # Step 3: Loop through each Avatar + Audio pair
            for i, (avatar_url, ref_audio_url) in enumerate(zip(avatars, audios)):
                sub_id = f"{task_id}_{i}"
                sub_folder = os.path.join(UPLOAD_DIR, sub_id)
                os.makedirs(sub_folder, exist_ok=True)

                raw_avatar = os.path.join(sub_folder, "raw_avatar.mp4")
                scaled_avatar = os.path.join(sub_folder, "scaled_avatar.mp4")
                ref_audio = os.path.join(sub_folder, "ref_audio.mp3")
                generated_audio = os.path.join(sub_folder, "generated.wav")
                final_output = os.path.join(OUTPUT_DIR, f"final_{sub_id}.mp4")

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    executor.submit(smart_download, avatar_url, raw_avatar)
                    executor.submit(smart_download, ref_audio_url, ref_audio)

                pre_scale_avatar(raw_avatar, target_avatar_w, target_avatar_h, scaled_avatar)
                
                try: os.remove(raw_avatar)
                except: pass

                voice_url = generate_voice(voice_api, transcript, ref_audio)
                smart_download(voice_url, generated_audio)

                final_video, final_audio = process_duration_logic(sub_id, main_video, generated_audio)

                merge_avatar_and_audio(final_video, scaled_avatar, final_audio, main_w, main_h, final_output)

                # Instantly upload to Tmpfiles.org and save link
                download_link = upload_to_tmpfiles(final_output)

                TASKS[task_id]["results"].append({
                    "pair_index": i + 1,
                    "download_url": download_link
                })

                # IMMEDIATELY delete local files for this pair to save space
                try: os.remove(final_output)
                except: pass
                try: shutil.rmtree(sub_folder)
                except: pass

            total_time = time.time() - start_time
            print(f"[{task_id}] Processed {len(avatars)} pairs in {total_time:.2f}s")

            TASKS[task_id].update({
                "status": "done",
                "completed_at": time.time(),
                "time_taken": round(total_time, 2)
            })

            # Clean up the main video folder
            try: shutil.rmtree(task_folder)
            except: pass

    except subprocess.CalledProcessError as e:
        error_msg = e.stdout if e.stdout else str(e)
        TASKS[task_id].update({"status": "failed", "error": error_msg[-1500:]})
    except Exception as e:
        TASKS[task_id].update({"status": "failed", "error": str(e)})

# ---------- API ----------
@app.post("/start")
def start(job: Job, request: Request):
    # Ensure they sent the same amount of avatars and audios
    if len(job.avatars) != len(job.audios):
        return {"status": "failed", "error": "The number of avatars and audios must match."}

    task_id = uuid.uuid4().hex[:21]
    TASKS[task_id] = {"status": "queued", "created": time.time()}

    threading.Thread(
        target=process_task, args=(task_id, job.main_url, job.voice_api, job.script, job.avatars, job.audios), daemon=True
    ).start()

    base_url = str(request.base_url).rstrip("/")
    return {
        "status": "queued",
        "task_id": task_id,
        "status_url": f"{base_url}/status/{task_id}"
    }

@app.get("/status/{task_id}")
def status(task_id: str):
    task = TASKS.get(task_id)
    if not task: return {"status": "not_found"}
    return task

@app.get("/")
def home():
    return {"service": "ai-video-api-batch", "status": "running"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
