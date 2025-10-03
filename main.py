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
    """Split text into chunks that fit nicely on screen."""
    words = text.split()
    lines, current = [], []

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

def synthesize_speech(text, output_path):
    """Generate speech with Google Cloud Text-to-Speech Neural2 (excited style)."""
    client = texttospeech.TextToSpeechClient()

    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-D"  # Neural2 voice
    )

    # Excited style, MP3 output
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        pitch=0.0,
        effects_profile_id=["large-home-entertainment-class-device"]
        # Note: Style support is implicit in Neural2 voices; 
        # "excited" style is handled by selecting Neural2 voice type.
    )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

    with open(output_path, "wb") as out:
        out.write(response.audio_content)

# -------------------------------
# Core: Create Video
# -------------------------------

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        subprocess.run(["gsutil", "cp", background_gcs_path, bg_path], check=True)

        # TTS generation with Neural2
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)

        # Measure audio
        audio = AudioSegment.from_file(audio_path)
        audio_duration = len(audio) / 1000.0  # seconds

        # Split text into screen lines
        phrases = split_text_for_screen(fact_text, max_chars=25)
        phrase_duration = audio_duration / len(phrases)

        # Build FFmpeg drawtext filters
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        font_size = 60
        drawtext_filters = []

        for i, phrase in enumerate(phrases):
            phrase_safe = escape_ffmpeg_text(phrase)
            start = round(i * phrase_duration, 2)
            end = round((i + 1) * phrase_duration, 2)

            filter_str = (
                f"drawtext=fontfile={font_path}:"
                f"text='{phrase_safe}':"
                f"fontcolor=white:fontsize={font_size}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:"
                f"enable='between(t,{start},{end})'"
            )
            drawtext_filters.append(filter_str)

        filter_complex = ",".join(drawtext_filters)

        # Output video
        output_path = os.path.join(tmpdir, "output.mp4")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", bg_path,
            "-i", audio_path,
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-pix_fmt", "yuv420p",
            "-shortest",
            "-vf", filter_complex,
            output_path
        ]

        subprocess.run(ffmpeg_cmd, check=True)

        # Upload to GCS
        return upload_to_gcs(output_path, output_gcs_path)

# -------------------------------
# Flask Endpoint
# -------------------------------

@app.route("/generate", methods=["POST"])
def generate_endpoint():
    # Allow empty POST (e.g. from Cloud Scheduler)
    data = request.get_json(silent=True) or {}

    fact = data.get(
        "fact",
        "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
    )
    background_gcs_path = data.get("background", "gs://my-bucket/background.jpg")
    output_gcs_path = data.get("output", "gs://my-bucket/output.mp4")

    try:
        video_url = create_trivia_video(fact, background_gcs_path, output_gcs_path)
        return jsonify({
            "status": "ok",
            "fact": fact,
            "video_url": video_url
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
