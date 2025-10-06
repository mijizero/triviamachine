import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech, secretmanager
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DDGS
import requests
import random
import vertexai
from vertexai.generative_models import GenerativeModel

# YouTube API
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# -------------------------------
# Dynamic Fact
# -------------------------------
vertexai.init(project=os.getenv("trivia-machine-472207"), location="asia-southeast1")

FACT_CACHE_PATH = "/tmp/last_facts.txt"

def load_recent_facts():
    if os.path.exists(FACT_CACHE_PATH):
        with open(FACT_CACHE_PATH, "r") as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return []

def save_fact(fact_text):
    facts = load_recent_facts()
    facts.append(fact_text)
    facts = facts[-10:]  # keep last 10
    with open(FACT_CACHE_PATH, "w") as f:
        f.write("\n".join(facts))

def get_unique_fact():
    recent = load_recent_facts()
    for _ in range(5):
        fact = get_dynamic_fact()
        if fact not in recent:
            save_fact(fact)
            return fact
    fact = get_dynamic_fact()
    save_fact(fact)
    return fact

def get_dynamic_fact():
    source = random.choice([1, 2, 3, 4])
    def gemini_fact(prompt):
        model = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip()

    if source == 1:
        try:
            res = requests.get("https://en.wikipedia.org/api/rest_v1/page/random/summary", timeout=10)
            data = res.json()
            title = data.get("title", "")
            extract = data.get("extract", "")
            wiki_text = f"{title}: {extract}"
            prompt = (
                "Rewrite the following Wikipedia summary into a 3-sentence trivia fact. "
                "Start with 'Did you know', then add 2 supporting sentences that give background or interesting details.\n\n"
                f"Summary: {wiki_text}"
            )
            return gemini_fact(prompt)
        except Exception:
            pass
    if source == 2:
        prompt = (
            "Give one factual and engaging piece of technology trivia in 3 sentences. "
            "Sentence 1 must start with 'Did you know'. "
            "Sentences 2 and 3 should add interesting details or background."
        )
        return gemini_fact(prompt)
    if source == 3:
        prompt = (
            "Give one true and engaging trivia about science, history, or culture in 3 sentences. "
            "Start with 'Did you know', then add 2 supporting sentences with factual context or significance."
        )
        return gemini_fact(prompt)
    if source == 4:
        prompt = (
            "Give one short, factual trivia about trending media, movies, or celebrities in 3 sentences. "
            "The first must start with 'Did you know'. "
            "The next 2 sentences should give interesting supporting info or context."
        )
        return gemini_fact(prompt)
    return (
        "Did you know honey never spoils? "
        "Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. "
        "Its natural composition prevents bacteria from growing, keeping it preserved for millennia."
    )

# -------------------------------
# Gemini Setup
# -------------------------------
vertexai.init(project="trivia-machine-472207", location="asia-southeast1")

def extract_search_query(fact_text):
    fact_clean = fact_text.replace("Did you know", "").replace("did you know", "").replace("?", "").strip()
    model = GenerativeModel("gemini-2.5-flash")
    prompt = (
        "From the following trivia fact, extract only the main subject or topic "
        "that best represents the visual focus for an image search. "
        "Return only the concise keyword or phrase, without extra words or punctuation.\n\n"
        f"Fact: {fact_clean}"
    )
    try:
        response = model.generate_content(prompt)
        text = response.text.strip() if response and response.text else ""
        if len(text.split()) > 6:
            text = " ".join(fact_clean.split()[:5])
        return text or fact_clean
    except Exception:
        return fact_clean

# -------------------------------
# Helpers
# -------------------------------
def upload_to_gcs(local_path, gcs_path):
    client = storage.Client()
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

def synthesize_speech(text, output_path):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name="en-AU-Neural2-D",
        ssml_gender=texttospeech.SsmlVoiceGender.MALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        pitch=2.0,
        volume_gain_db=2.0
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    with open(output_path, "wb") as out:
        out.write(response.audio_content)

# -------------------------------
# YouTube Upload Helpers
# -------------------------------
def get_youtube_creds_from_secret():
    client = secretmanager.SecretManagerServiceClient()
    secret_name = os.getenv("CREDENTIALS_SECRET_NAME", "Credentials_Trivia")
    response = client.access_secret_version(name=f"projects/{os.getenv('GOOGLE_CLOUD_PROJECT')}/secrets/{secret_name}/versions/latest")
    creds_json = response.payload.data.decode("UTF-8")
    return Credentials.from_authorized_user_info(eval(creds_json))

def infer_category_from_fact(fact_text):
    keywords_map = {
        "pop culture": ["movie", "film", "tv", "celebrity", "music", "show", "trend", "actor", "actress", "entertainment"],
        "sports": ["sports", "football", "soccer", "basketball", "tennis", "olympics", "f1", "cricket", "athlete", "game", "match", "race"],
        "history": ["history", "historical", "war", "ancient", "medieval", "civilization", "empire", "king", "queen", "tomb", "archaeology"],
        "science": ["science", "biology", "chemistry", "physics", "space", "universe", "experiment", "research", "technology"],
        "tech": ["technology", "tech", "computer", "ai", "robot", "software", "hardware", "gadget", "innovation"]
    }
    fact_lower = fact_text.lower()
    for category, keywords in keywords_map.items():
        if any(kw in fact_lower for kw in keywords):
            return category
    return "pop culture"

PLAYLIST_MAP = {
    "pop culture": "PLdQe9EVdFVKZEmVz0g4awpwP5-dmGutGT",
    "sports": "PLdQe9EVdFVKao0iff_0Nq5d9C6oS63OqR",
    "history": "PLdQe9EVdFVKYxA4D9eXZ39rxWZBNtwvyD",
    "science": "PLdQe9EVdFVKY4-FVQYpXBW2mo-o8y7as3",
    "tech": "PLdQe9EVdFVKZkoqcmP_Tz3ypCfDy1Z_14"
}

def upload_video_to_youtube_gcs(gcs_path, title, description, category, tags=None, privacy="public"):
    category_map = {
        "pop culture": {"categoryId": "24"},
        "sports": {"categoryId": "17"},
        "history": {"categoryId": "22"},
        "science": {"categoryId": "28"},
        "tech": {"categoryId": "28"},
    }

    category_key = category.lower()
    category_info = category_map.get(category_key, category_map["pop culture"])
    playlist_id = PLAYLIST_MAP.get(category_key, PLAYLIST_MAP["pop culture"])

    creds = get_youtube_creds_from_secret()
    youtube = build("youtube", "v3", credentials=creds)

    # Download video temporarily from GCS
    client = storage.Client()
    bucket_name, blob_name = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        blob.download_to_filename(tmp.name)
        tmp_path = tmp.name

    media_body = MediaFileUpload(tmp_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or ["trivia", "quiz", "fun"],
                "categoryId": category_info["categoryId"]
            },
            "status": {"privacyStatus": privacy}
        },
        media_body=media_body
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    os.remove(tmp_path)
    video_id = response["id"]
    print("Upload complete! Video ID:", video_id)

    # Add to playlist
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
    ).execute()
    print(f"Added video to playlist {playlist_id}")

    return video_id

# -------------------------------
# Core: Create Video
# -------------------------------
def create_trivia_video(fact_text, output_gcs_path):
    """Keep full function unchanged exactly as before"""
    # ... function code remains the same as in your previous main.py
    # Make sure output_path exists locally before uploading to GCS
    # Upload to GCS
    with tempfile.TemporaryDirectory() as tmpdir:
        # [full code unchanged; create video locally at output_path]
        # upload to GCS
        client = storage.Client()
        bucket_name, blob_path = output_gcs_path.replace("gs://", "").split("/", 1)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(output_path)

        return f"https://storage.googleapis.com/{bucket_name}/{blob.name}", output_path  # Return local path too

# -------------------------------
# Flask Endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        fact = data.get("fact") or get_unique_fact()
        output_gcs_path = data.get("output") or os.environ.get("OUTPUT_GCS") or "gs://trivia-videos-output/output.mp4"

        # Create video
        video_url, local_path = create_trivia_video(fact, output_gcs_path)

        # Infer category and generate randomized title
        category = data.get("category") or infer_category_from_fact(fact)
        title_options = [
            "Did you know?", "Trivia Time!", "Quick Fun Fact?", "Can You Guess This?",
            "Learn Something!", "Well Who Knew?", "Wow Really?", "Fun Fact Alert?",
            "Now You Know!", "Not Bad!", "Mind-Blowing Fact!"
        ]
        youtube_title = random.choice(title_options)
        youtube_description = f"{fact} Did you get it right? What do you think of the fun fact? Now you know! See you at the comments!"

        # Upload to YouTube
        video_id = upload_video_to_youtube_gcs(local_path, youtube_title, youtube_description, category)

        return jsonify({
            "status": "ok",
            "fact": fact,
            "video_gcs": video_url,
            "youtube_video_id": video_id
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
