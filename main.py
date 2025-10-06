import os
import tempfile
import json
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
# Vertex AI Init
# -------------------------------
vertexai.init(project="trivia-machine-472207", location="asia-southeast1")

# -------------------------------
# Dynamic Fact
# -------------------------------
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
    elif source == 2:
        prompt = (
            "Give one factual and engaging piece of technology trivia in 3 sentences. "
            "Sentence 1 must start with 'Did you know'. "
            "Sentences 2 and 3 should add interesting details or background."
        )
        return gemini_fact(prompt)
    elif source == 3:
        prompt = (
            "Give one true and engaging trivia about science, history, or culture in 3 sentences. "
            "Start with 'Did you know', then add 2 supporting sentences with factual context or significance."
        )
        return gemini_fact(prompt)
    elif source == 4:
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
# Gemini Helpers
# -------------------------------
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
    if gcs_path.endswith("/"):
        gcs_path += "output.mp4"  # force filename if empty
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    gs_url = f"gs://{bucket_name}/{blob_path}"
    https_url = f"https://storage.googleapis.com/{bucket_name}/{blob_path}"
    return gs_url, https_url

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
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    with open(output_path, "wb") as out:
        out.write(response.audio_content)

# -------------------------------
# YouTube Upload Helpers
# -------------------------------
def get_youtube_creds_from_secret():
    client = secretmanager.SecretManagerServiceClient()
    secret_name = "Credentials_Trivia"
    project_id = "trivia-machine-472207"
    response = client.access_secret_version(
        name=f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    )
    creds_json = response.payload.data.decode("UTF-8")
    return Credentials.from_authorized_user_info(json.loads(creds_json))

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

import re

def sanitize_for_youtube(text, max_len=100):
    if not text:
        return ""
    text = re.sub(r"[\x00-\x1F\x7F]", "", text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0]
    return text

def upload_video_to_youtube_gcs(gcs_path, title, description, category, tags=None, privacy="public"):
    try:
        if not gcs_path.startswith("gs://"):
            raise ValueError(f"Invalid GCS path: {gcs_path}")

        bucket_name, blob_name = gcs_path[5:].split("/", 1)
        print("Bucket:", bucket_name, "Blob:", blob_name)

        creds = get_youtube_creds_from_secret()
        youtube = build("youtube", "v3", credentials=creds)

        # Download video locally
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            blob.download_to_filename(tmp.name)
            tmp_path = tmp.name

        media_body = MediaFileUpload(tmp_path, chunksize=-1, resumable=True)
        title_safe = sanitize_for_youtube(title, max_len=100)
        description_safe = sanitize_for_youtube(description, max_len=5000)

        category_map = {
            "pop culture": "24",
            "sports": "17",
            "history": "22",
            "science": "28",
            "tech": "28",
        }
        category_id = category_map.get(category.lower(), "24")
        playlist_id = PLAYLIST_MAP.get(category.lower(), PLAYLIST_MAP["pop culture"])

        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title_safe,
                    "description": description_safe,
                    "tags": tags or ["trivia", "quiz", "fun"],
                    "categoryId": category_id
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

        video_id = response["id"]
        print("Video uploaded. ID:", video_id)

        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id}
                }
            }
        ).execute()

        os.remove(tmp_path)
        return video_id

    except Exception as e:
        print("ERROR in YouTube upload:", str(e))
        raise

# -------------------------------
# Create Trivia Video
# -------------------------------
def create_trivia_video(fact_text, output_gcs_path="gs://trivia-videos-output/output.mp4"):
    with tempfile.TemporaryDirectory() as tmpdir:
        search_query = extract_search_query(fact_text)
        bg_path = os.path.join(tmpdir, "background.jpg")
        valid_image = False
        img_url = None

        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(search_query, max_results=1))
            if results:
                img_url = results[0].get("image")
                if img_url:
                    response = requests.get(img_url, stream=True, timeout=10)
                    if response.status_code == 200 and "image" in response.headers.get("Content-Type", ""):
                        with open(bg_path, "wb") as f:
                            for chunk in response.iter_content(8192):
                                f.write(chunk)
                        valid_image = True
        except Exception:
            pass

        if not valid_image:
            fallback_url = "https://storage.googleapis.com/trivia-videos-output/background.jpg"
            response = requests.get(fallback_url)
            with open(bg_path, "wb") as f:
                f.write(response.content)

        # Resize/crop 1080x1920
        target_size = (1080, 1920)
        img = Image.open(bg_path).convert("RGB")
        img_ratio = img.width / img.height
        target_ratio = target_size[0] / target_size[1]
        if img_ratio > target_ratio:
            new_width = int(img.height * target_ratio)
            left = (img.width - new_width) // 2
            right = left + new_width
            img = img.crop((left, 0, right, img.height))
        else:
            new_height = int(img.width / target_ratio)
            top = (img.height - new_height) // 2
            bottom = top + new_height
            img = img.crop((0, top, img.width, bottom))
        img = img.resize(target_size, Image.LANCZOS)
        bg_path = os.path.join(tmpdir, "background_resized.jpg")
        img.save(bg_path)

        # TTS
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Text overlay
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 45)
        x_margin = int(img.width * 0.1)
        max_width = int(img.width * 0.8)

        words = fact_text.split()
        lines = []
        current_line = []
        for word in words:
            test_line = " ".join(current_line + [word])
            bbox = draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_width:
                current_line.append(word)
            else:
                lines.append(" ".join(current_line))
                current_line = [word]
        if current_line:
            lines.append(" ".join(current_line))

        pages = []
        for i in range(0, len(lines), 2):
            page_text = "\n".join(lines[i:i + 2])
            pages.append(page_text)

        def estimate_read_time(text):
            words = len(text.split())
            commas = text.count(",")
            periods = text.count(".")
            total_words = words + commas + periods
            return max(total_words / 2.0, 2)

        clips = []
        for page_text in pages:
            img_page = img.copy()
            draw_page = ImageDraw.Draw(img_page)
            y_text = 100
            for line in page_text.split("\n"):
                bbox = draw_page.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                x_text = (img_page.width - line_width) // 2
                draw_page.text((x_text, y_text), line, font=font, fill="white")
                y_text += line_height + 20
            page_path = os.path.join(tmpdir, f"page_{pages.index(page_text)}.jpg")
            img_page.save(page_path)
            clip_duration = estimate_read_time(page_text)
            clip = ImageClip(page_path).set_duration(clip_duration)
            clips.append(clip)

        video = concatenate_videoclips(clips)
        video = video.set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "trivia_video.mp4")
        video.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)

        # Upload to GCS
        gs_url, https_url = upload_to_gcs(output_path, output_gcs_path)
        return gs_url, https_url

# -------------------------------
# Flask Endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        fact = data.get("fact") or get_unique_fact()
        category = data.get("category") or infer_category_from_fact(fact)

        safe_fact = sanitize_for_youtube(fact, max_len=5000)
        output_gcs_path = "gs://trivia-videos-output/output.mp4"

        video_gs_url, video_https_url = create_trivia_video(fact, output_gcs_path)

        title_options = [
            "Did you know?", "Trivia Time!", "Quick Fun Fact!", "Can You Guess This?",
            "Learn Something!", "Well Who Knew?", "Wow Really?", "Fun Fact Alert!",
            "Now You Know!", "Not Bad!", "Mind-Blowing Fact!"
        ]
        youtube_title = sanitize_for_youtube(random.choice(title_options), max_len=100)
        youtube_description = safe_fact + " Did you get it right? What do you think of the fun fact? Now you know! See you at the comments!"

        video_id = upload_video_to_youtube_gcs(
            video_gs_url, youtube_title, youtube_description, category
        )

        return jsonify({
            "status": "ok",
            "fact": fact,
            "video_gcs": video_https_url,
            "youtube_video_id": video_id
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
