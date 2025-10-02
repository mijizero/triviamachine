import os
import random
import json
import requests
from flask import Flask, jsonify
from google.cloud import secretmanager, storage, texttospeech, aiplatform
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
import textwrap
import subprocess

app = Flask(__name__)

# ---------------------------
# CONFIG
# ---------------------------
PROJECT_ID = "trivia-machine-472207"
REGION = "asia-southeast1"
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", "trivia-videos-output")
YT_SECRET = "Credentials_Trivia"

# ---------------------------
# Secret / Credential Helpers
# ---------------------------
def get_credentials(secret_name: str):
    client = secretmanager.SecretManagerServiceClient()
    secret_path = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": secret_path})
    secret_payload = response.payload.data.decode("UTF-8")
    creds_dict = json.loads(secret_payload)
    return Credentials.from_authorized_user_info(creds_dict)

def get_youtube_client(secret_name: str):
    creds = get_credentials(secret_name)
    youtube = build("youtube", "v3", credentials=creds)
    return youtube

# ---------------------------
# Fact sources
# ---------------------------
def get_wikipedia_featured():
    dt = datetime.utcnow()
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{dt.year}/{dt.month:02d}/{dt.day:02d}"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        tfa = data.get("tfa")
        if tfa and "extract" in tfa:
            return f"Did you know? {tfa['extract']}"
        mr = data.get("mostread", {}).get("articles", [])
        if mr:
            fact = mr[0].get("extract") or mr[0].get("title")
            return f"Did you know? {fact}"
    except Exception as e:
        print("Wikipedia fetch failed:", e)
    return "Did you know honey never spoils?"

def call_gemini(prompt: str):
    aiplatform.init(project=PROJECT_ID, location=REGION)
    model = aiplatform.TextGenerationModel.from_pretrained("gemini-1.5-flash")
    response = model.predict(prompt)
    return response.text

def get_gemini_fact(category: str):
    prompt = f"Give me one surprising 'Did you know?' fact about {category}, in one or two sentences."
    try:
        return call_gemini(prompt).strip()
    except Exception as e:
        print("Gemini call failed:", e)
        return ""

def get_fact():
    choice = random.choice([1, 2, 3, 4])
    if choice == 1:
        return get_wikipedia_featured()
    elif choice == 2:
        return get_gemini_fact("technology")
    elif choice == 3:
        return get_gemini_fact("science, history, or culture")
    else:
        return get_gemini_fact("trending news")

# ---------------------------
# TTS + Video Generation
# ---------------------------
def create_trivia_video(fact: str, background_gcs_path: str, output_gcs_path: str):
    storage_client = storage.Client()

    # --- Download background ---
    bg_bucket_name, bg_blob_name = background_gcs_path.replace("gs://", "").split("/", 1)
    out_bucket_name, out_blob_name = output_gcs_path.replace("gs://", "").split("/", 1)
    bg_bucket = storage_client.bucket(bg_bucket_name)
    bg_blob = bg_bucket.blob(bg_blob_name)
    tmp_bg = "/tmp/background.jpg"
    bg_blob.download_to_filename(tmp_bg)

    # --- Generate TTS ---
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    tmp_audio = "/tmp/audio.mp3"
    with open(tmp_audio, "wb") as out:
        out.write(response.audio_content)

    # --- Prepare text overlay image ---
    img = Image.open(tmp_bg).convert("RGB")
    draw = ImageDraw.Draw(img)
    max_width, max_height = img.width * 0.9, img.height * 0.9
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    fontsize = 80
    min_fontsize = 20
    wrapped = textwrap.wrap(fact, width=25)
    
    while fontsize >= min_fontsize:
        font = ImageFont.truetype(font_path, fontsize)
        text_w = max(draw.textlength(line, font=font) for line in wrapped)
        text_h = sum(font.getsize(line)[1] for line in wrapped) + 10 * (len(wrapped)-1)
        if text_w <= max_width and text_h <= max_height:
            break
        fontsize -= 4
    # Draw text centered
    y_offset = (img.height - text_h) / 2
    for line in wrapped:
        line_w = draw.textlength(line, font=font)
        x = (img.width - line_w) / 2
        draw.text((x, y_offset), line, font=font, fill="white")
        y_offset += font.getsize(line)[1] + 10
    tmp_text_img = "/tmp/text_img.jpg"
    img.save(tmp_text_img)

    # --- Combine image + audio with FFmpeg ---
    tmp_out = "/tmp/output.mp4"
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", tmp_text_img,
        "-i", tmp_audio,
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-shortest", tmp_out
    ]
    subprocess.run(ffmpeg_cmd, check=True)

    # --- Upload to GCS ---
    out_bucket = storage_client.bucket(out_bucket_name)
    out_blob = out_bucket.blob(out_blob_name)
    out_blob.upload_from_filename(tmp_out)
    return output_gcs_path

# ---------------------------
# YouTube upload
# ---------------------------
def upload_to_youtube(video_gcs_path: str, title: str, description: str, secret_name: str, category_id: str = "24"):
    youtube = get_youtube_client(secret_name)
    bucket_name, blob_name = video_gcs_path.replace("gs://", "").split("/", 1)
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    tmp = f"/tmp/{os.path.basename(blob_name)}"
    blob.download_to_filename(tmp)

    media_body = MediaFileUpload(tmp, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title, "description": description, "categoryId": category_id},
            "status": {"privacyStatus": "public"}
        },
        media_body=media_body,
    )
    resp = request.execute()
    return resp.get("id")

# ---------------------------
# Main HTTP /generate
# ---------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    fact = get_fact()
    ts = int(random.random() * 1e9)
    background_gcs_path = f"gs://{OUTPUT_BUCKET}/background.jpg"
    output_gcs_path = f"gs://{OUTPUT_BUCKET}/fact_{ts}.mp4"

    video_path = create_trivia_video(fact, background_gcs_path, output_gcs_path)

    title = fact[:90]
    description = fact
    video_id = upload_to_youtube(video_path, title, description, YT_SECRET)

    return jsonify({
        "fact": fact,
        "youtube_id": video_id,
        "video_gcs": video_path
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
