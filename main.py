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
import subprocess

app = Flask(__name__)

# ---------------------------
# CONFIG
# ---------------------------
PROJECT_ID = "trivia-machine-472207"
REGION = "asia-southeast1"
OUTPUT_BUCKET = "trivia-videos-output"
YT_SECRET = "youtube-channel-1-creds"

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

    # download background
    bucket = storage_client.bucket(OUTPUT_BUCKET)
    blob = bucket.blob("background.jpg")
    tmp_bg = "/tmp/background.jpg"
    blob.download_to_filename(tmp_bg)

    # synthesize TTS
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-C"
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )
    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )
    tmp_audio = "/tmp/audio.mp3"
    with open(tmp_audio, "wb") as out:
        out.write(response.audio_content)

    # limit text length to 60 chars
    if len(fact) > 60:
        fact = fact[:57] + "..."

    # adjust font size dynamically
    if len(fact) <= 30:
        font_size = 48
    elif len(fact) <= 45:
        font_size = 40
    else:
        font_size = 32

    # write fact text into a file (UTF-8 safe)
    fact_file = "/tmp/fact.txt"
    with open(fact_file, "w", encoding="utf-8") as f:
        f.write(fact)

    tmp_out = "/tmp/output.mp4"

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", tmp_bg,
        "-i", tmp_audio,
        "-vf", (
            "scale=720:1280,"
            "drawtext="
            f"fontcolor=white:fontsize={font_size}:x=(w-text_w)/2:y=h-100:"
            "box=1:boxcolor=black@0.5:"
            f"textfile={fact_file}:"
            "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            "reload=1"
        ),
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-shortest",
        tmp_out
    ]

    subprocess.run(ffmpeg_cmd, check=True)

    # upload to GCS
    out_blob = bucket.blob(output_gcs_path.replace(f"gs://{OUTPUT_BUCKET}/", ""))
    out_blob.upload_from_filename(tmp_out)
    return f"gs://{OUTPUT_BUCKET}/{output_gcs_path.replace(f'gs://{OUTPUT_BUCKET}/', '')}"
    
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
    output_gcs = f"fact_{ts}.mp4"
    bg = f"gs://{OUTPUT_BUCKET}/background.jpg"

    video_path = create_trivia_video(fact=fact, background_gcs_path=bg, output_gcs_path=f"gs://{OUTPUT_BUCKET}/{output_gcs}")
    title = fact[:90]
    description = fact
    video_id = upload_to_youtube(video_gcs_path=video_path, title=title, description=description, secret_name=YT_SECRET)

    return jsonify({"fact": fact, "youtube_id": video_id, "video_gcs": video_path})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
