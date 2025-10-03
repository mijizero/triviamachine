import os
import textwrap
import subprocess
from flask import Flask, jsonify
from google.cloud import storage, texttospeech
from PIL import Image, ImageDraw, ImageFont
from mutagen.mp3 import MP3

app = Flask(__name__)

# CONFIG
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", "trivia-videos-output")

FACT = "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
BACKGROUND_GCS_PATH = f"gs://{OUTPUT_BUCKET}/background.jpg"
OUTPUT_GCS_PATH = f"gs://{OUTPUT_BUCKET}/output.mp4"

def create_trivia_video(fact: str, background_gcs_path: str, output_gcs_path: str):
    storage_client = storage.Client()

    # --- Download background ---
    bg_bucket_name, bg_blob_name = background_gcs_path.replace("gs://", "").split("/", 1)
    bg_bucket = storage_client.bucket(bg_bucket_name)
    bg_blob = bg_bucket.blob(bg_blob_name)
    tmp_bg = "/tmp/background.jpg"
    bg_blob.download_to_filename(tmp_bg)

    # --- Synthesize continuous TTS ---
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    tmp_audio = "/tmp/audio.mp3"
    with open(tmp_audio, "wb") as f:
        f.write(response.audio_content)

    # --- Split text into phrases (~4 words each) ---
    words = fact.split()
    phrase_len = 4
    phrases = [' '.join(words[i:i+phrase_len]) for i in range(0, len(words), phrase_len)]

    # --- Measure audio duration ---
    audio = MP3(tmp_audio)
    total_audio_duration = audio.info.length
    duration_per_phrase = total_audio_duration / len(phrases)

    # --- Build FFmpeg drawtext filters ---
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    filters = []
    for i, phrase in enumerate(phrases):
        start = i * duration_per_phrase
        end = (i+1) * duration_per_phrase
        # drawtext filter with enable between start/end
        filters.append(
            f"drawtext=fontfile={font_path}:text='{phrase}':fontcolor=white:fontsize=60:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,{start},{end})'"
        )
    filter_complex = ",".join(filters)

    tmp_out = "/tmp/output.mp4"
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", tmp_bg,
        "-i", tmp_audio,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-vf", filter_complex,
        tmp_out
    ]
    subprocess.run(ffmpeg_cmd, check=True)

    # --- Upload to GCS ---
    out_bucket_name, out_blob_name = output_gcs_path.replace("gs://", "").split("/", 1)
    out_bucket = storage_client.bucket(out_bucket_name)
    out_blob = out_bucket.blob(out_blob_name)
    out_blob.upload_from_filename(tmp_out)

    return output_gcs_path

@app.route("/generate", methods=["POST"])
def generate_endpoint():
    video_path = create_trivia_video(FACT, BACKGROUND_GCS_PATH, OUTPUT_GCS_PATH)
    return jsonify({"fact": FACT, "video_gcs": video_path})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
