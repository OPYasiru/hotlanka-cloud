import os
import re
import time
import json
import base64
import asyncio
import threading
import subprocess
from datetime import datetime, timedelta, timezone
from PIL import Image
from flask import Flask, render_template_string, request, jsonify, send_from_directory
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
import cv2
import numpy as np

# ================= CONFIGURATION =================
API_ID = 30345187
API_HASH = "b1d1c146f1ec4d51fbae1ca15acd51e7"

BASE_DIR = os.path.expanduser("~")
DOWNLOADS_FOLDER = os.path.join(BASE_DIR, "Downloads")
VIDEOS_FOLDER = os.path.join(BASE_DIR, "Edited")
PREVIEWS_FOLDER = os.path.join(VIDEOS_FOLDER, "preview")
COMPLETED_FOLDER = os.path.join(VIDEOS_FOLDER, "completed")
TEMP_FOLDER = os.path.join(VIDEOS_FOLDER, "temp")
SESSION_PATH = os.path.join(BASE_DIR, "telethon_cloud_session")

FILE_STORE_BOT = "LankaFile_bot"
POST_BOT = "HotLanka_Bot"
DEFAULT_CAPTION = "🔥Hot Lanka Exclusive 🔥"
INTRO_SKIP_SECONDS = 8.5

# Sri Lanka Timezone (UTC+5:30)
SL_TZ = timezone(timedelta(hours=5, minutes=30))
# =================================================

for d in [DOWNLOADS_FOLDER, VIDEOS_FOLDER, PREVIEWS_FOLDER, COMPLETED_FOLDER, TEMP_FOLDER]:
    os.makedirs(d, exist_ok=True)

app_flask = Flask(__name__)

# Global States
editor_state = {"active": False, "file": "", "step": "Idle"}
batch_state = {
    "running": False,
    "done": False,
    "msg": "Ready",
    "pct": 0,
    "speed": "",
    "current_file": None,
    "completed_files": [],
    "logs": []
}
stop_flag = False
batch_lock = threading.Lock()
processed_downloads = set()

def add_log(text):
    now_str = datetime.now(SL_TZ).strftime("%H:%M:%S")
    batch_state["logs"].append(f"[{now_str}] {text}")
    if len(batch_state["logs"]) > 40:
        batch_state["logs"].pop(0)

def clean_temp_folder():
    for f in os.listdir(TEMP_FOLDER):
        fp = os.path.join(TEMP_FOLDER, f)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
            except Exception:
                pass

# ================= AUTO VIDEO EDITOR (LOGIC FROM editor.py) =================
def detect_black_bars_and_crop(video_path, check_sec=9):
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * check_sec))
        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            return None

        h, w, _ = frame.shape
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        col_means = np.mean(gray, axis=0)

        left_bar = next((i for i in range(w // 2) if col_means[i] > 18), 0)
        right_bar = next((i + 1 for i in range(w - 1, w // 2, -1) if col_means[i] > 18), w)
        crop_w = right_bar - left_bar

        if left_bar > (w * 0.08) and (w - right_bar) > (w * 0.08):
            crop_w -= (crop_w % 2)
            left_bar -= (left_bar % 2)
            h -= (h % 2)
            return f"crop={crop_w}:{h}:{left_bar}:0"
    except Exception:
        pass
    return None

def process_downloaded_video(raw_path):
    filename = os.path.basename(raw_path)
    out_path = os.path.join(VIDEOS_FOLDER, filename)
    temp_out = os.path.join(TEMP_FOLDER, f"edit_{filename}")
    proxy_out = os.path.join(PREVIEWS_FOLDER, f"{filename}.mp4")

    editor_state["active"] = True
    editor_state["file"] = filename
    editor_state["step"] = "Trimming 8.5s & Cropping..."
    add_log(f"Auto-editing started: {filename}")

    try:
        crop_filter = detect_black_bars_and_crop(raw_path, check_sec=9)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(INTRO_SKIP_SECONDS), "-i", raw_path]
        if crop_filter:
            cmd.extend(["-vf", crop_filter])
        cmd.extend(["-c:v", "libx264", "-crf", "19", "-preset", "veryfast", "-movflags", "+faststart", "-c:a", "copy", temp_out])
        subprocess.run(cmd, check=True)

        if os.path.exists(temp_out) and os.path.getsize(temp_out) > 10240:
            if os.path.exists(out_path):
                os.remove(out_path)
            os.rename(temp_out, out_path)

            # Generate lightweight 360p fast-seek proxy for instant zero-lag preview
            editor_state["step"] = "Generating fast preview..."
            proxy_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", out_path, "-vf", "scale=-2:360",
                "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
                "-g", "15", "-an", proxy_out
            ]
            subprocess.run(proxy_cmd, check=True)

            # Remove raw download to save cloud disk space
            if os.path.exists(raw_path):
                os.remove(raw_path)
            add_log(f"Ready for upload: {filename}")
    except Exception as e:
        add_log(f"Edit error ({filename}): {e}")
        if os.path.exists(temp_out):
            os.remove(temp_out)
    finally:
        editor_state["active"] = False
        editor_state["file"] = ""
        editor_state["step"] = "Idle"

def background_download_watcher():
    valid_exts = ('.mp4', '.mkv', '.mov', '.webm', '.avi')
    while True:
        try:
            for f in sorted(os.listdir(DOWNLOADS_FOLDER)):
                if f.endswith(('.crdownload', '.part', '.tmp')) or f in processed_downloads:
                    continue
                full_p = os.path.join(DOWNLOADS_FOLDER, f)
                if os.path.isfile(full_p) and f.lower().endswith(valid_exts):
                    s1 = os.path.getsize(full_p)
                    time.sleep(2.0)
                    if os.path.exists(full_p) and os.path.getsize(full_p) == s1 and s1 > 50000:
                        processed_downloads.add(f)
                        process_downloaded_video(full_p)
        except Exception:
            pass
        time.sleep(2.0)

# ================= TELETHON BATCH UPLOADER (LOGIC FROM uploader.py) =================
def extract_video_metadata_and_thumb(video_path, task_idx):
    duration, width, height = 0, 0, 0
    video_thumb_path = os.path.join(TEMP_FOLDER, f"auto_thumb_{task_idx}_{int(time.time())}.jpg")
    try:
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            duration = int(total_frames / fps) if fps > 0 else 0
            cap.set(cv2.CAP_PROP_POS_MSEC, 3000)
            ret, frame = cap.read()
            if ret and frame is not None:
                cv2.imwrite(video_thumb_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            cap.release()
    except Exception:
        pass
    thumb_final = video_thumb_path if (os.path.exists(video_thumb_path) and os.path.getsize(video_thumb_path) > 0) else None
    return duration, width, height, thumb_final

async def get_fresh_link_telethon(client, last_bot_msg_id, wait_seconds=35):
    for _ in range(wait_seconds):
        if stop_flag:
            return None
        await asyncio.sleep(1.0)
        try:
            messages = await client.get_messages(FILE_STORE_BOT, limit=3)
            for msg in messages:
                if not msg.out and msg.id > last_bot_msg_id:
                    match = re.search(r'https?://t\.me/[^\s]+', msg.raw_text or "")
                    if match:
                        return match.group(0).strip()
        except Exception:
            pass
    return None

def calculate_schedule_datetime(date_str, time_str):
    if not time_str or not time_str.strip():
        return None
    try:
        hours, minutes = map(int, time_str.strip().split(":"))
        now_sl = datetime.now(SL_TZ)
        if date_str and date_str.strip():
            y, m, d = map(int, date_str.strip().split("-"))
            target_sl = datetime(y, m, d, hours, minutes, 0, tzinfo=SL_TZ)
        else:
            target_sl = now_sl.replace(hour=hours, minute=minutes, second=0, microsecond=0)
            if target_sl <= now_sl:
                target_sl += timedelta(days=1)
        return target_sl.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None

def run_uploader_thread(tasks, schedule_date_str, schedule_time_str):
    global stop_flag
    if not batch_lock.acquire(blocking=False):
        return

    try:
        stop_flag = False
        batch_state["running"] = True
        batch_state["done"] = False
        batch_state["completed_files"] = []

        async def worker():
            global stop_flag
            batch_state["msg"] = "Connecting to Telegram..."
            client = TelegramClient(SESSION_PATH, API_ID, API_HASH, connection_retries=5)
            await client.connect()

            if not await client.is_user_authorized():
                batch_state["msg"] = "Error: Telegram not logged in! Run login in terminal."
                batch_state["done"] = True
                batch_state["running"] = False
                add_log("Error: Telegram session not authorized.")
                return

            total = len(tasks)
            base_schedule_dt = calculate_schedule_datetime(schedule_date_str, schedule_time_str)
            add_log(f"Batch started: {total} videos.")

            for idx, item in enumerate(tasks, start=1):
                if stop_flag:
                    batch_state["msg"] = "Stopped by user"
                    add_log("Batch stopped by user.")
                    break

                f_name = item['fileName']
                caption = item.get('caption', '').strip() or DEFAULT_CAPTION
                image_b64 = item.get('imageBase64', '')
                file_path = os.path.join(VIDEOS_FOLDER, f_name)

                if not os.path.exists(file_path):
                    continue

                batch_state["current_file"] = f_name
                batch_state["msg"] = f"[{idx}/{total}] Step 1/4: Preparing..."
                batch_state["pct"] = 5
                batch_state["speed"] = ""

                post_photo_custom = None
                if image_b64 and "," in image_b64:
                    try:
                        post_photo_custom = os.path.join(TEMP_FOLDER, f"thumb_{idx}_{int(time.time())}.jpg")
                        img_bytes = base64.b64decode(image_b64.split(",")[1])
                        with open(post_photo_custom, "wb") as f_img:
                            f_img.write(img_bytes)
                        with Image.open(post_photo_custom) as im:
                            im.convert('RGB').save(post_photo_custom, quality=95)
                    except Exception:
                        post_photo_custom = None

                duration, width, height, auto_thumb = extract_video_metadata_and_thumb(file_path, idx)

                last_bot_msg_id = 0
                try:
                    recent = await client.get_messages(FILE_STORE_BOT, limit=3)
                    for m in recent:
                        if not m.out:
                            last_bot_msg_id = m.id
                            break
                except Exception:
                    pass

                start_t = time.time()
                def progress_callback(cur, tot):
                    if stop_flag:
                        return
                    elapsed = max(time.time() - start_t, 0.1)
                    speed_mb = (cur / (1024 * 1024)) / elapsed
                    pct = int((cur / tot) * 80)
                    batch_state["pct"] = pct
                    batch_state["speed"] = f"{speed_mb:.1f} MB/s"
                    batch_state["msg"] = f"[{idx}/{total}] Step 2/4: Uploading... ({speed_mb:.1f} MB/s)"

                uploaded_file = await client.upload_file(
                    file_path,
                    part_size_kb=512,
                    progress_callback=progress_callback
                )

                if stop_flag:
                    break

                await client.send_file(
                    entity=FILE_STORE_BOT,
                    file=uploaded_file,
                    caption="HotLanka",
                    thumb=auto_thumb,
                    attributes=[DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]
                )

                batch_state["msg"] = f"[{idx}/{total}] Step 3/4: Fetching Link..."
                batch_state["pct"] = 88
                batch_state["speed"] = ""
                extracted_link = await get_fresh_link_telethon(client, last_bot_msg_id, wait_seconds=35)

                if extracted_link and not stop_flag:
                    current_item_schedule = None
                    if base_schedule_dt:
                        current_item_schedule = base_schedule_dt + timedelta(seconds=(idx - 1) * 2)

                    batch_state["msg"] = f"[{idx}/{total}] Step 4/4: Scheduling Post..."
                    batch_state["pct"] = 96
                    post_payload = f"{caption}\n\n{extracted_link}"

                    if post_photo_custom and os.path.exists(post_photo_custom):
                        await client.send_file(POST_BOT, file=post_photo_custom, caption=post_payload, schedule=current_item_schedule)
                    else:
                        await client.send_message(POST_BOT, message=post_payload, schedule=current_item_schedule)

                    # Delete processed video & proxy to keep cloud storage clean (under 15GB limit)
                    for p in [file_path, os.path.join(PREVIEWS_FOLDER, f"{f_name}.mp4"), post_photo_custom, auto_thumb]:
                        if p and os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass

                    batch_state["completed_files"].append(f_name)
                    batch_state["pct"] = 100
                    add_log(f"[{idx}/{total}] Done: {f_name} -> Scheduled!")
                else:
                    add_log(f"[{idx}/{total}] Failed to get link for {f_name}")

                await asyncio.sleep(1.0)

            await client.disconnect()
            clean_temp_folder()
            batch_state["current_file"] = None
            batch_state["running"] = False
            batch_state["done"] = True
            if not stop_flag:
                batch_state["msg"] = "All Tasks Completed!"
                add_log("Batch finished successfully.")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker())
        loop.close()
    finally:
        batch_lock.release()

# ================= FLASK ROUTES =================
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

@app_flask.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app_flask.route('/preview/<path:filename>')
def serve_preview(filename):
    proxy_name = f"{filename}.mp4"
    if os.path.exists(os.path.join(PREVIEWS_FOLDER, proxy_name)):
        return send_from_directory(PREVIEWS_FOLDER, proxy_name)
    return send_from_directory(VIDEOS_FOLDER, filename)

@app_flask.route('/api/capture_frame', methods=['POST'])
def capture_frame():
    data = request.json or {}
    filename = data.get("filename", "")
    timestamp = float(data.get("timestamp", 0.0))
    video_path = os.path.join(VIDEOS_FOLDER, filename)

    if not os.path.exists(video_path):
        return jsonify({"error": "Video not found"}), 404

    try:
        cap = cv2.VideoCapture(video_path)
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            b64_str = base64.b64encode(buffer).decode('utf-8')
            return jsonify({"imageBase64": f"data:image/jpeg;base64,{b64_str}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Frame capture failed"}), 500

@app_flask.route('/api/get_videos')
def get_videos():
    supported_exts = ('.mp4', '.mkv', '.mov', '.avi', '.webm')
    v_list = []
    if os.path.exists(VIDEOS_FOLDER):
        raw_files = [
            f for f in os.listdir(VIDEOS_FOLDER)
            if f.lower().endswith(supported_exts) and os.path.isfile(os.path.join(VIDEOS_FOLDER, f))
        ]
        for f in sorted(raw_files, key=natural_sort_key):
            full_p = os.path.join(VIDEOS_FOLDER, f)
            size_mb = f"{os.path.getsize(full_p) / (1024 * 1024):.1f} MB"
            v_list.append({'fileName': f, 'size': size_mb})
    return jsonify({"videos": v_list, "editor": editor_state})

@app_flask.route('/api/start_batch', methods=['POST'])
def start_batch():
    payload = request.json or {}
    tasks = payload.get("tasks", [])
    schedule_date = payload.get("schedule_date", "")
    schedule_time = payload.get("schedule_time", "")

    if not tasks:
        return jsonify({"status": "no_tasks"}), 400

    threading.Thread(
        target=run_uploader_thread,
        args=(tasks, schedule_date, schedule_time),
        daemon=True
    ).start()
    return jsonify({"status": "started"})

@app_flask.route('/api/stop_batch', methods=['POST'])
def stop_batch():
    global stop_flag
    stop_flag = True
    batch_state["msg"] = "Stopping..."
    return jsonify({"status": "stopping"})

@app_flask.route('/api/get_status')
def get_status():
    return jsonify(batch_state)

# ================= WEB DASHBOARD UI (SIMPLE ENGLISH + INSTANT FRAME GRABBER) =================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>HotLanka Cloud Dashboard</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            background: #090712;
            color: #f1f5f9;
            padding: 18px;
        }
        .container {
            max-width: 960px;
            margin: 0 auto;
            background: #0f0b1e;
            border: 1px solid #24164a;
            border-radius: 14px;
            padding: 18px;
        }
        .top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 12px;
            border-bottom: 1px solid #1f143d;
            margin-bottom: 14px;
        }
        .title { font-size: 17px; font-weight: 700; color: #f8fafc; }
        .badge {
            font-size: 12px;
            color: #c084fc;
            background: rgba(147, 51, 234, 0.15);
            border: 1px solid rgba(147, 51, 234, 0.35);
            padding: 4px 12px;
            border-radius: 20px;
        }
        .editor-banner {
            background: #17102e;
            border: 1px solid #3b2466;
            color: #38bdf8;
            font-size: 12px;
            padding: 8px 12px;
            border-radius: 8px;
            margin-bottom: 12px;
            display: none;
        }
        .controls {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 14px;
            align-items: center;
        }
        input[type="text"], input[type="date"], input[type="time"] {
            background: #080612;
            border: 1px solid #2b1c54;
            border-radius: 8px;
            padding: 8px 11px;
            color: #fff;
            font-size: 12.5px;
            outline: none;
        }
        input:focus { border-color: #8b5cf6; }
        .bulk-cap { flex: 1; min-width: 200px; }
        .btn {
            background: #1d133b;
            border: 1px solid #3c2773;
            color: #e9d5ff;
            padding: 8px 13px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn:hover { background: #2c1d59; }
        .btn-primary {
            background: linear-gradient(135deg, #8b5cf6, #7c3aed);
            border: none;
            color: #fff;
            padding: 10px 20px;
            font-size: 13px;
        }
        .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
        .btn-danger { background: #ef4444; border: none; color: #fff; display: none; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(430px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }
        .card {
            background: #130d29;
            border: 1px solid #26184f;
            border-radius: 10px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 9px;
        }
        .card.done { border-color: #22c55e; opacity: 0.6; }
        .card-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 12.5px;
        }
        .file-title {
            font-weight: 600;
            color: #e2e8f0;
            max-width: 310px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .player-row {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        video {
            width: 210px;
            height: 118px;
            background: #000;
            border-radius: 7px;
            border: 1px solid #2e1d5e;
        }
        .thumb-side {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 6px;
            align-items: center;
        }
        .thumb-box {
            width: 100%;
            height: 82px;
            border: 1px dashed #4c2f8a;
            border-radius: 7px;
            background: #080612;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            overflow: hidden;
            position: relative;
        }
        .thumb-box:focus { border-color: #a855f7; }
        .thumb-box img { width: 100%; height: 100%; object-fit: cover; display: none; }
        .thumb-hint { font-size: 11px; color: #8b76b8; text-align: center; padding: 4px; }
        .btn-cap {
            width: 100%;
            background: #231647;
            border: 1px solid #472d8c;
            color: #d8b4fe;
            padding: 5px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-cap:hover { background: #342169; }
        .progress-section {
            background: #080612;
            border: 1px solid #1f143d;
            border-radius: 10px;
            padding: 12px;
        }
        .prog-top {
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: #c4b5fd;
            margin-bottom: 6px;
        }
        .bar-bg {
            width: 100%;
            height: 7px;
            background: #1a1136;
            border-radius: 10px;
            overflow: hidden;
            margin-bottom: 10px;
        }
        .bar-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, #8b5cf6, #22c55e);
            transition: width 0.25s;
        }
        .log-box {
            font-family: monospace;
            font-size: 11px;
            color: #94a3b8;
            max-height: 95px;
            overflow-y: auto;
            line-height: 1.5;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <span class="title">HotLanka Cloud Studio</span>
            <span class="badge" id="vid_count">0 Videos Ready</span>
        </div>

        <div class="editor-banner" id="editor_banner"></div>

        <div class="controls">
            <input type="text" id="bulk_caption" class="bulk-cap" placeholder="Bulk caption for all (leave empty for default)">
            <input type="date" id="schedule_date" title="Schedule Date">
            <input type="time" id="schedule_time" title="Schedule Time (24h)">
            <button class="btn" onclick="applyBulkCaption()">Apply Caption</button>
            <button class="btn" onclick="loadVideos()">Refresh</button>
            <button class="btn btn-primary" id="start_btn" onclick="startBatch()">Start Batch Schedule</button>
            <button class="btn btn-danger" id="stop_btn" onclick="stopBatch()">Stop</button>
        </div>

        <div class="grid" id="video_grid"></div>

        <div class="progress-section">
            <div class="prog-top">
                <span id="status_msg">Ready</span>
                <span id="status_pct">0%</span>
            </div>
            <div class="bar-bg"><div class="bar-fill" id="status_bar"></div></div>
            <div class="log-box" id="log_box">Waiting for videos...</div>
        </div>
    </div>

    <script>
        let videoItems = [];

        async function loadVideos() {
            const res = await fetch('/api/get_videos');
            const data = await res.json();

            const banner = document.getElementById("editor_banner");
            if (data.editor && data.editor.active) {
                banner.style.display = "block";
                banner.innerText = `Auto-Editor Running: ${data.editor.file} (${data.editor.step})`;
            } else {
                banner.style.display = "none";
            }

            const existing = new Map(videoItems.map(v => [v.fileName, v]));
            videoItems = data.videos.map(v => {
                if (existing.has(v.fileName)) {
                    return { ...existing.get(v.fileName), size: v.size };
                }
                return { fileName: v.fileName, size: v.size, selected: true, caption: "", imageBase64: "", done: false };
            });

            renderGrid();
        }

        function renderGrid() {
            const grid = document.getElementById("video_grid");
            document.getElementById("vid_count").innerText = `${videoItems.length} Videos Ready`;
            document.getElementById("start_btn").disabled = videoItems.length === 0;

            if (videoItems.length === 0) {
                grid.innerHTML = `<div style="color:#64748b;font-size:12px;padding:30px;text-align:center;grid-column:1/-1;">No edited videos yet. Download via Chrome (VDH) — they will appear here automatically.</div>`;
                return;
            }

            grid.innerHTML = "";
            videoItems.forEach((item, idx) => {
                const hasImg = !!item.imageBase64;
                const card = document.createElement("div");
                card.className = "card" + (item.done ? " done" : "");
                card.innerHTML = `
                    <div class="card-head">
                        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
                            <input type="checkbox" ${item.selected ? "checked" : ""} onchange="videoItems[${idx}].selected = this.checked">
                            <span class="file-title" title="${item.fileName}">${item.fileName}</span>
                        </label>
                        <span style="font-size:11px;color:#94a3b8;">${item.size}</span>
                    </div>
                    <div class="player-row">
                        <video id="vid_${idx}" src="/preview/${encodeURIComponent(item.fileName)}" controls preload="metadata"></video>
                        <div class="thumb-side">
                            <div class="thumb-box" tabindex="0" onpaste="handlePaste(${idx}, event)" title="Click and press Ctrl+V to paste image">
                                <span class="thumb-hint" id="hint_${idx}" style="${hasImg ? 'display:none;' : ''}">Click & Ctrl+V<br>or Capture</span>
                                <img id="img_${idx}" src="${item.imageBase64}" style="${hasImg ? 'display:block;' : ''}">
                            </div>
                            <button class="btn-cap" onclick="captureFrame(${idx})">📸 Capture Frame</button>
                        </div>
                    </div>
                    <input type="text" placeholder="Custom caption (Leave empty for default)" value="${item.caption}" oninput="videoItems[${idx}].caption = this.value">
                `;
                grid.appendChild(card);
            });
        }

        async function captureFrame(idx) {
            const vid = document.getElementById(`vid_${idx}`);
            const item = videoItems[idx];
            const res = await fetch('/api/capture_frame', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename: item.fileName, timestamp: vid.currentTime || 0 })
            });
            const data = await res.json();
            if (data.imageBase64) {
                setThumb(idx, data.imageBase64);
            }
        }

        function handlePaste(idx, event) {
            const items = (event.clipboardData || event.originalEvent.clipboardData).items;
            for (let it of items) {
                if (it.type.indexOf("image") !== -1) {
                    const reader = new FileReader();
                    reader.onload = e => setThumb(idx, e.target.result);
                    reader.readAsDataURL(it.getAsFile());
                    event.preventDefault();
                    break;
                }
            }
        }

        function setThumb(idx, b64) {
            videoItems[idx].imageBase64 = b64;
            document.getElementById(`img_${idx}`).src = b64;
            document.getElementById(`img_${idx}`).style.display = "block";
            document.getElementById(`hint_${idx}`).style.display = "none";
        }

        function applyBulkCaption() {
            const val = document.getElementById("bulk_caption").value;
            videoItems.forEach(v => v.caption = val);
            renderGrid();
        }

        async function startBatch() {
            const tasks = videoItems.filter(v => v.selected && !v.done);
            if (tasks.length === 0) return alert("Select at least one video!");

            document.getElementById("start_btn").disabled = true;
            document.getElementById("stop_btn").style.display = "inline-block";

            await fetch('/api/start_batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tasks,
                    schedule_date: document.getElementById("schedule_date").value,
                    schedule_time: document.getElementById("schedule_time").value
                })
            });
        }

        async function stopBatch() {
            await fetch('/api/stop_batch', { method: 'POST' });
        }

        setInterval(async () => {
            const st = await (await fetch('/api/get_status')).json();
            document.getElementById("status_msg").innerText = st.msg;
            document.getElementById("status_pct").innerText = st.pct + "%";
            document.getElementById("status_bar").style.width = st.pct + "%";

            if (st.logs && st.logs.length > 0) {
                const box = document.getElementById("log_box");
                box.innerHTML = st.logs.join("<br>");
                box.scrollTop = box.scrollHeight;
            }

            if (st.completed_files && st.completed_files.length > 0) {
                let changed = false;
                videoItems.forEach(v => {
                    if (st.completed_files.includes(v.fileName) && !v.done) {
                        v.done = true;
                        changed = true;
                    }
                });
                if (changed) renderGrid();
            }

            if (!st.running) {
                document.getElementById("start_btn").disabled = videoItems.length === 0;
                document.getElementById("stop_btn").style.display = "none";
            }
        }, 1000);

        setInterval(() => {
            if (!document.querySelector("video:not([paused])")) {
                loadVideos();
            }
        }, 4000);

        loadVideos();
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    threading.Thread(target=background_download_watcher, daemon=True).start()
    app_flask.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
