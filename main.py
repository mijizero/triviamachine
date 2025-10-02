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
import textwrap

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
def compress_fact_with_gemini(fact: str, max_chars: int = 60) -> str:
    try:
        client = aiplatform.gapic.PredictionServiceClient()
        endpoint = client.endpoint_path(
            project=PROJECT_ID, location=REGION, endpoint="gemini-2.5-flash"
        )
        prompt = f"Summarize this fact into one concise sentence under {max_chars} characters: {fact}"
        response = client.predict(
            endpoint=endpoint,
            instances=[{"content": prompt}],
            parameters={}
        )
        summary = response.predictions[0].get("content", "").strip()
        if summary:
            return summary[:max_chars]
    except Exception as e:
        print("Gemini compression failed, falling back:", e)
    return fact[:max_chars-3] + "..."

def summarize_fact(fact: str, max_chars: int = 60) -> str:
    if len(fact) <= max_chars:
        return fact
    return compress_fact_with_gemini(fact, max_chars)

def create_trivia_video(fact: str, background_gcs_path: str, output_gcs_path: str):
    storage_client = storage.Client()

    # --- Parse GCS paths ---
    bg_bucket_name, bg_blob_name = background_gcs_path.replace("gs://", "").split("/", 1)
    out_bucket_name, out_blob_name = output_gcs_path.replace("gs://", "").split("/", 1)

    # --- Download background ---
    tmp_bg = "/tmp/background.jpg"
    bg_bucket = storage_client.bucket(bg_bucket_name)
    bg_blob = bg_bucket.blob(bg_blob_name)
    bg_blob.download_to_filename(tmp_bg)

    # --- Summarize fact if >60 chars ---
    fact = summarize_fact(fact, 60)

    # --- Wrap text for multi-line display ---
    max_line_chars = 30
    lines = textwrap.wrap(fact, width=max_line_chars)
    fact_wrapped = "\n".join(lines)

    tmp_fact = "/tmp/fact.txt"
    with open(tmp_fact, "w") as f:
        f.write(fact_wrapped)

    # --- TTS WAV (LINEAR16) to avoid MP3 misdetection ---
    tts_client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=fact)
    voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Neural2-C")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.LINEAR16)
    response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)

    tmp_audio = "/tmp/audio.wav"
    with open(tmp_audio, "wb") as out:
        out.write(response.audio_content)

    # --- Dynamic font sizing ---
    fontfile = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    max_width = 720 * 0.9
    max_height = 1280 * 0.9
    fontsize = 80
    min_fontsize = 20

    # Measure text dimensions using PIL for reliable sizing
    from PIL import ImageFont, ImageDraw, Image
    while fontsize >= min_fontsize:
        font = ImageFont.truetype(fontfile, fontsize)
        img = Image.new("RGB", (720, 1280))
        draw = ImageDraw.Draw(img)
        text_w, text_h = draw.multiline_textsize(fact_wrapped, font=font, spacing=10)
        if text_w <= max_width and text_h <= max_height:
            break
        fontsize -= 2

    # --- Render final video ---
    tmp_out = "/tmp/output.mp4"
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", tmp_bg,
        "-i", tmp_audio,
        "-vf", (
            f"scale=720:1280,"
            f"drawtext=fontfile={fontfile}:textfile={tmp_fact}:fontsize={fontsize}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=10:"
            f"fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=10"
        ),
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

    # Fixed background and output bucket
    background_gcs_path = f"gs://{OUTPUT_BUCKET}/background.jpg"
    output_gcs_path = f"gs://{OUTPUT_BUCKET}/fact_{ts}.mp4"

    # --- Create video ---
    video_path = create_trivia_video(
        fact=fact,
        background_gcs_path=background_gcs_path,
        output_gcs_path=output_gcs_path
    )

    # --- YouTube metadata ---
    title = fact[:90]
    description = fact

    # --- Upload to YouTube ---
    video_id = upload_to_youtube(
        video_gcs_path=video_path,
        title=title,
        description=description,
        secret_name=YT_SECRET
    )

    return jsonify({
        "fact": fact,
        "youtube_id": video_id,
        "video_gcs": video_path
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
