bash

cat /mnt/user-data/outputs/video_builder.py
Output

import os
import re
import json
import time
import base64
import logging
import tempfile
import subprocess
import requests
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ComedyVideoBuilder")

app = Flask(__name__)

PORT = int(os.environ.get("PORT", 8080))
OUTPUT_DIR = Path("/tmp/comedy_videos")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

job_status = {}


def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-800:])
    return result


def get_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(path)],
        capture_output=True, text=True
    )
    try:
        for s in json.loads(r.stdout).get("streams", []):
            if s.get("duration"):
                return float(s["duration"])
    except Exception:
        pass
    return 0.0


def save_voiceover(voice_audio_base64, out_path):
    """Decode the base64 MP3 that n8n already generated via Google TTS."""
    out_path.write_bytes(base64.b64decode(voice_audio_base64))
    return get_duration(out_path)


def download_footage(footage_urls, work_dir):
    """Download the direct MP4 clip URLs that n8n already selected via Pexels search."""
    clips = []
    for i, url in enumerate(footage_urls):
        try:
            out = work_dir / f"raw_{i}.mp4"
            with requests.get(url, stream=True, timeout=30) as dl:
                if dl.ok:
                    out.write_bytes(dl.content)
                    if out.stat().st_size > 10000:
                        clips.append(out)
        except Exception as e:
            log.warning("Footage download error for %s: %s", url, e)
    if not clips:
        raise RuntimeError("No footage clips downloaded")
    return clips


def escape_drawtext(text):
    text = text.replace("\\", "\\\\\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\u2019")
    text = text.replace("%", "\\%")
    return text


def build_video_job(job_id, voice_audio_base64, footage_urls, title_text, channel_tag):
    try:
        job_status[job_id] = "running"
        log.info("Job %s starting", job_id)

        with tempfile.TemporaryDirectory(prefix="comedy_") as tmp:
            work = Path(tmp)
            voice = work / "voice.mp3"

            log.info("Saving voiceover...")
            duration = save_voiceover(voice_audio_base64, voice)
            log.info("Voiceover saved: %.1fs", duration)

            log.info("Downloading footage...")
            raw_clips = download_footage(footage_urls, work)
            log.info("Got %d clips", len(raw_clips))

            norm = []
            for i, clip in enumerate(raw_clips):
                out = work / f"norm_{i}.mp4"
                try:
                    run_cmd(["ffmpeg", "-i", str(clip),
                             "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,"
                                    "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=25",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                             "-an", str(out), "-y"])
                    norm.append(out)
                except Exception:
                    continue
            if not norm:
                raise RuntimeError("No clips normalised")

            needed = duration + 2
            looped = []
            total = 0.0
            idx = 0
            while total < needed:
                src = norm[idx % len(norm)]
                d = get_duration(src) or 6.0
                looped.append(src)
                total += d
                idx += 1
                if idx > 100:
                    break

            concat_file = work / "concat.txt"
            concat_file.write_text("\n".join(f"file '{p}'" for p in looped))
            raw_video = work / "raw.mp4"
            run_cmd(["ffmpeg", "-f", "concat", "-safe", "0",
                     "-i", str(concat_file),
                     "-t", str(needed),
                     "-c", "copy", str(raw_video), "-y"])

            total_dur = needed
            punch_dur = min(3.5, total_dur * 0.4)
            punch_dur = max(punch_dur, 1.5)
            pre_dur = max(0.1, total_dur - punch_dur)

            safe_tag = escape_drawtext(channel_tag[:30].strip()) if channel_tag else ""
            tag_filter = (
                f"drawtext=text='{safe_tag}':fontsize=20:fontcolor=yellow:"
                f"bordercolor=black:borderw=1:x=w-tw-15:y=h-th-15"
                if safe_tag else "null"
            )

            title_filter = "null"
            if title_text:
                safe_title = escape_drawtext(title_text[:50].strip())
                title_filter = (
                    f"drawtext=text='{safe_title}':fontsize=42:fontcolor=white:"
                    f"bordercolor=black:borderw=3:x=(w-tw)/2:y=80:enable='between(t,0,3)'"
                )

            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", (title_text or "comedy_video"))[:35]
            out_path = OUTPUT_DIR / f"{safe_name}_{job_id}.mp4"

            filter_complex = (
                f"[0:v]trim=0:{pre_dur:.2f},setpts=PTS-STARTPTS,"
                f"scale=720:1280,"
                f"{title_filter},{tag_filter}[pre];"

                f"[0:v]trim={pre_dur:.2f}:{total_dur:.2f},setpts=PTS-STARTPTS,"
                f"scale=822:1462,"
                f"crop=720:1280:(822-720)/2:(1462-1280)/2,"
                f"eq=brightness=0.25:enable='between(t,0,0.12)',"
                f"{tag_filter}[punch];"

                f"[pre][punch]concat=n=2:v=1:a=0[vout]"
            )

            run_cmd(["ffmpeg",
                     "-i", str(raw_video),
                     "-i", str(voice),
                     "-filter_complex", filter_complex,
                     "-map", "[vout]", "-map", "1:a",
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                     "-c:a", "aac", "-b:a", "128k",
                     "-t", str(duration + 1),
                     "-movflags", "+faststart",
                     str(out_path), "-y"])

            size_mb = round(out_path.stat().st_size / 1_048_576, 1)
            log.info("Job %s complete: %s (%.1fMB)", job_id, out_path.name, size_mb)

            job_status[job_id] = {
                "status": "complete",
                "video_url": f"/download/{out_path.name}",
                "size_mb": size_mb,
                "duration_s": round(duration, 1)
            }

    except Exception as e:
        log.exception("Job %s failed", job_id)
        job_status[job_id] = {"status": "failed", "error": str(e)}


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"healthy": True})


@app.route("/build-video", methods=["POST"])
def build_video():
    data = request.get_json(force=True, silent=True) or {}

    voice_audio_base64 = data.get("voice_audio_base64", "").strip()
    footage_urls = data.get("footage_urls", [])
    title_text = data.get("title_text", "")
    channel_tag = data.get("channel_tag", "")
    job_id = str(data.get("job_id", int(time.time())))

    if not voice_audio_base64:
        return jsonify({"error": "voice_audio_base64 required"}), 400
    if not footage_urls:
        return jsonify({"error": "footage_urls required (list of direct mp4 links)"}), 400

    thread = threading.Thread(
        target=build_video_job,
        args=(job_id, voice_audio_base64, footage_urls, title_text, channel_tag),
        daemon=True
    )
    thread.start()

    return jsonify({
        "message": "Video build started",
        "job_id": job_id,
        "status_url": f"/status/{job_id}"
    }), 202


@app.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    s = job_status.get(job_id, "not_found")
    return jsonify({"job_id": job_id, "result": s})


@app.route("/download/<filename>", methods=["GET"])
def download(filename):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(path), mimetype="video/mp4",
                      as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
