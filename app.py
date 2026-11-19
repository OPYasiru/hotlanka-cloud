import os
import re
import json
import time
import base64
import asyncio
import threading
import subprocess
from datetime import datetime, timedelta, timezone
from PIL import Image
from flask import Flask, render_template_string, request, jsonify, send_from_directory, abort
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
SCRUB_FOLDER = os.path.join(VIDEOS_FOLDER, "scrub_previews")
SAVED_THUMBS_FOLDER = os.path.join(VIDEOS_FOLDER, "saved_thumbs")
TEMP_FOLDER = os.path.join(VIDEOS_FOLDER, "temp")
META_FILE = os.path.join(VIDEOS_FOLDER, "metadata.json")
SESSION_PATH = os.path.join(BASE_DIR, "telethon_cloud_session")

FILE_STORE_BOT = "LankaFile_bot"
POST_BOT = "HotLanka_Bot"
DEFAULT_CAPTION = "🔥Hot Lanka Exclusive 🔥"
INTRO_SKIP_SECONDS = 8.5

SL_TZ = timezone(timedelta(hours=5, minutes=30))
# =================================================

for d in [DOWNLOADS_FOLDER, VIDEOS_FOLDER, SCRUB_FOLDER, SAVED_THUMBS_FOLDER, TEMP_FOLDER]:
    os.makedirs(d, exist_ok=True)

app_flask = Flask(__name__)

progress_state = {
    "mode": "idle",
    "msg": "Ready",
    "pct": 0
}

stop_flag = False
batch_lock = threading.Lock()
meta_lock = threading.Lock()
processed_downloads = set()
completed_uploads = []

# ================= PERSISTENT METADATA =================
def load_metadata():
    with meta_lock:
        if os.path.exists(META_FILE):
            try:
                with open(META_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

def save_metadata(data):
    with meta_lock:
        try:
            with open(META_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def update_video_meta(filename, **kwargs):
    data = load_metadata()
    entry = data.get(filename, {})
    entry.update(kwargs)
    data[filename] = entry
    save_metadata(data)

def delete_video_meta(filename):
    data = load_metadata()
    if filename in data:
        del data[filename]
        save_metadata(data)

def get_video_info(video_path):
    w, h, dur = 720, 1280, 0.0
    try:
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 720)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1280)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            dur = round(frames / fps, 2) if fps > 0 else 0.0
            cap.release()
    except Exception:
        pass
    aspect = "9:16" if h >= w else "16:9"
    return w, h, dur, aspect

# ================= SCRUBBING PREVIEW & HD FRAME EXTRACTOR =================
def generate_scrub_video(hd_path, filename):
    """Creates browser-compatible yuv420p fast-seek scrubbing file"""
    scrub_path = os.path.join(SCRUB_FOLDER, f"{filename}.mp4")
    if os.path.exists(scrub_path) and os.path.getsize(scrub_path) > 5000:
        return scrub_path

    temp_scrub = os.path.join(TEMP_FOLDER, f"scrub_{int(time.time()*1000)}_{filename}.mp4")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", hd_path,
        "-vf", "fps=2,scale='min(480,iw)':-2,format=yuv420p",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-g", "1", "-crf", "28", "-preset", "ultrafast",
        "-an", "-movflags", "+faststart",
        temp_scrub
    ]
    subprocess.run(cmd, check=True)
    if os.path.exists(temp_scrub) and os.path.getsize(temp_scrub) > 3000:
        if os.path.exists(scrub_path):
            os.remove(scrub_path)
        os.rename(temp_scrub, scrub_path)
    return scrub_path

def extract_hd_frame_at(hd_path, filename, timestamp_sec):
    """Captures 100% Original Full-HD frame at exact timestamp & saves permanently"""
    thumb_name = f"{filename}.jpg"
    thumb_path = os.path.join(SAVED_THUMBS_FOLDER, thumb_name)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(timestamp_sec)):.2f}",
        "-i", hd_path,
        "-frames:v", "1",
        "-q:v", "2",
        thumb_path
    ]
    subprocess.run(cmd, check=True)
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1000:
        update_video_meta(filename, thumbFile=thumb_name, thumbUpdated=time.time())
        return thumb_name
    return None

# ================= AUTO VIDEO EDITOR WITH % PROGRESS =================
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

    _, _, raw_dur, _ = get_video_info(raw_path)
    target_dur = max(1.0, raw_dur - INTRO_SKIP_SECONDS)

    if progress_state["mode"] != "uploading":
        progress_state["mode"] = "editing"
        progress_state["msg"] = f"Editing: '{filename}' (5%)"
        progress_state["pct"] = 5

    try:
        crop_filter = detect_black_bars_and_crop(raw_path, check_sec=9)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1",
            "-ss", str(INTRO_SKIP_SECONDS), "-i", raw_path
        ]
        if crop_filter:
            cmd.extend(["-vf", crop_filter])
        cmd.extend([
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", "-preset", "veryfast",
            "-movflags", "+faststart", "-c:a", "copy", temp_out
        ])

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=")[1].strip())
                    sec_done = us / 1_000_000.0
                    pct = min(85, max(5, int((sec_done / target_dur) * 85)))
                    if progress_state["mode"] != "uploading":
                        progress_state["msg"] = f"Editing: '{filename}' ({pct}%)"
                        progress_state["pct"] = pct
                except Exception:
                    pass
        proc.wait()

        if os.path.exists(temp_out) and os.path.getsize(temp_out) > 10240:
            if progress_state["mode"] != "uploading":
                progress_state["msg"] = f"Editing: '{filename}' (92%)"
                progress_state["pct"] = 92

            # Generate scrub video and default HD thumb BEFORE publishing to VIDEOS_FOLDER
            generate_scrub_video(temp_out, filename)
            _, _, dur, aspect = get_video_info(temp_out)
            default_seek = min(3.0, dur / 2.0) if dur > 0 else 1.0
            thumb_name = extract_hd_frame_at(temp_out, filename, default_seek)
            update_video_meta(filename, aspect=aspect, duration=dur, thumbFile=thumb_name or "", caption="")

            if os.path.exists(out_path):
                os.remove(out_path)
            os.rename(temp_out, out_path)

            if os.path.exists(raw_path):
                os.remove(raw_path)

            if progress_state["mode"] != "uploading":
                progress_state["msg"] = f"Editing: '{filename}' (100%)"
                progress_state["pct"] = 100
                time.sleep(0.5)
    except Exception:
        if os.path.exists(temp_out):
            os.remove(temp_out)
    finally:
        if progress_state["mode"] == "editing":
            progress_state["mode"] = "idle"
            progress_state["msg"] = "Ready"
            progress_state["pct"] = 0

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

# ================= TELETHON BATCH UPLOADER =================
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

def cleanup_video_files(f_name):
    for p in [
        os.path.join(VIDEOS_FOLDER, f_name),
        os.path.join(SCRUB_FOLDER, f"{f_name}.mp4"),
        os.path.join(SAVED_THUMBS_FOLDER, f"{f_name}.jpg")
    ]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    delete_video_meta(f_name)

def run_uploader_thread(tasks, schedule_date_str, schedule_time_str):
    global stop_flag
    if not batch_lock.acquire(blocking=False):
        return

    try:
        stop_flag = False
        progress_state["mode"] = "uploading"
        progress_state["msg"] = "Connecting to Telegram..."
        progress_state["pct"] = 2

        async def worker():
            global stop_flag
            client = TelegramClient(SESSION_PATH, API_ID, API_HASH, connection_retries=5)
            await client.connect()

            if not await client.is_user_authorized():
                progress_state["msg"] = "Error: Telegram not logged in!"
                progress_state["mode"] = "idle"
                return

            total = len(tasks)
            base_schedule_dt = calculate_schedule_datetime(schedule_date_str, schedule_time_str)

            for idx, item in enumerate(tasks, start=1):
                if stop_flag:
                    progress_state["msg"] = "Stopped by user"
                    break

                f_name = item['fileName']
                caption = item.get('caption', '').strip() or DEFAULT_CAPTION
                file_path = os.path.join(VIDEOS_FOLDER, f_name)

                if not os.path.exists(file_path):
                    continue

                saved_thumb_path = os.path.join(SAVED_THUMBS_FOLDER, f"{f_name}.jpg")
                post_photo_custom = saved_thumb_path if os.path.exists(saved_thumb_path) else None

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

                def progress_callback(cur, tot):
                    if stop_flag:
                        return
                    pct = int((cur / tot) * 85)
                    progress_state["pct"] = pct
                    progress_state["msg"] = f"Uploading [{idx}/{total}]: '{f_name}' ({pct}%)"

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

                progress_state["msg"] = f"Uploading [{idx}/{total}]: '{f_name}' (90%)"
                progress_state["pct"] = 90
                extracted_link = await get_fresh_link_telethon(client, last_bot_msg_id, wait_seconds=35)

                if extracted_link and not stop_flag:
                    current_item_schedule = None
                    if base_schedule_dt:
                        current_item_schedule = base_schedule_dt + timedelta(seconds=(idx - 1) * 2)

                    progress_state["msg"] = f"Uploading [{idx}/{total}]: '{f_name}' (96%)"
                    progress_state["pct"] = 96
                    post_payload = f"{caption}\n\n{extracted_link}"

                    if post_photo_custom and os.path.exists(post_photo_custom):
                        await client.send_file(POST_BOT, file=post_photo_custom, caption=post_payload, schedule=current_item_schedule)
                    else:
                        await client.send_message(POST_BOT, message=post_payload, schedule=current_item_schedule)

                    cleanup_video_files(f_name)
                    if auto_thumb and os.path.exists(auto_thumb):
                        os.remove(auto_thumb)

                    completed_uploads.append(f_name)
                    progress_state["pct"] = 100
                    progress_state["msg"] = f"Uploading [{idx}/{total}]: '{f_name}' (100%)"

                await asyncio.sleep(0.8)

            await client.disconnect()
            progress_state["mode"] = "idle"
            progress_state["pct"] = 0
            progress_state["msg"] = "All Tasks Completed!" if not stop_flag else "Stopped"

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker())
        loop.close()
    finally:
        batch_lock.release()

# ================= FLASK API ROUTES =================
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

@app_flask.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app_flask.route('/scrub/<path:filename>')
def serve_scrub(filename):
    scrub_p = os.path.join(SCRUB_FOLDER, f"{filename}.mp4")
    if not os.path.exists(scrub_p) or os.path.getsize(scrub_p) < 3000:
        hd_p = os.path.join(VIDEOS_FOLDER, filename)
        if os.path.exists(hd_p):
            try:
                generate_scrub_video(hd_p, filename)
            except Exception:
                abort(404)
        else:
            abort(404)
    return send_from_directory(SCRUB_FOLDER, f"{filename}.mp4")

@app_flask.route('/saved_thumb/<path:filename>')
def serve_saved_thumb(filename):
    return send_from_directory(SAVED_THUMBS_FOLDER, f"{filename}.jpg")

@app_flask.route('/api/get_videos')
def get_videos():
    supported_exts = ('.mp4', '.mkv', '.mov', '.avi', '.webm')
    meta = load_metadata()
    v_list = []

    if os.path.exists(VIDEOS_FOLDER):
        raw_files = [
            f for f in os.listdir(VIDEOS_FOLDER)
            if f.lower().endswith(supported_exts) and os.path.isfile(os.path.join(VIDEOS_FOLDER, f))
        ]
        for f in sorted(raw_files, key=natural_sort_key):
            full_p = os.path.join(VIDEOS_FOLDER, f)
            size_mb = f"{os.path.getsize(full_p) / (1024 * 1024):.1f} MB"
            entry = meta.get(f, {})

            scrub_p = os.path.join(SCRUB_FOLDER, f"{f}.mp4")
            if not os.path.exists(scrub_p) or os.path.getsize(scrub_p) < 3000:
                try:
                    generate_scrub_video(full_p, f)
                except Exception:
                    continue

            thumb_p = os.path.join(SAVED_THUMBS_FOLDER, f"{f}.jpg")
            if not os.path.exists(thumb_p):
                try:
                    _, _, dur, aspect = get_video_info(full_p)
                    extract_hd_frame_at(full_p, f, min(3.0, dur / 2.0) if dur > 0 else 1.0)
                    update_video_meta(f, aspect=aspect, duration=dur)
                    entry = load_metadata().get(f, {})
                except Exception:
                    pass

            v_list.append({
                'fileName': f,
                'size': size_mb,
                'aspect': entry.get('aspect', '9:16'),
                'caption': entry.get('caption', ''),
                'hasThumb': os.path.exists(thumb_p),
                'thumbVer': entry.get('thumbUpdated', 1)
            })

    return jsonify({"videos": v_list, "progress": progress_state, "completed": completed_uploads})

@app_flask.route('/api/capture_frame', methods=['POST'])
def api_capture_frame():
    data = request.json or {}
    filename = data.get("fileName", "")
    ts = float(data.get("timestamp", 0.0))
    hd_path = os.path.join(VIDEOS_FOLDER, filename)
    if not os.path.exists(hd_path):
        return jsonify({"ok": False}), 404

    t_name = extract_hd_frame_at(hd_path, filename, ts)
    return jsonify({"ok": bool(t_name), "thumbVer": time.time()})

@app_flask.route('/api/paste_thumb', methods=['POST'])
def api_paste_thumb():
    data = request.json or {}
    filename = data.get("fileName", "")
    b64 = data.get("imageBase64", "")
    if not filename or "," not in b64:
        return jsonify({"ok": False}), 400

    thumb_name = f"{filename}.jpg"
    thumb_path = os.path.join(SAVED_THUMBS_FOLDER, thumb_name)
    try:
        img_bytes = base64.b64decode(b64.split(",")[1])
        temp_p = os.path.join(TEMP_FOLDER, f"paste_{int(time.time()*1000)}.img")
        with open(temp_p, "wb") as f:
            f.write(img_bytes)
        with Image.open(temp_p) as im:
            im.convert("RGB").save(thumb_path, quality=95)
        os.remove(temp_p)
        now_v = time.time()
        update_video_meta(filename, thumbFile=thumb_name, thumbUpdated=now_v)
        return jsonify({"ok": True, "thumbVer": now_v})
    except Exception:
        return jsonify({"ok": False}), 500

@app_flask.route('/api/save_caption', methods=['POST'])
def api_save_caption():
    data = request.json or {}
    filename = data.get("fileName", "")
    caption = data.get("caption", "")
    if filename:
        update_video_meta(filename, caption=caption)
    return jsonify({"ok": True})

@app_flask.route('/api/delete_video', methods=['POST'])
def api_delete_video():
    data = request.json or {}
    filename = data.get("fileName", "")
    if filename:
        cleanup_video_files(filename)
    return jsonify({"ok": True})

@app_flask.route('/api/start_batch', methods=['POST'])
def start_batch():
    payload = request.json or {}
    tasks = payload.get("tasks", [])
    if not tasks:
        return jsonify({"status": "no_tasks"}), 400
    threading.Thread(
        target=run_uploader_thread,
        args=(tasks, payload.get("schedule_date", ""), payload.get("schedule_time", "")),
        daemon=True
    ).start()
    return jsonify({"status": "started"})

@app_flask.route('/api/stop_batch', methods=['POST'])
def stop_batch():
    global stop_flag
    stop_flag = True
    return jsonify({"status": "stopping"})

@app_flask.route('/api/shutdown_server', methods=['POST'])
def shutdown_server():
    def _stop():
        time.sleep(0.5)
        os.system("gh codespace stop -c $CODESPACE_NAME || sudo shutdown -h now")
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"status": "shutting_down"})

# ================= FINAL 65:35 MINIMALIST STUDIO UI =================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>HotLanka Cloud Studio</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            background: #090712;
            color: #f1f5f9;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .top-header {
            background: #0f0b1f;
            border-bottom: 1px solid #24164a;
            padding: 12px 18px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            flex-shrink: 0;
        }
        .header-row-1 {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
        }
        .brand { font-size: 16px; font-weight: 700; color: #fff; white-space: nowrap; }
        .progress-wrap { flex: 1; max-width: 480px; display: flex; flex-direction: column; gap: 5px; }
        .prog-text {
            font-size: 12px; font-weight: 500; color: #cbd5e1;
            text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .prog-track { width: 100%; height: 6px; background: #1b1236; border-radius: 6px; overflow: hidden; }
        .prog-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #8b5cf6, #22c55e); transition: width 0.25s ease; }
        .sys-actions { display: flex; gap: 8px; }

        .header-row-2 { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
        .count-pill {
            font-size: 12px; font-weight: 600; color: #c084fc;
            background: rgba(139, 92, 246, 0.14); border: 1px solid rgba(139, 92, 246, 0.35);
            padding: 5px 12px; border-radius: 20px;
        }
        .schedule-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

        input[type="text"], input[type="date"] {
            background: #080612; border: 1px solid #2b1c54; border-radius: 7px;
            padding: 6px 10px; color: #f8fafc; font-size: 12.5px; outline: none;
        }
        input:focus { border-color: #8b5cf6; }

        .time-selector {
            position: relative; display: inline-flex; align-items: center;
            background: #080612; border: 1px solid #2b1c54; border-radius: 7px;
            padding: 5px 10px; gap: 6px; cursor: pointer; font-size: 12.5px;
        }
        .time-popup {
            display: none; position: absolute; top: 115%; right: 0;
            background: #130d29; border: 1px solid #4c2f8a; border-radius: 9px;
            padding: 8px; z-index: 500; box-shadow: 0 12px 30px rgba(0,0,0,0.85); gap: 6px;
        }
        .time-popup.open { display: flex; }
        .time-col { max-height: 150px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; padding-right: 4px; }
        .time-opt { padding: 4px 10px; border-radius: 5px; font-size: 12px; cursor: pointer; text-align: center; }
        .time-opt:hover, .time-opt.active { background: #7c3aed; color: #fff; }

        .btn {
            background: #1b1236; border: 1px solid #38246b; color: #e9d5ff;
            padding: 6px 12px; border-radius: 7px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap;
        }
        .btn:hover { background: #291b52; }
        .btn-primary { background: linear-gradient(135deg, #8b5cf6, #7c3aed); border: none; color: #fff; padding: 7px 16px; }
        .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
        .btn-stop-server { background: rgba(239, 68, 68, 0.14); border: 1px solid rgba(239, 68, 68, 0.45); color: #fca5a5; }
        .btn-stop-server:hover { background: #dc2626; color: #fff; }
        .btn-danger { background: #ef4444; border: none; color: #fff; display: none; }

        .workspace { flex: 1; display: flex; gap: 14px; padding: 14px 18px; overflow: hidden; }
        .left-list { flex: 0 0 65%; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding-right: 6px; }
        .left-list::-webkit-scrollbar { width: 6px; }
        .left-list::-webkit-scrollbar-thumb { background: #311f61; border-radius: 6px; }

        .v-card {
            background: #0f0b1f; border: 1px solid #221545; border-radius: 11px;
            padding: 10px 12px; display: flex; gap: 12px; align-items: center; cursor: pointer;
            transition: border-color 0.15s, background 0.15s;
        }
        .v-card:hover { border-color: #4c2f8a; }
        .v-card.active { border-color: #8b5cf6; background: #150f2b; }

        .thumb-box {
            width: 78px; height: 112px; border-radius: 8px; background: #080612;
            border: 1px dashed #43297a; flex-shrink: 0; position: relative; overflow: hidden;
            display: flex; align-items: center; justify-content: center;
        }
        .thumb-box.landscape { width: 128px; height: 76px; }
        .thumb-box img { width: 100%; height: 100%; object-fit: cover; }
        .thumb-hint {
            position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0, 0, 0, 0.72);
            font-size: 9px; color: #cbd5e1; text-align: center; padding: 2px;
        }

        .card-info { flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
        .card-top-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .v-name { font-size: 13.5px; font-weight: 600; color: #f8fafc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .v-meta { font-size: 11px; color: #94a3b8; }
        .card-actions { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
        .card-actions input[type="checkbox"] { width: 16px; height: 16px; accent-color: #8b5cf6; cursor: pointer; }
        .del-btn {
            background: transparent; border: 1px solid rgba(239, 68, 68, 0.35); color: #f87171;
            padding: 3px 8px; border-radius: 6px; font-size: 11px; cursor: pointer;
        }
        .del-btn:hover { background: #ef4444; color: #fff; }
        .cap-label { font-size: 11px; color: #94a3b8; }
        .cap-box { width: 100%; background: #080612; border: 1px solid #26184f; border-radius: 7px; padding: 7px 10px; color: #f1f5f9; font-size: 12.5px; }

        .right-player {
            flex: 1; background: #0f0b1f; border: 1px solid #221545; border-radius: 12px;
            padding: 12px; display: flex; flex-direction: column; justify-content: space-between; gap: 10px;
        }
        .player-title { font-size: 13px; font-weight: 600; color: #e2e8f0; display: flex; justify-content: space-between; }
        .video-stage {
            flex: 1; background: #05040a; border-radius: 9px;
            display: flex; align-items: center; justify-content: center; overflow: hidden; position: relative;
        }
        .video-stage video { max-width: 100%; max-height: 100%; object-fit: contain; }
        .seek-controls { display: flex; flex-direction: column; gap: 8px; }
        .seek-row { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: #94a3b8; font-family: monospace; }
        .seek-slider { flex: 1; accent-color: #8b5cf6; cursor: pointer; }
        .btn-capture {
            width: 100%; background: linear-gradient(135deg, #8b5cf6, #6d28d9); border: none; color: #fff;
            padding: 9px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;
        }
        .btn-capture:hover { opacity: 0.92; }
        .btn-capture:disabled { opacity: 0.4; cursor: not-allowed; }

        .hover-zoom {
            position: fixed; pointer-events: none; z-index: 9999; background: #0f0b1f;
            border: 2px solid #8b5cf6; border-radius: 10px; box-shadow: 0 18px 40px rgba(0,0,0,0.92);
            max-width: 360px; max-height: 440px; object-fit: contain; display: none;
        }

        @media (max-width: 820px) {
            body { height: auto; overflow-y: auto; }
            .workspace { flex-direction: column-reverse; overflow: visible; }
            .left-list { flex: none; overflow: visible; }
            .video-stage { height: 280px; }
        }
    </style>
</head>
<body>
    <img id="hover_zoom" class="hover-zoom" src="">

    <div class="top-header">
        <div class="header-row-1">
            <span class="brand">HotLanka Cloud Studio</span>

            <div class="progress-wrap">
                <div class="prog-text" id="prog_text">Ready</div>
                <div class="prog-track"><div class="prog-fill" id="prog_fill"></div></div>
            </div>

            <div class="sys-actions">
                <button class="btn" onclick="openCloudBrowser()">🌐 Open Cloud Browser</button>
                <button class="btn btn-stop-server" onclick="shutdownServer()">⏻ Stop Server</button>
            </div>
        </div>

        <div class="header-row-2">
            <span class="count-pill" id="vid_count">0 Videos Ready</span>

            <div class="schedule-controls">
                <input type="text" id="bulk_caption" placeholder="Bulk caption..." style="width:170px;">
                <input type="date" id="schedule_date">

                <div class="time-selector" onclick="toggleTimePopup(event)">
                    <span>🕒</span>
                    <span id="time_display">Now</span>
                    <span onclick="clearTime(event)" style="color:#94a3b8;padding-left:4px;">✕</span>
                    <div class="time-popup" id="time_popup" onclick="event.stopPropagation()">
                        <div class="time-col" id="hour_col"></div>
                        <div class="time-col" id="min_col"></div>
                    </div>
                </div>

                <button class="btn" onclick="applyBulkCaption()">Apply Caption</button>
                <button class="btn btn-primary" id="start_btn" onclick="startBatch()">Start Batch Schedule</button>
                <button class="btn btn-danger" id="stop_btn" onclick="stopBatch()">Stop</button>
            </div>
        </div>
    </div>

    <div class="workspace">
        <div class="left-list" id="video_list"></div>

        <div class="right-player">
            <div class="player-title">
                <span>Preview Player</span>
                <span id="active_vid_label" style="color:#a78bfa;font-weight:500;">Select a video</span>
            </div>

            <div class="video-stage">
                <video id="scrub_player" preload="auto" muted playsinline></video>
            </div>

            <div class="seek-controls">
                <div class="seek-row">
                    <button class="btn" id="play_btn" onclick="togglePlay()" style="padding:3px 8px;">▶</button>
                    <input type="range" class="seek-slider" id="seek_slider" min="0" max="100" step="0.1" value="0">
                    <span id="time_readout">00:00 / 00:00</span>
                </div>
                <button class="btn-capture" id="capture_btn" onclick="captureCurrentFrame()" disabled>📸 Capture Frame</button>
            </div>
        </div>
    </div>

    <script>
        let videos = [];
        let activeFileName = null;
        let selectedHour = "";
        let selectedMin = "";
        let blobCache = {};
        let captionTimers = {};

        (function initTimePicker() {
            const hCol = document.getElementById("hour_col");
            const mCol = document.getElementById("min_col");
            for (let h = 0; h < 24; h++) {
                const hs = h.toString().padStart(2, '0');
                const d = document.createElement("div");
                d.className = "time-opt";
                d.innerText = hs;
                d.onclick = () => { selectedHour = hs; if (!selectedMin) selectedMin = "00"; updateTimeLabel(); };
                hCol.appendChild(d);
            }
            for (let m = 0; m < 60; m += 5) {
                const ms = m.toString().padStart(2, '0');
                const d = document.createElement("div");
                d.className = "time-opt";
                d.innerText = ms;
                d.onclick = () => { selectedMin = ms; if (!selectedHour) selectedHour = "12"; updateTimeLabel(); };
                mCol.appendChild(d);
            }
            document.addEventListener("click", () => document.getElementById("time_popup").classList.remove("open"));
        })();

        function toggleTimePopup(e) {
            e.stopPropagation();
            document.getElementById("time_popup").classList.toggle("open");
        }
        function clearTime(e) {
            e.stopPropagation();
            selectedHour = "";
            selectedMin = "";
            document.getElementById("time_display").innerText = "Now";
            document.getElementById("time_popup").classList.remove("open");
        }
        function updateTimeLabel() {
            document.getElementById("time_display").innerText = `${selectedHour}:${selectedMin}`;
        }

        function openCloudBrowser() {
            const vncUrl = window.location.origin.replace("-5000.", "-6080.") + "/vnc.html?autoconnect=true&resize=scale";
            window.open(vncUrl, "_blank");
        }

        async function shutdownServer() {
            if (!confirm("Shutdown Codespace server now to save CPU hours?")) return;
            document.getElementById("prog_text").innerText = "Shutting down server...";
            await fetch('/api/shutdown_server', { method: 'POST' });
        }

        function showZoom(src, e) {
            if (!src) return;
            const z = document.getElementById("hover_zoom");
            z.src = src;
            z.style.display = "block";
            moveZoom(e);
        }
        function moveZoom(e) {
            const z = document.getElementById("hover_zoom");
            if (z.style.display !== "block") return;
            let left = e.clientX + 18;
            let top = e.clientY - 140;
            if (left + 370 > window.innerWidth) left = e.clientX - 370;
            if (top < 10) top = 10;
            if (top + 380 > window.innerHeight) top = window.innerHeight - 380;
            z.style.left = left + "px";
            z.style.top = top + "px";
        }
        function hideZoom() {
            document.getElementById("hover_zoom").style.display = "none";
        }

        const player = document.getElementById("scrub_player");
        const slider = document.getElementById("seek_slider");

        function fmtTime(sec) {
            if (!isFinite(sec)) return "00:00";
            const m = Math.floor(sec / 60).toString().padStart(2, '0');
            const s = Math.floor(sec % 60).toString().padStart(2, '0');
            return `${m}:${s}`;
        }

        slider.addEventListener("input", () => {
            if (player.duration) {
                player.currentTime = (slider.value / 100) * player.duration;
                document.getElementById("time_readout").innerText = `${fmtTime(player.currentTime)} / ${fmtTime(player.duration)}`;
            }
        });

        player.addEventListener("timeupdate", () => {
            if (player.duration) {
                slider.value = (player.currentTime / player.duration) * 100;
                document.getElementById("time_readout").innerText = `${fmtTime(player.currentTime)} / ${fmtTime(player.duration)}`;
            }
        });

        function togglePlay() {
            if (!player.src) return;
            if (player.paused) {
                player.play();
                document.getElementById("play_btn").innerText = "⏸";
            } else {
                player.pause();
                document.getElementById("play_btn").innerText = "▶";
            }
        }

        async function selectVideoForPreview(fileName) {
            if (activeFileName === fileName && player.src) return;
            activeFileName = fileName;
            document.getElementById("active_vid_label").innerText = fileName;
            document.getElementById("capture_btn").disabled = false;
            document.getElementById("play_btn").innerText = "▶";

            document.querySelectorAll(".v-card").forEach(c => {
                c.classList.toggle("active", c.dataset.file === fileName);
            });

            if (!blobCache[fileName]) {
                try {
                    const resp = await fetch(`/scrub/${encodeURIComponent(fileName)}`);
                    if (resp.ok) {
                        const blob = await resp.blob();
                        if (blob.size > 3000) {
                            blobCache[fileName] = URL.createObjectURL(blob);
                        }
                    }
                } catch (e) {}
            }

            if (blobCache[fileName]) {
                player.src = blobCache[fileName];
                player.load();
                player.currentTime = 0;
            }
        }

        async function captureCurrentFrame() {
            if (!activeFileName) return;
            const btn = document.getElementById("capture_btn");
            btn.disabled = true;
            btn.innerText = "Capturing HD...";

            const res = await fetch('/api/capture_frame', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fileName: activeFileName, timestamp: player.currentTime })
            });
            const data = await res.json();
            if (data.ok) {
                const v = videos.find(x => x.fileName === activeFileName);
                if (v) v.thumbVer = data.thumbVer;
                const imgEl = document.querySelector(`.v-card[data-file="${CSS.escape(activeFileName)}"] .thumb-box img`);
                if (imgEl) imgEl.src = `/saved_thumb/${encodeURIComponent(activeFileName)}?v=${data.thumbVer}`;
            }

            btn.innerText = "📸 Capture Frame";
            btn.disabled = false;
        }

        function handlePaste(fileName, event) {
            event.stopPropagation();
            const items = (event.clipboardData || event.originalEvent.clipboardData).items;
            for (let it of items) {
                if (it.type.indexOf("image") !== -1) {
                    const reader = new FileReader();
                    reader.onload = async e => {
                        const res = await fetch('/api/paste_thumb', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ fileName, imageBase64: e.target.result })
                        });
                        const data = await res.json();
                        if (data.ok) {
                            const imgEl = document.querySelector(`.v-card[data-file="${CSS.escape(fileName)}"] .thumb-box img`);
                            if (imgEl) imgEl.src = `/saved_thumb/${encodeURIComponent(fileName)}?v=${data.thumbVer}`;
                        }
                    };
                    reader.readAsDataURL(it.getAsFile());
                    event.preventDefault();
                    break;
                }
            }
        }

        function onCaptionInput(fileName, val) {
            const v = videos.find(x => x.fileName === fileName);
            if (v) v.caption = val;
            clearTimeout(captionTimers[fileName]);
            captionTimers[fileName] = setTimeout(() => {
                fetch('/api/save_caption', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ fileName, caption: val })
                });
            }, 400);
        }

        async function deleteVideo(fileName, e) {
            e.stopPropagation();
            await fetch('/api/delete_video', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fileName })
            });
            videos = videos.filter(v => v.fileName !== fileName);
            delete blobCache[fileName];
            const card = document.querySelector(`.v-card[data-file="${CSS.escape(fileName)}"]`);
            if (card) card.remove();
            updateCountBadge();
            if (activeFileName === fileName) {
                activeFileName = null;
                player.removeAttribute("src");
                document.getElementById("active_vid_label").innerText = "Select a video";
                document.getElementById("capture_btn").disabled = true;
            }
        }

        function applyBulkCaption() {
            const val = document.getElementById("bulk_caption").value;
            videos.forEach(v => {
                v.caption = val;
                const inp = document.querySelector(`.v-card[data-file="${CSS.escape(v.fileName)}"] .cap-box`);
                if (inp) inp.value = val;
                fetch('/api/save_caption', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ fileName: v.fileName, caption: val })
                });
            });
        }

        function updateCountBadge() {
            document.getElementById("vid_count").innerText = `${videos.length} Videos Ready`;
            document.getElementById("start_btn").disabled = videos.length === 0;
        }

        function createCardElement(v) {
            const card = document.createElement("div");
            card.className = "v-card" + (activeFileName === v.fileName ? " active" : "");
            card.dataset.file = v.fileName;
            card.onclick = () => selectVideoForPreview(v.fileName);

            const thumbUrl = `/saved_thumb/${encodeURIComponent(v.fileName)}?v=${v.thumbVer}`;
            const isLand = v.aspect === "16:9" ? "landscape" : "";

            card.innerHTML = `
                <div class="thumb-box ${isLand}" tabindex="0"
                     onclick="event.stopPropagation(); this.focus();"
                     onpaste="handlePaste('${v.fileName.replace(/'/g, "\\'")}', event)"
                     onmouseenter="showZoom(this.querySelector('img').src, event)"
                     onmousemove="moveZoom(event)"
                     onmouseleave="hideZoom()">
                    <img src="${thumbUrl}" alt="">
                    <span class="thumb-hint">Hover + Ctrl+V</span>
                </div>
                <div class="card-info">
                    <div class="card-top-row">
                        <div>
                            <div class="v-name">${v.fileName}</div>
                            <div class="v-meta">${v.size} • ${v.aspect}</div>
                        </div>
                        <div class="card-actions" onclick="event.stopPropagation()">
                            <input type="checkbox" checked onchange="toggleSelect('${v.fileName.replace(/'/g, "\\'")}', this.checked)">
                            <button class="del-btn" onclick="deleteVideo('${v.fileName.replace(/'/g, "\\'")}', event)">🗑️ Delete</button>
                        </div>
                    </div>
                    <span class="cap-label">Caption</span>
                    <input type="text" class="cap-box" placeholder="🔥Hot Lanka Exclusive 🔥"
                           value="${(v.caption || '').replace(/"/g, '&quot;')}"
                           onclick="event.stopPropagation()"
                           oninput="onCaptionInput('${v.fileName.replace(/'/g, "\\'")}', this.value)">
                </div>
            `;
            return card;
        }

        function toggleSelect(fileName, checked) {
            const v = videos.find(x => x.fileName === fileName);
            if (v) v.selected = checked;
        }

        async function syncDashboard() {
            try {
                const res = await fetch('/api/get_videos');
                const data = await res.json();

                if (data.progress) {
                    document.getElementById("prog_text").innerText = data.progress.msg;
                    document.getElementById("prog_fill").style.width = data.progress.pct + "%";
                    const isUploading = data.progress.mode === "uploading";
                    document.getElementById("stop_btn").style.display = isUploading ? "inline-block" : "none";
                    if (!isUploading) {
                        document.getElementById("start_btn").disabled = data.videos.length === 0;
                    }
                }

                const listEl = document.getElementById("video_list");
                const serverNames = new Set(data.videos.map(x => x.fileName));

                videos = videos.filter(v => {
                    if (!serverNames.has(v.fileName)) {
                        const el = document.querySelector(`.v-card[data-file="${CSS.escape(v.fileName)}"]`);
                        if (el) el.remove();
                        return false;
                    }
                    return true;
                });

                const localNames = new Set(videos.map(x => x.fileName));
                data.videos.forEach(sv => {
                    if (!localNames.has(sv.fileName)) {
                        const newObj = { ...sv, selected: true };
                        videos.push(newObj);
                        listEl.appendChild(createCardElement(newObj));
                    }
                });

                updateCountBadge();

                if (!activeFileName && videos.length > 0) {
                    selectVideoForPreview(videos[0].fileName);
                }
            } catch (e) {}
        }

        async function startBatch() {
            const tasks = videos.filter(v => v.selected);
            if (tasks.length === 0) return alert("Select at least one video!");

            document.getElementById("start_btn").disabled = true;
            document.getElementById("stop_btn").style.display = "inline-block";

            const timeStr = (selectedHour && selectedMin) ? `${selectedHour}:${selectedMin}` : "";
            await fetch('/api/start_batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tasks,
                    schedule_date: document.getElementById("schedule_date").value,
                    schedule_time: timeStr
                })
            });
        }

        async function stopBatch() {
            await fetch('/api/stop_batch', { method: 'POST' });
        }

        setInterval(syncDashboard, 1500);
        syncDashboard();
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    threading.Thread(target=background_download_watcher, daemon=True).start()
    app_flask.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
