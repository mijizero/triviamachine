import os
import random
import json
import requests
from flask import Flask, jsonify
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.cloud import aiplatform  # for calling Gemini
# (Or the appropriate Gemini client from google cloud generating AI)

app = Flask(__name__)

PROJECT_ID = "trivia-machine-472207"
REGION = "asia-southeast1"  # adjust if needed

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
    """
    Use Wikimedia Feed API to get today's featured article.  
    e.g. GET https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{year}/{month}/{day}
    """
    from datetime import datetime
    dt = datetime.utcnow()
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{dt.year}/{dt.month:02d}/{dt.day:02d}"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        # The “most read” or “tfa” sections have content
        tfa = data.get("tfa")
        if tfa and "normalizedtitle" in tfa and "extract" in tfa:
            fact = tfa["extract"]
            return f"Did you know? {fact}"
        # fallback: pick first article from “mostread”
        mr = data.get("mostread", {}).get("articles", [])
        if mr:
            fact = mr[0].get("extract") or mr[0].get("title")
            return f"Did you know? {fact}"
    except Exception as e:
        print("Wikipedia fetch failed:", e)
    # fallback static
    return "Did you know honey never spoils?"

def call_gemini(prompt: str):
    """
    Calls Gemini via Vertex AI to generate a short fact string.
    Returns prompt -> text. Adjust according to your SDK.
    """
    # Example with aiplatform; adapt based on your setup
    client = aiplatform.gapic.PredictionServiceClient()
    endpoint = client.endpoint_path(project=PROJECT_ID, location=REGION, endpoint="gemini-2.5-flash")
    response = client.predict(
        endpoint=endpoint,
        instances=[{"content": prompt}],
        parameters={}
    )
    # Assume response.predictions[0]["content"] has the generated string
    return response.predictions[0].get("content", "")

def get_gemini_fact(category: str):
    """
    Wrapper to ask Gemini for different categories of facts.
    """
    prompt = f"Give me one surprising 'Did you know?' fact about {category}, in one or two sentences."
    try:
        ans = call_gemini(prompt)
        # Clean up quotes/backticks etc.
        return ans.strip()
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
# Video + TTS placeholders
# ---------------------------
def create_trivia_video(fact: str, background_gcs_path: str, output_gcs_path: str):
    """
    Your existing function to create a video from the fact string + TTS + visuals.
    Use this signature. Return the GCS path or local path.
    """
    # (You reuse your old create_trivia_video logic, adapting for fact instead of question + choices)
    raise NotImplementedError("Hook in your video generation logic here")

# ---------------------------
# YouTube upload
# ---------------------------
def upload_to_youtube(video_gcs_path: str, title: str, description: str, secret_name: str, category_id: str = "24"):
    youtube = get_youtube_client(secret_name)
    # download video from GCS to local
    bucket_name, blob_name = video_gcs_path.replace("gs://", "").split("/", 1)
    from google.cloud import storage
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    tmp = f"/tmp/{blob_name}"
    blob.download_to_filename(tmp)

    media_body = None
    from googleapiclient.http import MediaFileUpload
    media_body = MediaFileUpload(tmp, chunksize=-1, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": category_id
            },
            "status": {"privacyStatus": "public"}
        },
        media_body=media_body,
    )
    resp = request.execute()
    video_id = resp.get("id")
    print("Uploaded to YouTube, video id:", video_id)
    return video_id

# ---------------------------
# Main HTTP /generate endpoint
# ---------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    fact = get_fact()
    print("Chosen fact:", fact)

    ts = int(random.random() * 1e9)  # or timestamp
    output_gcs = f"gs://trivia-videos-output/fact_{ts}.mp4"
    bg = "gs://trivia-videos-output/background.jpg"

    video_path = create_trivia_video(
        fact=fact,
        background_gcs_path=bg,
        output_gcs_path=output_gcs,
    )

    title = fact[:90]
    description = fact

    # Use first channel or secret name
    video_id = upload_to_youtube(
        video_gcs_path=output_gcs,
        title=title,
        description=description,
        secret_name="youtube-channel-1-creds",
        category_id="24"
    )

    return jsonify({
        "fact": fact,
        "youtube_id": video_id,
        "video_gcs": output_gcs
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
