import os
import re
import subprocess
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from pydub import AudioSegment

app = Flask(__name__)

# -------------------------------
# Helpers
# -------------------------------

def escape_ffmpeg_text(text: str) -> str:
    """Escape text for FFmpeg drawtext filter."""
    text = text.replace(":", r"\\:")
    text = text.replace("'", r"\\'")
    text = text.replace(",", r"\\,")
    text = text.replace("[", r"\\[")
    text = text.replace("]", r"\\]")
    return text

def split_text_for_screen(text: str, max_chars=25):
    """Split text into chunks that fit on screen lines."""
    words = text.split()
    lines = []
    current = []

    for word in words:
        test_line = " ".join(current + [word])
        if len(test_line) > max_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))

    return lines

def upload_to_gcs(local_path, gcs_path):
    """Upload file to GCS and return public URL."""
    client = storage.Client()
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

def download_from_gcs(gcs_path, local_path):
    """Download file from GCS to local path."""
    client = storage.Client()
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.download_to_filename(local_path)

def synthesize_speech(text, output_path):
    """Generate speech using Google Cloud Text-to-Speech (Neural2)."""
    client = texttospeech.TextToSpeechClient()

    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-C"  # Neural2 voice
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.1,
        pitch=0.0
    )

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    with open(output_path, "wb") as out:
        out.write(response.audio_content)

# -------------------------------
# Core: Create Video
# -------------------------------
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
import tempfile
import os
from google.cloud import storage

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create a trivia video with centered text using MoviePy + Pillow."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # Load audio
        audio_path = os.path.join(tmpdir, "audio.mp3")
        # Assuming synthesize_speech already writes audio here
        # synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Load background image
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Load font
        font_path = "Roboto-Regular.ttf"
        font_size = 60
        font = ImageFont.truetype(font_path, font_size)

        # Wrap text to fit screen
        max_width = img.width - 100
        words = fact_text.split()
        lines = []
        line = ""
        for word in words:
            test_line = f"{line} {word}".strip()
            bbox = draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_width:
                line = test_line
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)

        # Draw text centered vertically
        total_text_height = sum([draw.textbbox((0, 0), l, font=font)[3] - draw.textbbox((0, 0), l, font=font)[1] for l in lines])
        current_h = (img.height - total_text_height) // 2
        for line in lines:
            bbox = draw.textbbox((0,0), line, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            draw.text(((img.width - w) / 2, current_h), line, font=font, fill="white")
            current_h += h

        # Save annotated image
        annotated_path = os.path.join(tmpdir, "annotated.jpg")
        img.save(annotated_path)

        # Create video clip
        clip = ImageClip(annotated_path, duration=audio_duration)
        clip = clip.set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "output.mp4")
        clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path.replace("background.jpg", "output.mp4"))
        blob.upload_from_filename(output_path)
        return f"https://storage.googleapis.com/{bucket_name}/{blob.name}"

# -------------------------------
# Flask Endpoint
# -------------------------------

@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        # Allow empty POST (e.g. from Cloud Scheduler)
        data = request.get_json(silent=True) or {}

        fact = data.get("fact") or os.environ.get("DEFAULT_FACT") or \
            "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
        background_gcs_path = data.get("background") or os.environ.get("BACKGROUND_REF") or \
            "gs://trivia-videos-output/background.jpg"
        output_gcs_path = data.get("output") or os.environ.get("OUTPUT_GCS") or \
            "gs://trivia-videos-output/output.mp4"

        video_url = create_trivia_video(fact, background_gcs_path, output_gcs_path)
        print(f"✅ Generated video for fact: {fact}")  # Log to Cloud Run
        return jsonify({"status": "ok", "fact": fact, "video_url": video_url})

    except Exception as e:
        print(f"❌ Error: {e}")  # Log to Cloud Run
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
