import os
import subprocess
from flask import Flask, jsonify
from google.cloud import storage, texttospeech
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# CONFIG
PROJECT_ID = "trivia-machine-472207"
REGION = "asia-southeast1"
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", "trivia-videos-output")

# ---------------------------
# Minimal Fact + Paths
# ---------------------------
FACT = "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
BACKGROUND_GCS_PATH = f"gs://{OUTPUT_BUCKET}/background.jpg"
OUTPUT_GCS_PATH = f"gs://{OUTPUT_BUCKET}/output.mp4"

# ---------------------------
# Video Creation
# ---------------------------
def create_trivia_video(fact: str, background_gcs_path: str, output_gcs_path: str):
    storage_client = storage.Client()

    # --- Download background ---
    bg_bucket_name, bg_blob_name = background_gcs_path.replace("gs://", "").split("/", 1)
    bg_bucket = storage_client.bucket(bg_bucket_name)
    bg_blob = bg_bucket.blob(bg_blob_name)
    tmp_bg = "/tmp/background.jpg"
    bg_blob.download_to_filename(tmp_bg)

    # --- Synthesize TTS (full fact) ---
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    tmp_audio = "/tmp/audio.mp3"
    with open(tmp_audio, "wb") as f:
        f.write(response.audio_content)

    # --- Prepare image for text measurement ---
    img = Image.open(tmp_bg).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_size = 60
    font = ImageFont.truetype(font_path, font_size)
    max_text_width = img.width * 0.8  # 80% of screen width

    # --- Dynamically split fact into lines that fit 80% width ---
    words = fact.split()
    lines = []
    current_line = ""
    for word in words:
        test_line = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_width = bbox[2] - bbox[0]
        if line_width <= max_text_width:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)

    # --- Measure audio duration for each line ---
    audio = MP3(tmp_audio)
    total_duration = audio.info.length
    line_duration = total_duration / len(lines)

    # --- Build FFmpeg drawtext filters dynamically ---
    vf_filters = []
    for idx, line in enumerate(lines):
        start = idx * line_duration
        end = (idx + 1) * line_duration
        text_escaped = line.replace(":", "\\:").replace("'", "\\'")
        vf_filters.append(
            f"drawtext=fontfile={font_path}:text='{text_escaped}':fontcolor=white:fontsize={font_size}:x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,{start},{end})'"
        )
    vf_filter_str = ",".join(vf_filters)

    tmp_out = "/tmp/output.mp4"

    # --- FFmpeg combine ---
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", tmp_bg,
        "-i", tmp_audio,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-vf", vf_filter_str,
        tmp_out
    ]
    subprocess.run(ffmpeg_cmd, check=True)

    # --- Upload to GCS ---
    out_bucket_name, out_blob_name = output_gcs_path.replace("gs://", "").split("/", 1)
    out_bucket = storage_client.bucket(out_bucket_name)
    out_blob = out_bucket.blob(out_blob_name)
    out_blob.upload_from_filename(tmp_out)

    return output_gcs_path

# ---------------------------
# HTTP Endpoint
# ---------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    video_path = create_trivia_video(FACT, BACKGROUND_GCS_PATH, OUTPUT_GCS_PATH)
    return jsonify({"fact": FACT, "video_gcs": video_path})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
