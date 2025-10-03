import os
import subprocess
from flask import Flask, jsonify
from google.cloud import storage, texttospeech
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# CONFIG
PROJECT_ID = "trivia-machine-472207"
REGION = "asia-southeast1"
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", "trivia-videos-output")

# Minimal Fact + Paths
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

    # --- Load background image ---
    img = Image.open(tmp_bg).convert("RGB")
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    max_width = img.width * 0.8  # 80% width for one line
    max_height = img.height * 0.7  # optional max height if needed

    # --- Prepare phrases to fit width ---
    font_size = 60
    font = ImageFont.truetype(font_path, font_size)
    draw = ImageDraw.Draw(img)

    words = fact.split()
    phrases = []
    temp_phrase = ""
    for word in words:
        test_phrase = (temp_phrase + " " + word).strip()
        w, _ = draw.textbbox((0, 0), test_phrase, font=font)[2:4]
        if w > max_width:
            if temp_phrase:
                phrases.append(temp_phrase)
            temp_phrase = word
        else:
            temp_phrase = test_phrase
    if temp_phrase:
        phrases.append(temp_phrase)

    # --- Generate TTS audio for each phrase ---
    tts_client = texttospeech.TextToSpeechClient()
    audio_files = []
    for i, phrase in enumerate(phrases):
        synthesis_input = texttospeech.SynthesisInput(text=phrase)
        voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
        response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
        tmp_audio = f"/tmp/audio_{i}.mp3"
        with open(tmp_audio, "wb") as f:
            f.write(response.audio_content)
        audio_files.append(tmp_audio)

    # --- Create a video segment for each phrase ---
    tmp_video_files = []
    for i, phrase in enumerate(phrases):
        frame = img.copy()
        draw_frame = ImageDraw.Draw(frame)
        bbox = draw_frame.textbbox((0, 0), phrase, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (img.width - text_w) / 2
        y = (img.height - text_h) / 2
        draw_frame.text((x, y), phrase, font=font, fill="white")
        tmp_img_file = f"/tmp/frame_{i}.jpg"
        frame.save(tmp_img_file)

        tmp_out = f"/tmp/output_{i}.mp4"
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", tmp_img_file,
            "-i", audio_files[i],
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-shortest",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=720:1280",
            tmp_out
        ]
        subprocess.run(ffmpeg_cmd, check=True)
        tmp_video_files.append(tmp_out)

    # --- Concatenate all video segments ---
    concat_file = "/tmp/concat.txt"
    with open(concat_file, "w") as f:
        for vid in tmp_video_files:
            f.write(f"file '{vid}'\n")
    tmp_out_final = "/tmp/output.mp4"
    ffmpeg_concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        tmp_out_final
    ]
    subprocess.run(ffmpeg_concat_cmd, check=True)

    # --- Upload to GCS ---
    out_bucket_name, out_blob_name = output_gcs_path.replace("gs://", "").split("/", 1)
    out_bucket = storage_client.bucket(out_bucket_name)
    out_blob = out_bucket.blob(out_blob_name)
    out_blob.upload_from_filename(tmp_out_final)

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
