import os
import textwrap
import subprocess
from flask import Flask, jsonify
from google.cloud import storage, texttospeech
from PIL import Image, ImageDraw, ImageFont
from mutagen.mp3 import MP3
import math
import tempfile

app = Flask(__name__)

# CONFIG
PROJECT_ID = "trivia-machine-472207"
REGION = "asia-southeast1"
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", "trivia-videos-output")

# Minimal Fact + Paths
FACT = "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
BACKGROUND_GCS_PATH = f"gs://{OUTPUT_BUCKET}/background.jpg"
OUTPUT_GCS_PATH = f"gs://{OUTPUT_BUCKET}/output.mp4"

def split_fact_to_phrases(fact, font_path, font_size, img_width, max_width_ratio=0.8):
    """Split fact into phrases that fit max width of the image."""
    font = ImageFont.truetype(font_path, font_size)
    words = fact.split()
    phrases = []
    current_phrase = []
    for word in words:
        test_phrase = " ".join(current_phrase + [word])
        dummy_img = Image.new("RGB", (img_width, 100))
        draw = ImageDraw.Draw(dummy_img)
        bbox = draw.textbbox((0,0), test_phrase, font=font)
        text_width = bbox[2] - bbox[0]
        if text_width <= img_width * max_width_ratio:
            current_phrase.append(word)
        else:
            if current_phrase:
                phrases.append(" ".join(current_phrase))
            current_phrase = [word]
    if current_phrase:
        phrases.append(" ".join(current_phrase))
    return phrases

def create_text_image(background_path, phrase, font_path, font_size):
    img = Image.open(background_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, font_size)
    bbox = draw.textbbox((0,0), phrase, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (img.width - text_width) / 2
    y = (img.height - text_height) / 2
    draw.text((x, y), phrase, font=font, fill="white")
    tmp_img = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    img.save(tmp_img.name)
    return tmp_img.name

def get_audio_duration(audio_path):
    audio = MP3(audio_path)
    return audio.info.length

def create_trivia_video(fact: str, background_gcs_path: str, output_gcs_path: str):
    storage_client = storage.Client()

    # Download background
    bg_bucket_name, bg_blob_name = background_gcs_path.replace("gs://", "").split("/", 1)
    bg_bucket = storage_client.bucket(bg_bucket_name)
    bg_blob = bg_bucket.blob(bg_blob_name)
    tmp_bg = "/tmp/background.jpg"
    bg_blob.download_to_filename(tmp_bg)

    # Synthesize TTS (full fact)
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    tmp_audio = "/tmp/audio.mp3"
    with open(tmp_audio, "wb") as f:
        f.write(response.audio_content)

    # Split fact into phrases
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_size = 60
    img = Image.open(tmp_bg)
    phrases = split_fact_to_phrases(fact, font_path, font_size, img.width, max_width_ratio=0.8)

    # Determine timing for each phrase based on number of words
    words = fact.split()
    total_duration = get_audio_duration(tmp_audio)
    words_per_phrase = [len(p.split()) for p in phrases]
    total_words = sum(words_per_phrase)
    timings = [total_duration * (w / total_words) for w in words_per_phrase]

    # Generate video segments per phrase
    video_segments = []
    current_time = 0.0
    for phrase, duration in zip(phrases, timings):
        img_path = create_text_image(tmp_bg, phrase, font_path, font_size)
        tmp_vid = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", img_path,
            "-ss", f"{current_time}",
            "-t", f"{duration}",
            "-i", tmp_audio,
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            "-shortest",
            tmp_vid
        ]
        subprocess.run(ffmpeg_cmd, check=True)
        video_segments.append(tmp_vid)
        current_time += duration

    # Concatenate segments
    concat_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    for seg in video_segments:
        concat_file.write(f"file '{seg}'\n")
    concat_file.close()
    tmp_out = "/tmp/output.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file.name,
        "-c", "copy",
        tmp_out
    ], check=True)

    # Upload final video
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
