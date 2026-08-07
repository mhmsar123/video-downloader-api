import os
import re
import threading
import time
import uuid
import glob
import shutil
import socket

import yt_dlp
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
CLEANUP_AGE_SEC = int(os.environ.get("CLEANUP_AGE_SEC", str(2 * 3600)))
API_KEY = os.environ.get("API_KEY", "")
COOKIES_FILE = os.environ.get("COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))

app = Flask(__name__)
CORS(app)

jobs = {}
jobs_lock = threading.Lock()
semaphore = threading.BoundedSemaphore(MAX_CONCURRENT)

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "geo_bypass": True,
    "noplaylist": True,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    },
}


def ydl_opts(**extra):
    opts = {**BASE_OPTS, **extra}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


@app.route("/api/cookies", methods=["POST"])
def upload_cookies():
    if not require_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_data(as_text=True)
    if not data or "youtube.com" not in data and "# Netscape" not in data:
        return jsonify({"error": "Invalid cookies file"}), 400
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(data)
    return jsonify({"success": True, "message": "Cookies saved"})


@app.route("/api/cookies", methods=["GET"])
def cookies_status():
    if not require_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"enabled": os.path.exists(COOKIES_FILE)})


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "video")
    return name.strip()[:120] or "video"


def client_ip():
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


rate_map = {}
rate_lock = threading.Lock()


def rate_limited(ip, limit=30, window=60):
    now = time.time()
    with rate_lock:
        timestamps = [t for t in rate_map.get(ip, []) if now - t < window]
        timestamps.append(now)
        rate_map[ip] = timestamps
        return len(timestamps) > limit


def require_api_key():
    if not API_KEY:
        return True
    provided = request.headers.get("X-API-Key", "")
    return provided == API_KEY


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active_jobs": len([j for j in jobs.values() if j["status"] in ("queued", "downloading", "processing")])})


def is_valid_url(url):
    return isinstance(url, str) and re.match(r"^https?://", url.strip())


@app.route("/api/info", methods=["POST"])
def get_info():
    if not require_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    ip = client_ip()
    if rate_limited(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    try:
        opts = ydl_opts(skip_download=True, extract_flat=False)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist" or "entries" in info:
            entries = []
            for entry in info.get("entries", []):
                if entry:
                    entries.append({
                        "title": entry.get("title") or entry.get("url", "Unknown"),
                        "url": entry.get("webpage_url") or entry.get("url", ""),
                        "duration": entry.get("duration"),
                    })
            return jsonify({
                "success": True,
                "is_playlist": True,
                "title": info.get("title", "Playlist"),
                "count": len(entries),
                "entries": entries,
            })

        formats = []
        seen = set()
        for f in info.get("formats", []):
            if f.get("vcodec") == "none":
                continue
            height = f.get("height")
            ext = f.get("ext") or "mp4"
            fid = f.get("format_id") or ""
            fps = f.get("fps")
            if height:
                key = (height, fps, ext)
                label = f"{height}p"
            elif fid in ("sd", "hd", "ld", "sd_mp4", "hd_mp4", "standard", "high"):
                key = ("named", fid)
                label = fid.upper()
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            formats.append({
                "label": label,
                "height": height,
                "fps": fps,
                "ext": ext,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "format_id": fid or label,
            })
        formats.sort(key=lambda x: (x["height"] or 0), reverse=True)

        duration = info.get("duration") or 0
        return jsonify({
            "success": True,
            "is_playlist": False,
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": duration,
            "uploader": info.get("uploader", ""),
            "webpage_url": info.get("webpage_url", url),
            "formats": formats,
        })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Could not fetch: {_clean_error(e)}"}), 422
    except Exception as e:
        return jsonify({"error": f"Fetch failed: {e}"}), 500


def _clean_error(e):
    msg = str(e)
    msg = re.sub(r"^\s*(ERROR|WARNING):\s*", "", msg)
    return msg.strip()[:300]


@app.route("/api/download", methods=["POST"])
def start_download():
    if not require_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    ip = client_ip()
    if rate_limited(ip, limit=20, window=60):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    quality = str(data.get("quality", "best"))
    audio_only = bool(data.get("audio_only", False))
    audio_quality = str(data.get("audio_quality", "192"))

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "progress": 0.0,
        "speed": None,
        "eta": None,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "title": "",
        "thumbnail": "",
        "filename": None,
        "error": None,
        "cancel": False,
        "audio_only": audio_only,
        "created": time.time(),
    }
    with jobs_lock:
        jobs[job_id] = job

    threading.Thread(target=_download_worker, args=(job, quality, audio_only, audio_quality), daemon=True).start()
    return jsonify({"success": True, "job_id": job_id, "status_url": f"/api/status/{job_id}"})


@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({k: job[k] for k in ("id", "status", "progress", "speed", "eta", "downloaded_bytes", "total_bytes", "title", "thumbnail", "filename", "error", "audio_only")})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        job["cancel"] = True
        return jsonify({"success": True})


@app.route("/api/download/<job_id>", methods=["GET"])
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] == "error":
            return jsonify({"error": job["error"]}), 422
        if job["status"] != "completed":
            return jsonify({"error": "Job not ready", "status": job["status"]}), 409
        path = job["filename"]
        title = job["title"] or "video"

    if not path or not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404

    ext = os.path.splitext(path)[1] or ".mp4"
    download_name = sanitize_filename(title) + ext
    return send_file(path, as_attachment=True, download_name=download_name)


def _download_worker(job, quality, audio_only, audio_quality):
    try:
        with semaphore:
            with jobs_lock:
                if job["cancel"]:
                    job["status"] = "cancelled"
                    return
                job["status"] = "downloading"

            job_dir = os.path.join(DOWNLOAD_DIR, job["id"])
            os.makedirs(job_dir, exist_ok=True)

            def hook(d):
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    with jobs_lock:
                        job["downloaded_bytes"] = downloaded
                        job["total_bytes"] = total
                        job["speed"] = d.get("speed")
                        job["eta"] = d.get("eta")
                        if total > 0:
                            job["progress"] = round(downloaded / total * 100, 1)
                        if job["cancel"]:
                            raise yt_dlp.utils.DownloadError("Cancelled")
                elif d["status"] == "finished":
                    with jobs_lock:
                        job["progress"] = 100.0
                        job["status"] = "processing"

            if audio_only:
                format_str = "bestaudio/best"
                postprocessors = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": audio_quality,
                }]
            else:
                if quality == "best":
                    format_str = "bestvideo+bestaudio/best"
                elif quality == "worst":
                    format_str = "worstvideo+worstaudio/worst"
                else:
                    m = re.match(r"(\d+)", quality)
                    height = m.group(1) if m else None
                    if height:
                        format_str = (
                            f"bestvideo[height<={height}]+bestaudio/"
                            f"bestvideo[height<={height}]/best"
                        )
                    elif quality in ("sd", "hd", "ld"):
                        format_str = f"{quality}/bestvideo+bestaudio/best"
                    else:
                        format_str = "bestvideo+bestaudio/best"
                postprocessors = [{
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }]

            opts = ydl_opts(
                format=format_str,
                outtmpl=os.path.join(job_dir, "%(id)s.%(ext)s"),
                progress_hooks=[hook],
                postprocessors=postprocessors,
                merge_output_format="mp4",
            )

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job["url"], download=True)
                with jobs_lock:
                    job["title"] = info.get("title", "") if info else ""
                    job["thumbnail"] = info.get("thumbnail", "") if info else ""

            files = sorted(glob.glob(os.path.join(job_dir, "*")))
            files = [f for f in files if not f.endswith(".part") and not f.endswith(".ytdl")]
            if not files:
                raise Exception("No file produced")
            final = max(files, key=os.path.getsize)
            for extra in files:
                if extra != final:
                    try:
                        os.remove(extra)
                    except OSError:
                        pass

            with jobs_lock:
                job["filename"] = final
                job["filesize"] = os.path.getsize(final)
                job["status"] = "completed"
                job["progress"] = 100.0

    except yt_dlp.utils.DownloadError as e:
        _fail_job(job, _clean_error(e))
    except Exception as e:
        _fail_job(job, str(e)[:300])


def _fail_job(job, message):
    with jobs_lock:
        job["status"] = "cancelled" if job["cancel"] else "error"
        job["error"] = message


def _cleanup_loop():
    while True:
        time.sleep(300)
        try:
            now = time.time()
            with jobs_lock:
                for job_id, job in list(jobs.items()):
                    if job["status"] in ("completed", "cancelled", "error") and now - job["created"] > CLEANUP_AGE_SEC:
                        job_dir = os.path.join(DOWNLOAD_DIR, job_id)
                        shutil.rmtree(job_dir, ignore_errors=True)
                        jobs.pop(job_id, None)
        except Exception:
            pass


def _delete_expired_job_files():
    try:
        now = time.time()
        for entry in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, entry)
            if os.path.isdir(path):
                if now - os.path.getmtime(path) > CLEANUP_AGE_SEC:
                    with jobs_lock:
                        if entry not in jobs:
                            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def get_host_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    _delete_expired_job_files()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    print(f"Video Downloader API running on http://{get_host_ip()}:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
