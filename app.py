import uuid
import os
import threading
import time
import subprocess
import json
import requests
import uvicorn

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


app = FastAPI()

# ---------- PATHS ----------
FFMPEG = "./ffmpeg" if os.path.exists("./ffmpeg") else "ffmpeg"
FFPROBE = "./ffprobe" if os.path.exists("./ffprobe") else "ffprobe"

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
RETENTION_TIME = 600

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")

# ---------- TASK STORE & LOCK ----------
TASKS = {}
# Limits processing to 1 job at a time to strictly protect Render RAM
JOB_LOCK = threading.Semaphore(1)

# ---------- INPUT ----------
class Job(BaseModel):
    main_url: str
    ref_audio_url: str
    voice_api: str
    new_avatar_url: str


# ---------- HELPERS ----------
def run_cmd(cmd, timeout=600):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=True
    )


def smart_download(url, dest):
    run_cmd([
        "curl",
        "-L",
        "-o",
        dest,
        "--silent",
        "--show-error",
        url
    ])


def get_duration(path):
    r = subprocess.run(
        [
            FFPROBE,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    try:
        return float(r.stdout.strip())
    except:
        return 0


def get_dimensions(path):
    r = subprocess.run(
        [
            FFPROBE,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    try:
        data = json.loads(r.stdout)
        w = int(data["streams"][0]["width"])
        h = int(data["streams"][0]["height"])
        return w, h
    except:
        return 1080, 1920


def extract_audio(video_path, out_audio):
    run_cmd([
        FFMPEG,
        "-y",
        "-threads", "1",
        "-i", video_path,
        "-vn",
        "-acodec", "mp3",
        out_audio
    ])


def transcribe_audio(audio_path):
    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://transcription.itz-dev.workers.dev/",
            files={"file": f},
            timeout=300
        )
    try:
        data = r.json()
    except:
        data = json.loads(r.text.split("~")[0])
    return data.get("text", "")


def generate_voice(voice_api, transcript, ref_audio_path):
    with open(ref_audio_path, "rb") as f:
        files = {"ref_audio": f}
        data = {
            "text": transcript,
            "language": "Hindi",
            "speed": "1.0",
            "guidance_scale": "2",
            "num_step": "32",
            "denoise": "true",
            "preprocess_prompt": "true",
            "postprocess_output": "true"
        }
        r = requests.post(voice_api, files=files, data=data, timeout=600)

    j = r.json()
    if not j.get("success"):
        raise Exception("Voice generation failed")
    return j["audio_url"]


def speed_audio(input_audio, output_audio, speed):
    run_cmd([
        FFMPEG,
        "-y",
        "-threads", "1",
        "-i", input_audio,
        "-filter:a", f"atempo={speed}",
        output_audio
    ]) 


# ---------- VIDEO HELPERS ----------
def speed_video(input_video, output_video, speed):
    setpts = 1 / speed
    run_cmd([
        FFMPEG,
        "-y",
        "-threads", "1",
        "-i", input_video,
        "-filter:v", f"setpts={setpts}*PTS",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "32",
        "-pix_fmt", "yuv420p",
        output_video
    ])


def trim_video(input_path, start, duration, output_path):
    run_cmd([
        FFMPEG,
        "-y",
        "-threads", "1",
        "-ss", str(start),
        "-i", input_path,
        "-t", str(duration),
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "32",
        "-pix_fmt", "yuv420p",
        output_path
    ])


def concat_videos(video_list, output_path):
    txt_path = output_path + ".txt"
    with open(txt_path, "w") as f:
        for v in video_list:
            f.write(f"file '{os.path.abspath(v)}'\n")

    run_cmd([
        FFMPEG,
        "-y",
        "-threads", "1",
        "-f", "concat",
        "-safe", "0",
        "-i", txt_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "32",
        "-pix_fmt", "yuv420p",
        output_path
    ])
    try:
        os.remove(txt_path)
    except:
        pass


# ---------- NEW LOW-RAM PRE-PROCESSING HELPERS ----------
def pre_scale_avatar(avatar_path, target_w, target_h, output_path):
    """Shrinks avatar immediately after downloading to prevent decoding spikes later"""
    run_cmd([
        FFMPEG,
        "-y",
        "-threads", "1",
        "-i", avatar_path,
        "-vf", f"scale={target_w}:{target_h}",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "32",
        "-r", "30",
        "-pix_fmt", "yuv420p",
        output_path
    ])


def merge_avatar_and_audio(video_path, pre_scaled_avatar_path, audio_path, target_width, target_height, final_height, output_path):
    main_w = target_width
    main_h = final_height
    overlay_y = main_h - target_height

    cmd = [
        FFMPEG,
        "-y",
        "-threads", "1",            # Force single-threaded scaling
        "-syncpoints", "1",         # Low-memory synchronization flag
        "-i", video_path,
        "-stream_loop", "-1",       
        "-i", pre_scaled_avatar_path,
        "-i", audio_path,
        "-filter_complex", 
        f"[0:v]scale={main_w}:{main_h}[main];[main][1:v]overlay=0:{overlay_y}[outv]",
        "-map", "[outv]",
        "-map", "2:a",
        "-c:v", "libx264",
        "-preset", "ultrafast",     
        "-crf", "30",               # Slightly higher compression to save server RAM allocation
        "-rc-lookahead", "0",       # Strips frame caching arrays from memory
        "-r", "30",                 
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",                
        output_path
    ]
    run_cmd(cmd)


def process_duration_logic(task_id, video_path, voice_audio):
    temp_dir = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(temp_dir, exist_ok=True)

    video_duration = get_duration(video_path)
    audio_duration = get_duration(voice_audio)

    # ---------- CASE 1: Audio longer ----------
    if audio_duration > video_duration:
        gap_ratio = audio_duration / video_duration

        if gap_ratio >= 1.35:
            vid_speed = 0.85
        elif gap_ratio >= 1.20:
            vid_speed = 0.88
        elif gap_ratio >= 1.10:
            vid_speed = 0.92
        else:
            vid_speed = 0.95

        slowed_video = os.path.join(temp_dir, "slowed.mp4")
        speed_video(video_path, slowed_video, vid_speed)
        slowed_duration = get_duration(slowed_video)

        remaining_gap = audio_duration / slowed_duration
        audio_speed = min(1.10, max(1.0, remaining_gap))

        sped_audio = os.path.join(temp_dir, "audio_sped.mp3")
        speed_audio(voice_audio, sped_audio, audio_speed)
        
        final_audio_duration = get_duration(sped_audio)
        slowed_duration = get_duration(slowed_video)

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

    # ---------- CASE 2: Video longer ----------
    else:
        ratio = video_duration / audio_duration

        if ratio >= 1.30:
            vid_speed = 1.10
        elif ratio >= 1.20:
            vid_speed = 1.08
        elif ratio >= 1.10:
            vid_speed = 1.05
        else:
            vid_speed = 1.02

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
                elapsed = now - info["completed_at"]

                if elapsed > RETENTION_TIME:
                    file_path = os.path.join(OUTPUT_DIR, info["file"])
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    except:
                        pass

                    task_folder = os.path.join(UPLOAD_DIR, task_id)
                    try:
                        if os.path.exists(task_folder):
                            for f in os.listdir(task_folder):
                                try:
                                    os.remove(os.path.join(task_folder, f))
                                except:
                                    pass
                            os.rmdir(task_folder)
                    except:
                        pass

                    to_delete.append(task_id)

        for tid in to_delete:
            del TASKS[tid]

        time.sleep(30)


threading.Thread(target=cleanup_worker, daemon=True).start()


# ---------- WORKER ----------
def process_task(task_id, main_url, ref_audio_url, voice_api, new_avatar_url):
    task_folder = os.path.join(UPLOAD_DIR, task_id)
    os.makedirs(task_folder, exist_ok=True)

    main_video = os.path.join(task_folder, "main.mp4")
    ref_audio = os.path.join(task_folder, "ref.mp3")
    raw_avatar = os.path.join(task_folder, "raw_avatar.mp4")
    scaled_avatar = os.path.join(task_folder, "scaled_avatar.mp4")
    extracted_audio = os.path.join(task_folder, "extract.mp3")
    generated_audio = os.path.join(task_folder, "generated.wav")
    output_name = f"final_{task_id}.mp4"
    final_output = os.path.join(OUTPUT_DIR, output_name)

    try:
        TASKS[task_id]["status"] = "waiting_in_queue"

        with JOB_LOCK:
            TASKS[task_id]["status"] = "processing"

            # Download assets
            smart_download(main_url, main_video)
            smart_download(ref_audio_url, ref_audio)
            smart_download(new_avatar_url, raw_avatar)

            # --- DYNAMIC LOW-RAM DIMENSION MATH (Target 540p Vertical) ---
            width, height = get_dimensions(main_video)
            if height > 960:
                ratio = 960 / height
                width = int(width * ratio)
                height = 960

            target_width = (width // 2) * 2
            target_height = (height // 4) * 2
            final_height = (height // 2) * 2

            # Step 1: Pre-scale avatar to tiny dimension sequentially
            pre_scale_avatar(raw_avatar, target_width, target_height, scaled_avatar)
            
            # Remove high resolution raw avatar from folder to keep disk footprint small
            try:
                os.remove(raw_avatar)
            except:
                pass

            # Step 2: Extract & Transcribe
            extract_audio(main_video, extracted_audio)
            transcript = transcribe_audio(extracted_audio)
            TASKS[task_id]["transcript"] = transcript

            # Step 3: Generate Voice
            voice_url = generate_voice(voice_api, transcript, ref_audio)
            smart_download(voice_url, generated_audio)

            # Step 4: Video timing / padding adjustment
            final_video, final_audio = process_duration_logic(
                task_id,
                main_video,
                generated_audio
            )

            # Step 5: Merge pre-scaled avatar and final layout sequence
            merge_avatar_and_audio(
                final_video,
                scaled_avatar,
                final_audio,
                target_width,
                target_height,
                final_height,
                final_output
            )

            TASKS[task_id].update({
                "status": "done",
                "file": output_name,
                "completed_at": time.time()
            })

    except subprocess.CalledProcessError as e:
        error_msg = e.stdout if e.stdout else str(e)
        TASKS[task_id].update({
            "status": "failed",
            "error": error_msg[-1500:]
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
        args=(
            task_id,
            job.main_url,
            job.ref_audio_url,
            job.voice_api,
            job.new_avatar_url
        ),
        daemon=True
    ).start()

    return {
        "status": "queued",
        "task_id": task_id
    }


@app.get("/status/{task_id}")
def status(task_id: str, request: Request):
    task = TASKS.get(task_id)
    if not task:
        return {"status": "not_found"}

    if task.get("status") == "done":
        base = str(request.base_url).rstrip("/")
        task["download_url"] = f"{base}/files/{task['file']}"
        elapsed = time.time() - task.get("completed_at", 0)
        task["seconds_until_deletion"] = max(0, int(RETENTION_TIME - elapsed))

    return task


@app.get("/")
def home():
    return {
        "service": "ai-video-api",
        "status": "running"
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
