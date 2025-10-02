import os
import textwrap
import subprocess
from flask import Flask, jsonify
from google.cloud import storage, texttospeech
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

    # --- Synthesize TTS ---
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    tmp_audio = "/tmp/audio.mp3"
    with open(tmp_audio, "wb") as f:
        f.write(response.audio_content)

    # --- Wrap text and dynamic font sizing ---
    img = Image.open(tmp_bg).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    # Max allowed height for text (70% of image height)
    max_text_height = img.height * 0.7

    # Start with a large font size and reduce until text fits
    font_size = 100
    wrapped = textwrap.wrap(fact, width=30)
    while font_size > 10:
        font = ImageFont.truetype(font_path, font_size)
        line_heights = [draw.textbbox((0,0), line, font=font)[3] - draw.textbbox((0,0), line, font=font)[1] for line in wrapped]
        total_height = sum(line_heights) + 10 * (len(wrapped) - 1)
        if total_height <= max_text_height:
            break
        font_size -= 2

    # Vertical centering
    y_start = (img.height - total_height) / 2

    # Draw each line centered
    y = y_start
    for i, line in enumerate(wrapped):
        bbox = draw.textbbox((0,0), line, font=font)
        line_width = bbox[2] - bbox[0]
        x = (img.width - line_width) / 2
        draw.text((x, y), line, font=font, fill="white")
        y += line_heights[i] + 10

    tmp_text_img = "/tmp/text_bg.jpg"
    img.save(tmp_text_img)

    tmp_out = "/tmp/output.mp4"

    # --- FFmpeg combine ---
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", tmp_text_img,
        "-i", tmp_audio,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-shortest",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=720:1280",
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
