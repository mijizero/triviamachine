import os
import subprocess
import tempfile
import traceback
from flask import Flask, request, jsonify
from google.cloud import texttospeech, storage

# pydub optional
try:
    from pydub import AudioSegment  # only for measuring duration
    _HAS_PYDUB = True
except Exception:
    _HAS_PYDUB = False

app = Flask(__name__)

# -------------------------------
# Helpers
# -------------------------------

def debug_print(*args):
    print("[DEBUG]", *args)

def escape_ffmpeg_text(text: str) -> str:
    """Escape text for FFmpeg drawtext filter (basic)."""
    return (
        text.replace(":", r"\\:")
            .replace("'", r"\\'")
            .replace(",", r"\\,")
            .replace("[", r"\\[")
            .replace("]", r"\\]")
    )

def split_text_for_screen(text: str, max_chars=25):
    words = text.split()
    lines, current = [], []
    for word in words:
        test_line = " ".join(current + [word])
        if len(test_line) > max_chars:
            if current:
                lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines

def get_audio_duration(path: str) -> float:
    """Return seconds. Prefer pydub, fallback to ffprobe."""
    if _HAS_PYDUB:
        audio = AudioSegment.from_file(path)
        return len(audio) / 1000.0
    # fallback to ffprobe (requires ffprobe in container)
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = p.stdout.strip()
    if not out:
        raise RuntimeError(f"ffprobe failed: {p.stderr.strip()}")
    return float(out)

def download_from_gcs(gcs_path: str, local_path: str):
    client = storage.Client()
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.download_to_filename(local_path)

def download_from_url(url: str, local_path: str, timeout=20):
    import requests
    r = requests.get(url, timeout=timeout, stream=True)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(1024 * 64):
            if chunk:
                f.write(chunk)

def download_background(background_ref: str, local_path: str):
    """Supports gs://... or http(s) URL. Raises exception on failure."""
    if background_ref.startswith("gs://"):
        debug_print("Downloading from GCS:", background_ref)
        download_from_gcs(background_ref, local_path)
    elif background_ref.startswith("http://") or background_ref.startswith("https://"):
        debug_print("Downloading from URL:", background_ref)
        download_from_url(background_ref, local_path)
    else:
        raise ValueError("background must be a gs:// path or http(s) URL")

def synthesize_speech(text: str, output_path: str):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # Neural2 voice (change if you want other Neural2 voices)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-C"
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.05
    )

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    with open(output_path, "wb") as f:
        f.write(response.audio_content)

def upload_to_gcs(local_path: str, gcs_path: str) -> str:
    client = storage.Client()
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    # return public URL style
    return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

# -------------------------------
# Core video creation
# -------------------------------

def create_trivia_video(fact_text: str, background_ref: str, output_gcs_path: str) -> str:
    debug_print("create_trivia_video start")
    with tempfile.TemporaryDirectory() as tmpdir:
        bg_local = f"{tmpdir}/background.jpg"
        audio_local = f"{tmpdir}/audio.mp3"
        out_local = f"{tmpdir}/output.mp4"

        # download background
        download_background(background_ref, bg_local)
        debug_print("background downloaded ->", bg_local)

        # TTS
        synthesize_speech(fact_text, audio_local)
        debug_print("tts audio written ->", audio_local)

        # measure audio
        audio_duration = get_audio_duration(audio_local)
        debug_print("audio duration:", audio_duration)

        # split into phrases
        phrases = split_text_for_screen(fact_text, max_chars=25)
        if not phrases:
            phrases = [fact_text]
        phrase_duration = audio_duration / len(phrases)

        # build drawtext filters using system font 'sans'
        filters = []
        font_size = 60
        for i, phrase in enumerate(phrases):
            phrase_safe = escape_ffmpeg_text(phrase)
            start = round(i * phrase_duration, 2)
            end = round((i + 1) * phrase_duration, 2)
            fstr = (
                f"drawtext=font=sans:text='{phrase_safe}':"
                f"fontcolor=white:fontsize={font_size}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:"
                f"enable='between(t,{start},{end})'"
            )
            filters.append(fstr)
        filter_complex = ",".join(filters)
        debug_print("filter_complex:", filter_complex)

        # run ffmpeg
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", bg_local,
            "-i", audio_local,
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-pix_fmt", "yuv420p",
            "-shortest",
            "-vf", filter_complex,
            out_local
        ]
        debug_print("Running ffmpeg:", " ".join(ffmpeg_cmd))
        p = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            debug_print("ffmpeg stdout:", p.stdout)
            debug_print("ffmpeg stderr:", p.stderr)
            raise RuntimeError(f"ffmpeg failed with code {p.returncode}: {p.stderr.strip()}")

        debug_print("ffmpeg succeeded, out:", out_local)

        # upload
        public_url = upload_to_gcs(out_local, output_gcs_path)
        debug_print("uploaded to gcs ->", public_url)
        return public_url

# -------------------------------
# Flask Endpoint
# -------------------------------

@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        data = request.get_json(silent=True) or {}

        fact = data.get("fact") or os.environ.get("DEFAULT_FACT")
        if not fact:
            return jsonify({"status":"error", "message":"Missing 'fact' in request body or DEFAULT_FACT env var"}), 400

        # background can be gs://bucket/object OR http(s) URL
        background = data.get("background") or os.environ.get("BACKGROUND_REF")
        if not background:
            return jsonify({"status":"error", "message":"Missing 'background' (gs://... or https://...) in request body or BACKGROUND_REF env var"}), 400

        # output gcs path required (must be gs://bucket/path.mp4)
        output = data.get("output") or os.environ.get("OUTPUT_GCS")
        if not output:
            return jsonify({"status":"error", "message":"Missing 'output' (gs://bucket/path.mp4) in request body or OUTPUT_GCS env var"}), 400

        debug_print("Request received", {"fact": fact[:80] + ("..." if len(fact)>80 else ""), "background": background, "output": output})
        result_url = create_trivia_video(fact, background, output)
        return jsonify({"status":"ok", "video_url": result_url})

    except Exception as e:
        debug_print("Exception in /generate:", str(e))
        traceback.print_exc()
        return jsonify({"status":"error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
