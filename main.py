import os
import tempfile
import json
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech, secretmanager
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, concatenate_audioclips
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DDGS
import requests
import random
import vertexai
from vertexai.generative_models import GenerativeModel
from aeneas.executetask import ExecuteTask
from aeneas.task import Task

from vertexai.preview import generative_models
import re
import time
from vertexai.preview.vision_models import ImageGenerationModel

import hashlib
from difflib import SequenceMatcher

# YouTube API
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.cloud import firestore

app = Flask(__name__)

# -------------------------------
# Vertex AI Init
# -------------------------------
vertexai.init(project="trivia-machine-472207", location="asia-southeast1")

# -------------------------------
# Dynamic Fact (Firestore version)
# -------------------------------
from google.cloud import firestore

# Force Firestore client to use correct project
firestore_client = firestore.Client(project="trivia-machine-472207", database="(default)")

db = firestore_client
FACTS_COLLECTION = "facts_history"

_seen_facts = set()
_checked_firestore = False  # ensures Firestore is loaded only once per runtime

def get_secret(secret_name):
    client = secretmanager.SecretManagerServiceClient()
    project_id = "trivia-machine-472207"
    response = client.access_secret_version(
        name=f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    )
    return response.payload.data.decode("UTF-8")

def normalize_fact(text: str) -> str:
    """Normalize text for consistent duplicate checking."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    words = sorted(text.split())
    return " ".join(words)


def load_seen_facts_from_firestore():
    """Load all previously used facts from Firestore into memory once."""
    global _checked_firestore
    if _checked_firestore:
        return
    _checked_firestore = True
    try:
        docs = db.collection(FACTS_COLLECTION).stream()
        for doc in docs:
            normalized = doc.get("normalized")
            if normalized:
                _seen_facts.add(normalized)
        print(f"✅ Loaded {len(_seen_facts)} facts from Firestore history.")
    except Exception as e:
        print(f"⚠️ Could not load facts from Firestore: {e}")


def save_fact_to_firestore(fact: str):
    """Save a new fact to Firestore and export the collection to GCS JSON.
    Fixes DatetimeWithNanoseconds serialization.
    """
    from google.cloud import firestore
    import datetime, json, tempfile, os
    from google.cloud import storage

    normalized = normalize_fact(fact)
    try:
        # --- 1. Save to Firestore ---
        db.collection(FACTS_COLLECTION).add({
            "fact": fact,
            "normalized": normalized,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        _seen_facts.add(normalized)

        # --- 2. Gather all facts ---
        all_facts = []
        docs = db.collection(FACTS_COLLECTION).stream()
        for d in docs:
            data = d.to_dict() or {}
            data["id"] = d.id
            all_facts.append(data)

        # --- 3. Convert non-serializable types (timestamps, etc.) ---
        def default_converter(o):
            if isinstance(o, datetime.datetime):
                return o.isoformat()
            return str(o)

        # --- 4. Write temp JSON ---
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as tmp_json:
            json.dump(all_facts, tmp_json, indent=2, ensure_ascii=False, default=default_converter)
            tmp_json_path = tmp_json.name

        # --- 5. Upload to GCS ---
        bucket_name = "trivia-videos-output"
        json_blob_path = "facts_history.json"
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(json_blob_path)
        blob.upload_from_filename(tmp_json_path, content_type="application/json")

        https_url = f"https://storage.googleapis.com/{bucket_name}/{json_blob_path}"
        print(f"✅ Exported Firestore facts to {https_url}")

        # --- 6. Clean local temp ---
        try:
            os.remove(tmp_json_path)
        except Exception as e_rm:
            print(f"⚠️ Could not remove local temp JSON: {e_rm}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"⚠️ Could not save/export fact to Firestore/GCS: {e}")


def is_duplicate_fact(fact: str, threshold: float = 0.88) -> bool:
    """
    Detect duplicates or near-duplicates using normalization and fuzzy similarity.
    Returns True if the fact already exists or is too similar to an existing one.
    """
    load_seen_facts_from_firestore()
    normalized = normalize_fact(fact)

    # Quick exact match check
    if normalized in _seen_facts:
        return True

    # Fuzzy similarity check for reworded duplicates
    for existing in _seen_facts:
        ratio = SequenceMatcher(None, normalized, existing).ratio()
        if ratio > threshold:
            return True

    # If passed both checks → mark as new fact
    _seen_facts.add(normalized)
    save_fact_to_firestore(fact)
    return False


def load_recent_facts(limit=10):
    try:
        docs = db.collection(FACTS_COLLECTION) \
            .order_by("timestamp", direction=firestore.Query.DESCENDING) \
            .limit(limit).stream()
        return [d.get("fact") for d in docs if d.get("fact")]
    except Exception as e:
        print("Error loading facts from Firestore:", str(e))
        return []

def get_unique_fact(ytdest):
    recent = load_recent_facts()
    for _ in range(5):
        # choose generator based on destination
        if ytdest == 'kk':
            fact, source_code = get_dynamic_fact_JINJA()
        else:
            fact, source_code = get_dynamic_fact()

        if not is_duplicate_fact(fact) and fact not in recent:
            save_fact_to_firestore(fact)
            print(f"get_unique_fact: selected fact for {ytdest} from source {source_code}")
            return fact, source_code

    # fallback if all attempts failed
    if ytdest == 'tech':
        fact, source_code = get_dynamic_fact()
    elif ytdest == 'kk':
        fact, source_code = get_dynamic_fact_JINJA()
    else:
        fact, source_code = get_dynamic_fact()

    save_fact_to_firestore(fact)
    print(f"get_unique_fact: fallback fact for {ytdest} from source {source_code}")
    return fact, source_code

def get_dynamic_fact():
    """Try the 2 sources in random order and return (fact_text, source_label).
    If every source attempt fails, return the honey fallback with source 'Z'."""
    sources = [1, 2]
    random.shuffle(sources)
    source_label_map = {1: "A", 2: "B"}
    json_firestore = "https://storage.googleapis.com/trivia-videos-output/facts_history.json"

    def gemini_fact(prompt):
        model = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip() if response and getattr(response, "text", None) else ""

    # Try each source once in a random order
    for source in sources:
        label = source_label_map[source]
        try:

            if source == 1:
                prompt = (
                    "Generate a new interesting fact that naturally fits within the theme of technology and innovation. "
                    "Overall 3 sentences. Sentence 1 must start with 'Did you know'. "
                    "Sentences 2 and 3 should add interesting details or background."
                    "Do not focus narrowly on definitions or lists; instead, vary concepts, examples, and perspectives. "
                    "Avoid repeating any main ideas found in the JSON file at " + json_firestore
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

            elif source == 2:
                prompt = (
                    "Generate a new interesting fact that naturally fits within the theme of how tech gadgets or everyday products work. "
                    "Overall 3 sentences. Sentence 1 must start with 'Did you know'. "
                    "Sentences 2 and 3 should add interesting details or background."
                    "Do not focus narrowly on definitions or lists; instead, vary concepts, examples, and perspectives. "
                    "Avoid repeating any main ideas found in the JSON file at " + json_firestore
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

        except Exception as e:
            # don't raise — try the next source
            print(f"get_dynamic_fact(): source {source} attempt failed: {e}")
            continue

    # If all sources failed, return honey fallback
    honey = (
        "Did you know honey never spoils? "
        "Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. "
        "Its natural composition prevents bacteria from growing, keeping it preserved for millennia."
    )
    return honey, "Z"

def get_dynamic_fact_JINJA():
    """Try the 2 sources in random order and return (fact_text, source_label).
    If every source attempt fails, return the honey fallback with source 'Z'."""
    sources = [1, 2]
    random.shuffle(sources)
    source_label_map = {1: "A", 2: "B"}
    json_firestore = "https://storage.googleapis.com/trivia-videos-output/facts_history.json"

    def gemini_fact(prompt):
        model = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip() if response and getattr(response, "text", None) else ""

    # Try each source once in a random order
    for source in sources:
        label = source_label_map[source]
        try:

            if source == 1:
                prompt = (
                    "Give one factual and engaging piece of korean drama trivia or latest news on actors/dramas in 3 sentences. "
                    "Sentence 1 must start with 'Did you know'. "
                    "Sentences 2 and 3 should add interesting details or background."
                    "The fact should not have the same main idea as any of the sentences in the json file at " + json_firestore
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

            elif source == 2:
                prompt = (
                    "Give one short, factual and engaging latest news or trivia about KPOP groups BTS, BlackPink, Twice, or other famous KPOP groups in 3 sentences. "
                    "The first must start with 'Did you know'. "
                    "The next 2 sentences should give interesting supporting info or context."
                    "The fact should not have the same main idea as any of the sentences in the json file at " + json_firestore
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

        except Exception as e:
            # don't raise — try the next source
            print(f"get_dynamic_fact(): source {source} attempt failed: {e}")
            continue

    # If all sources failed, return honey fallback
    honey = (
        "Did you know Korea is the best? "
        "Just a huge fan talking. LOL "
        "Like and Subscribe!"
    )
    return honey, "Z"

# -------------------------------
# Gemini Helpers
# -------------------------------
from vertexai.generative_models import GenerativeModel

def generate_image_search_query(fact_text):
    """
    Generate a concise, high-quality search query for an image based on the trivia fact.
    Uses Gemini to handle all post-processing: max words, essential descriptors,
    no punctuation, high-quality visuals.
    """
    # Initialize Gemini model once
    try:
        model = GenerativeModel("gemini-2.5-flash")
    except Exception as e:
        print("⚠️ Failed to initialize Gemini model:", e)
        model = None

    # 🧠 Combined expert prompt
    prompt = (
        "You are an expert at creating search queries for image search APIs. "
        "Given the following trivia fact, generate the best possible search query that will yield a relevant, "
        "high-quality image.\n\n"
        "Rules:\n"
        "- Use less than 3 words only.\n"
        "- No punctuation.\n"
        "- Use the main idea/person/group of the fact only.\n"
        "- Output only the query string.\n\n"
        "Examples:\n"
        '"BTS group sign a contract with HYBE entertainment → "BTS kpop"\n'
        '"Did you know battery was made using..." → "=Battery"\n'
        '"Did you know the the first microwave is the F1thV" → "Microwave F1thv"\n\n'
        f"Fact: {fact_text}"
    )    

    try:
        if model:
            response = model.generate_content(prompt)
            search_query = (
                response.text.strip()
                if hasattr(response, "text") and response.text
                else fact_text
            )
        else:
            raise Exception("Model not available")

        # 🩵 Clean-up & ensure relevance
        search_query = search_query.replace('"', '').replace("'", "")
        if len(search_query.split()) > 10:
            search_query = " ".join(search_query.split()[:10])
        if not any(word in search_query.lower() for word in ["photo", "image", "still", "portrait"]):
            search_query += " photo"

        print(f"🎯 Final Search Query: {search_query}")
        return search_query

    except Exception as e:
        print(f"⚠️ Gemini search query generation failed: {e}")
        # Simple fallback: 4–5 cleaned keywords
        fallback_query = " ".join(
            fact_text.lower().replace("?", "").replace(".", "").split()[:5]
        ) + " photo"
        print(f"🪄 Fallback Search Query: {fallback_query}")
        return fallback_query

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
        gcs_path += "output.mp4"
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    gs_url = f"gs://{bucket_name}/{blob_path}"
    https_url = f"https://storage.googleapis.com/{bucket_name}/{blob_path}"
    return gs_url, https_url

def synthesize_speech(text, output_path, ytdest):
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # Pick voice depending on destination
    if ytdest == "tech":
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-AU",
            name="en-AU-Neural2-D",
            ssml_gender=texttospeech.SsmlVoiceGender.MALE
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.9,
            pitch=-3,
            volume_gain_db=2.0
        )  
    elif ytdest == "kk":
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Neural2-F",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
            pitch=-5,
            volume_gain_db=3.0
        )  
    else:
        # fallback voice
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Neural2-F",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.9,
            pitch=-3,
            volume_gain_db=2.0
        )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

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

def get_youtube_creds_from_secret_JINJA():
    client = secretmanager.SecretManagerServiceClient()
    secret_name = "Credentials_Jinja"
    project_id = "trivia-machine-472207"
    response = client.access_secret_version(
        name=f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    )
    creds_json = response.payload.data.decode("UTF-8")
    return Credentials.from_authorized_user_info(json.loads(creds_json))

def infer_category_from_fact(fact_text):
    keywords_map = {
        "pop culture": ["movie","film","tv","celebrity","music","show","trend","actor","actress","entertainment"],
        "sports": ["sports","football","soccer","basketball","tennis","olympics","f1","cricket","athlete","game","match","race"],
        "history": ["history","historical","war","ancient","medieval","civilization","empire","king","queen","tomb","archaeology"],
        "science": ["science","biology","chemistry","physics","space","universe","experiment","research","technology"],
        "tech": ["technology","tech","computer","ai","robot","software","hardware","gadget","innovation"]
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
    text = text.replace("\n"," ").replace("\r"," ").strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ",1)[0]
    return text

def upload_video_to_youtube_gcs(gcs_path, title, description, category, source_code, ytdest, tags=None, privacy="public"):
    try:
        if not gcs_path.startswith("gs://"):
            raise ValueError(f"Invalid GCS path: {gcs_path}")

        bucket_name, blob_name = gcs_path[5:].split("/",1)
        if ytdest == 'tech':
            creds = get_youtube_creds_from_secret()
        elif ytdest == 'kk':
            creds = get_youtube_creds_from_secret_JINJA()    
        youtube = build("youtube","v3",credentials=creds)

        # Download video locally
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            blob.download_to_filename(tmp.name)
            tmp_path = tmp.name
        
        media_body = MediaFileUpload(tmp_path, chunksize=-1, resumable=True)
        title_safe = sanitize_for_youtube(title, max_len=100)
        description += f"\n\n(S: {source_code})"
        description_safe = sanitize_for_youtube(description, max_len=5000)
        category_map = {"pop culture":"24","sports":"17","history":"22","science":"28","tech":"28"}
        category_id = category_map.get(category.lower(),"24")
        playlist_id = PLAYLIST_MAP.get(category.lower(),PLAYLIST_MAP["pop culture"])

        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {"title":title_safe,"description":description_safe,"tags":tags or ["trivia","quiz","fun"],"categoryId":category_id},
                "status":{"privacyStatus":privacy}
            },
            media_body=media_body
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"Upload progress: {int(status.progress()*100)}%")

        video_id = response["id"]
        print("Video uploaded. ID:", video_id)

        youtube.playlistItems().insert(
            part="snippet",
            body={"snippet":{"playlistId":playlist_id,"resourceId":{"kind":"youtube#video","videoId":video_id}}}
        ).execute()

        os.remove(tmp_path)
        return video_id

    except Exception as e:
        print("ERROR in YouTube upload:",str(e))
        raise


from difflib import SequenceMatcher

def is_similar(a, b, threshold=0.8):
    """Returns True if two strings are semantically similar."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > threshold

# --- Helper: Categorize tech topic ---
def detect_tech_category(fact_text: str) -> str:
    """Roughly classify the fact to adjust the Gemini prompt style."""
    text = fact_text.lower()

    # Check for known product or brand cues
    if any(x in text for x in ["iphone", "macbook", "samsung", "xiaomi", "laptop", "camera", "headphones", "gpu", "processor", "chip", "gadget"]):
        return "product"

    # Apps or software tools
    if any(x in text for x in ["app", "software", "android", "ios", "windows", "chrome", "facebook", "tiktok", "instagram", "youtube", "browser"]):
        return "app"

    # Broader tech or futuristic concepts
    if any(x in text for x in ["ai", "artificial intelligence", "quantum", "robot", "blockchain", "server", "data", "cloud", "internet", "virtual reality"]):
        return "concept"

    return "generic"


# --- Helper: Generate tech-themed image using Gemini ---
from vertexai.preview.vision_models import ImageGenerationModel
import vertexai
import os
import time

def generate_gemini_tech_image(fact_text, tmpdir, max_retries=5):
    """
    Generate a relevant tech/product/app image using Google's Imagen model via Vertex AI.
    This follows the officially documented method (no 'response_mime_type' hack).
    """
    vertexai.init(project="trivia-machine-472207", location="us-central1")
    model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")

    # --- Categorize tech topic ---
    text = fact_text.lower()
    if any(x in text for x in ["iphone", "macbook", "samsung", "camera", "laptop", "gpu", "chip", "gadget"]):
        category = "product"
    elif any(x in text for x in ["app", "software", "android", "ios", "windows", "tiktok", "instagram", "facebook"]):
        category = "app"
    elif any(x in text for x in ["ai", "robot", "blockchain", "quantum", "server", "cloud", "internet"]):
        category = "concept"
    else:
        category = "generic"

    # --- Dynamic prompt ---
    if category == "product":
        prompt = (
            f"High-quality cinematic studio photo of the gadget, device, or hardware described below. "
            f"Professional lighting, clear object view, soft neutral background. "
            f"No humans or text. Topic: {fact_text}"
        )
    elif category == "app":
        prompt = (
            f"Modern, realistic digital illustration representing the app or software interface described below. "
            f"Show screens, panels, or icons in a clean 3D or digital environment. "
            f"No logos or text overlays. Topic: {fact_text}"
        )
    elif category == "concept":
        prompt = (
            f"Futuristic concept art representing the technology or innovation mentioned below. "
            f"Sleek, cinematic, modern style. No humans or text. Topic: {fact_text}"
        )
    else:
        prompt = (
            f"Clean, realistic image representing a technology-related object or idea described below. "
            f"Modern lighting and depth of field. No humans or text. Topic: {fact_text}"
        )

    # --- Generate with retry ---
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[TECH] 🧠 Imagen (Gemini) attempt {attempt} for category: {category}")
            response = model.generate_images(
                prompt=prompt,
                number_of_images=1,
                aspect_ratio="16:9"
            )

            if response and response.images:
                image_bytes = response.images[0]._image_bytes  # officially returned raw bytes
                bg_path = os.path.join(tmpdir, f"tech_bg_{attempt}.jpg")

                with open(bg_path, "wb") as f:
                    f.write(image_bytes)

                print(f"[TECH] ✅ Gemini Imagen generated tech image ({category}) on attempt {attempt}")
                return bg_path
            else:
                print(f"[TECH] ⚠️ Imagen returned no images on attempt {attempt}")
        except Exception as e:
            print(f"[TECH] ⚠️ Gemini Imagen attempt {attempt} failed: {e}")
            time.sleep(2)

    raise RuntimeError(f"[TECH] Gemini Imagen failed to generate a valid tech image after {max_retries} attempts.")

def build_kpop_gemini_prompt(fact_text: str) -> str:
    """
    Builds an adaptive prompt for Gemini/Imagen image generation based on the
    content of the fact_text — handling K-pop groups, solo idols, Korean actors/actresses,
    and automatically choosing between drama scene vs press photo for actors/actresses.
    """
    lower = fact_text.lower()

    female_groups = [
        "blackpink", "newjeans", "ive", "aespa", "itzy", "twice",
        "red velvet", "g-idle", "mamamoo", "stayc", "kep1er", "le sserafim",
        "nmixx", "fromis_9", "oh my girl", "woo!ah!", "purple kiss"
    ]

    male_groups = [
        "bts", "exo", "seventeen", "nct", "stray kids", "txt", "enhypen",
        "ateez", "treasure", "monsta x", "super junior", "shinee", "astro",
        "the boyz", "sf9", "ikon", "pentagon", "winner"
    ]

    female_idols = [
        "jennie", "lisa", "jisoo", "rose", "sana", "mina", "nayeon",
        "momo", "jihyo", "dahyun", "chaeyoung", "tzuyu", "karina", "winter",
        "giselle", "ningning", "yuna", "yeji", "ryujin", "lia", "chaeryeong",
        "wendy", "joy", "seulgi", "irene", "yeri", "soyeon", "miyeon", "minnie",
        "yuqi", "shuhua", "hwasa", "solar", "moonbyul", "wheein", "sulhyun",
        "hani", "hyuna", "sunmi", "iu", "taeyeon", "chungha", "cl", "boa"
    ]

    male_idols = [
        "jungkook", "v ", "taehyung", "jin", "rm", "suga", "j-hope",
        "jimin", "kai", "baekhyun", "chanyeol", "mark", "jeno", "taeyong",
        "ten", "johnny", "jaehyun", "taemin", "minho", "key", "hyunjin",
        "felix", "han", "changbin", "bang chan", "soobin", "yeonjun",
        "beomgyu", "jay", "heeseung", "sunghoon", "ni-ki"
    ]

    actors = [
        "lee min-ho", "hyun bin", "park seo-joon", "song joong-ki", "gong yoo",
        "lee byung-hun", "jo in-sung", "park bo-gum", "nam joo-hyuk", "ahn hyo-seop",
        "cha eun-woo", "kim soo-hyun", "lee dong-wook", "ji chang-wook",
        "kim woo-bin", "ryu jun-yeol", "kang ha-neul", "park hyung-sik", "im si-wan",
        "lee seung-gi", "kim bum", "yoo seung-ho"
    ]

    actresses = [
        "song hye-kyo", "jun ji-hyun", "kim tae-ri", "bae suzy", "shin min-a",
        "han hyo-joo", "park min-young", "kim go-eun", "lee sung-kyung",
        "kim ji-won", "seo ye-ji", "park shin-hye", "lee da-hee",
        "hwang jung-eum", "kim hye-soo", "yoona", "jang nara", "kim hee-sun",
        "kim yoo-jung", "go ara", "iu", "seolhyun"
    ]

    # Choose style variant for actors/actresses
    # e.g., if fact_text contains keywords like “drama”, “scene”, “episode”, “on-set”, use drama style,
    # else use press headshot style
    def actor_prompt_style(name: str, is_actor: bool):
        style = ""
        if "drama" in lower or "scene" in lower or "episode" in lower:
            if is_actor:
                style = (
                    "a Korean male actor in a dramatic TV-drama scene, cinematic lighting, "
                    "moody atmosphere, expressive face"
                )
            else:
                style = (
                    "a Korean actress in a dramatic K-drama scene, emotional lighting, "
                    "style reminiscent of a film poster"
                )
        else:
            if is_actor:
                style = (
                    "a Korean male actor in a professional studio portrait, sleek suit, "
                    "high-fashion look, minimal background"
                )
            else:
                style = (
                    "a Korean actress in a soft-lighting studio portrait, elegant dress, "
                    "magazine cover style"
                )
        return style

    if any(g in lower for g in female_groups):
        return (
            "a group of 4-5 Korean female pop idols performing on stage with pink and neon lights, "
            "K-pop concert atmosphere, cinematic lighting, vibrant audience glow"
        )

    elif any(g in lower for g in male_groups):
        return (
            "a group of Korean male pop idols performing in concert lighting, energetic stage scene, "
            "blue and red back-lights, intense choreography moment"
        )

    elif any(n in lower for n in female_idols):
        return (
            "a Korean female pop idol performing on stage in elegant fashion lighting, "
            "cinematic close-up, modern concert background, soft glow aesthetic"
        )

    elif any(n in lower for n in male_idols):
        return (
            "a Korean male pop idol in stylish concert stage lighting, confident pose, "
            "spotlight from above, dramatic concert colour palette"
        )

    elif any(a in lower for a in actors):
        return actor_prompt_style(a, is_actor=True)

    elif any(a in lower for a in actresses):
        return actor_prompt_style(a, is_actor=False)

    else:
        return (
            "a cinematic portrait of a Korean pop idol or actor performing live, "
            "stylish and vivid K-pop or K-drama aesthetic lighting"
        )

from vertexai.preview import generative_models
import vertexai
import os
from PIL import Image
from io import BytesIO

def generate_gemini_image(prompt: str, tmpdir: str, retries: int = 5) -> str:
    """
    Generate an image using Vertex AI Gemini (same as tech) given a text prompt.
    Saves the image as PNG in tmpdir and returns the path.
    Auto-handles both PNG and JPEG responses.
    """
    vertexai.init(project="trivia-machine-472207", location="us-central1")
    model = generative_models.GenerativeModel("gemini-2.5-flash")
    output_path = os.path.join(tmpdir, "gemini_generated.png")

    print(f"[KK] 🧠 Gemini generating image for prompt: {prompt}")

    for attempt in range(1, retries + 1):
        try:
            # Ask for PNG by default, but we’ll handle JPEG too
            response = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "image/png"}
            )

            image_data = None
            mime_type = None

            # Extract image bytes from candidates
            if hasattr(response, "candidates") and response.candidates:
                for cand in response.candidates:
                    for part in getattr(cand.content, "parts", []):
                        # Gemini sometimes omits "type" but has mime_type
                        if hasattr(part, "mime_type") and "image" in part.mime_type:
                            mime_type = part.mime_type
                            image_data = part.data
                            break
                    if image_data:
                        break

            if not image_data:
                raise ValueError("No image data returned from Gemini.")

            # Handle both base64 or raw bytes
            try:
                # Some Vertex SDKs return base64-encoded bytes
                image_bytes = base64.b64decode(image_data)
            except Exception:
                # If already bytes
                image_bytes = image_data

            # Open and convert image → PNG
            img = Image.open(BytesIO(image_bytes)).convert("RGB")
            img.save(output_path, "PNG")

            print(f"[KK] ✅ Gemini image generated successfully ({mime_type}) → {output_path}")
            return output_path

        except Exception as e:
            print(f"[KK] ⚠️ Gemini attempt {attempt} failed: {e}")
            if attempt == retries:
                raise RuntimeError(f"[KK] Gemini failed after {retries} attempts.")
            continue
            
# -------------------------------
# Core: Create Video with Text (Gemini-only image source)
# -------------------------------
def create_trivia_video(fact_text, ytdest, output_gcs_path="gs://trivia-videos-output/output.mp4"):
    with tempfile.TemporaryDirectory() as tmpdir:
        fact_text = fact_text.replace("*", "").strip()
        bg_path = os.path.join(tmpdir, "background.jpg")
        valid_image = False

        try:
            if ytdest.lower() == "tech":
                print(f"[{ytdest.upper()}] 🧠 Generating tech image with Gemini (adaptive mode)...")
                bg_path = generate_gemini_tech_image(fact_text, tmpdir)
                valid_image = True

            elif ytdest.lower() == "kk":
                print(f"[{ytdest.upper()}] 🧠 Fetching free/unlicensed image...")
            
                # 1️⃣ Generate concise search query using Gemini
                search_query = extract_search_query(fact_text)
                print(f"[{ytdest.upper()}] 🔎 Search query for free image: {search_query}")
            
                img_url = None
            
                # 2️⃣ Try DuckDuckGo first
                try:
                    from duckduckgo_search import DDGS
                    with DDGS() as ddgs:
                        results = list(ddgs.images(search_query, max_results=1))
                    if results:
                        img_url = results[0].get("image")
                        if img_url:
                            resp = requests.get(img_url, stream=True, timeout=20)
                            if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", "").lower():
                                with open(bg_path, "wb") as f:
                                    for chunk in resp.iter_content(8192):
                                        if chunk:
                                            f.write(chunk)
                                valid_image = True
                                print(f"[{ytdest.upper()}] ✅ DuckDuckGo image downloaded: {img_url}")
                            else:
                                print(f"[{ytdest.upper()}] ⚠️ DuckDuckGo GET failed: status={resp.status_code}")
                                resp.close()
                except Exception as e:
                    print(f"[{ytdest.upper()}] ⚠️ DuckDuckGo search failed: {e}")

                # fallback to Gemini if free/unlicensed fetch fails
                if not valid_image:
                    print(f"[{ytdest.upper()}] ⚠️ Falling back to Gemini generation...")
                    prompt = build_kpop_gemini_prompt(fact_text)
                    bg_path = generate_gemini_image(prompt, tmpdir)
                    valid_image = True

            if not valid_image:
                raise RuntimeError(f"{ytdest.upper()} failed to produce a valid background image.")

        except Exception as e:
            print(f"[{ytdest.upper()}] 🔥 Image creation failed: {e}")
            raise

        print(f"[{ytdest.upper()}] 🖼️ Background image ready → {bg_path}")

        # --- Resize/crop to 1080x1920 ---
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

        # --- Split fact text into pages ---
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 55)
        x_margin = int(img.width * 0.1)
        max_width = int(img.width * 0.8)

        words = fact_text.split()
        lines = []
        current_line = []
        for word in words:
            test_line = " ".join(current_line + [word])
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line.append(word)
            else:
                lines.append(" ".join(current_line))
                current_line = [word]
        if current_line:
            lines.append(" ".join(current_line))

        pages = ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]

        # --- SINGLE continuous TTS for all pages ---
        audio_path = os.path.join(tmpdir, "audio_full.mp3")
        synthesize_speech(fact_text, audio_path, ytdest)
        full_audio_clip = AudioFileClip(audio_path)
        audio_duration = full_audio_clip.duration

        # --- Use Aeneas forced alignment to get exact timings per page ---
        print("Running Aeneas alignment...")
        text_path = os.path.join(tmpdir, "fact.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            # Write each page (2 lines) on a separate line for alignment
            for page in pages:
                f.write(page.replace("\n", " ") + "\n")

        config_string = "task_language=eng|is_text_type=plain|os_task_file_format=json"
        task = Task(config_string=config_string)
        task.audio_file_path_absolute = audio_path
        task.text_file_path_absolute = text_path
        task.sync_map_file_path_absolute = os.path.join(tmpdir, "map.json")

        ExecuteTask(task).execute()
        task.output_sync_map_file()

        # --- Parse Aeneas output to derive durations and authoritative starts ---
        with open(task.sync_map_file_path_absolute, "r", encoding="utf-8") as f:
            sync_map = json.load(f)

        segments = sync_map.get("fragments", [])
        per_page_durations = []
        aeneas_starts = []
        for i in range(len(pages)):
            if i < len(segments):
                start = float(segments[i].get("begin", 0))
                end = float(segments[i].get("end", 0))
                dur = max(0.05, end - start)
                per_page_durations.append(dur)
                aeneas_starts.append(start)
            else:
                # fallback: small duration and start guessed as cumulative so far
                per_page_durations.append(1.0)
                aeneas_starts.append(sum(per_page_durations[:-1]))

        # NOTE:
        # We will trust Aeneas start times (aeneas_starts) as authoritative.
        # Build video_starts from per_page_durations; if any video_start is earlier
        # than Aeneas start (i.e., page appears too early), we delay that page by
        # adding the needed delta to its immediate predecessor duration.
        # This avoids showing a page before the audio reaches it (fixes last-page-ahead).

        # compute initial video starts
        video_starts = []
        acc = 0.0
        for d in per_page_durations:
            video_starts.append(acc)
            acc += d

        # correction loop: if page i would start earlier than aeneas_starts[i], push it later
        for i in range(1, len(pages)):
            delta = aeneas_starts[i] - video_starts[i]
            # only adjust if delta is meaningfully positive (page would be early)
            if delta > 0.03:
                # add delta to immediate predecessor so page i starts later
                per_page_durations[i - 1] = max(0.05, per_page_durations[i - 1] + delta)
                # recompute subsequent video_starts
                video_starts = []
                acc = 0.0
                for d in per_page_durations:
                    video_starts.append(acc)
                    acc += d

        # If after corrections the total video sum differs from audio, adjust last clip
        total_video_len = sum(per_page_durations)
        if total_video_len < audio_duration:
            per_page_durations[-1] += (audio_duration - total_video_len)
        elif total_video_len > audio_duration + 0.001 and len(per_page_durations) > 1:
            # if video too long (should be rare), trim a tiny bit from the last non-zero predecessors
            excess = total_video_len - audio_duration
            # remove proportionally from earlier clips (but keep min 0.05)
            adjustable_indices = list(range(len(per_page_durations) - 1))
            adj_total = sum(per_page_durations[i] - 0.05 for i in adjustable_indices if per_page_durations[i] > 0.05)
            if adj_total > 0:
                for i in adjustable_indices:
                    if per_page_durations[i] > 0.05:
                        take = (per_page_durations[i] - 0.05) / adj_total * excess
                        per_page_durations[i] = max(0.05, per_page_durations[i] - take)
                # final safety: recompute and adjust last
                total_video_len = sum(per_page_durations)
                if total_video_len > audio_duration:
                    per_page_durations[-1] = max(0.05, per_page_durations[-1] - (total_video_len - audio_duration))

        # --- Prepare logo once (hardcoded GCS path) ---
        logo_resized = None
        try:
            if ytdest == "tech":
                logo_url = "https://storage.googleapis.com/trivia-videos-output/trivia_logo.png"
                logo_path = os.path.join(tmpdir, "trivia_logo.png")
            elif ytdest == "kk":
                logo_url = "https://storage.googleapis.com/trivia-videos-output/trivia_logo_jinja.png"
                logo_path = os.path.join(tmpdir, "trivia_logo_jinja.png")
            r = requests.get(logo_url, timeout=10)
            if r.ok:
                with open(logo_path, "wb") as lf:
                    lf.write(r.content)
                logo = Image.open(logo_path).convert("RGBA")
                
                # Resize to 50% of original
                logo_resized = logo.resize((logo.width // 2, logo.height // 2), Image.LANCZOS)
                
                # Apply 80% opacity
                alpha = logo_resized.split()[3].point(lambda p: int(p * 0.8))
                logo_resized.putalpha(alpha)
                
                print(f"✅ Logo loaded, resized to {logo_resized.size} with 80% opacity")
            else:
                print("⚠️ Logo request returned non-ok status:", r.status_code)
        except Exception as e:
            print("⚠️ Failed to download/prepare logo:", e)
            logo_resized = None

        # --- Page creation synced to Aeneas durations ---
        clips = []
        for i, (page_text, duration) in enumerate(zip(pages, per_page_durations)):
            page_img = img.copy().convert("RGBA")  # Ensure RGBA for transparency
            draw_page = ImageDraw.Draw(page_img)
        
            # --- Calculate text position ---
            bbox = draw_page.multiline_textbbox((0, 0), page_text, font=font, spacing=15)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            text_x = (page_img.width - text_w) / 2
            text_y = (page_img.height - text_h) / 2
        
            # --- Draw text ---
            draw_page.multiline_text(
                (text_x, text_y),
                page_text,
                font=font,
                fill="#FFD700",
                spacing=15,
                stroke_width=40,
                stroke_fill="black",
                align="center"
            )
        
            # --- Paste logo above text ---
            # --- Paste huge logo above text (testing) ---
            if logo_resized is not None:
                try:
                    # Resize to 20% of video width
                    target_logo_width = int(page_img.width * 0.24)
                    aspect_ratio = logo_resized.height / logo_resized.width
                    logo = logo_resized.resize(
                        (target_logo_width, int(target_logo_width * aspect_ratio)),
                        Image.LANCZOS
                    )
            
                    # Ensure logo is RGBA
                    logo = logo.convert("RGBA")
            
                    # Apply  opacity
                    alpha = logo.split()[3].point(lambda p: int(p * 0.23))
                    logo.putalpha(alpha)
            
                    # Center horizontally
                    logo_x = (page_img.width - logo.width) // 2
            
                    # Fixed Y position (68% down)
                    logo_y = int(page_img.height * 0.63)
            
                    # Ensure base image is RGBA
                    page_rgba = page_img.convert("RGBA")
            
                    # Paste logo using alpha as mask
                    page_rgba.paste(logo, (logo_x, logo_y), mask=logo)
            
                    # Convert back to RGB for final saving
                    page_img = page_rgba.convert("RGB")
            
                    print(f"✅ Logo pasted with 80% opacity at ({logo_x},{logo_y})")
                except Exception as e:
                    print(f"⚠️ Failed to paste logo: {e}")
        
            # --- Flatten and save ---
            page_img_rgb = page_img.convert("RGB")
            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img_rgb.save(page_path)
        
            clip = ImageClip(page_path).set_duration(duration)
            clips.append(clip)

        # Final safety: ensure last clip covers remaining audio time if tiny diff
        total_video_len = sum(c.duration for c in clips)
        if total_video_len < audio_duration - 1e-3 and len(clips) > 0:
            extra = audio_duration - total_video_len
            last = clips[-1]
            clips[-1] = last.set_duration(last.duration + extra)

        video_clip = concatenate_videoclips(clips).set_audio(full_audio_clip)
        output_path = os.path.join(tmpdir, "trivia_video.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264",
                                   audio_codec="aac", verbose=False, logger=None)

        gs_url, https_url = upload_to_gcs(output_path, output_gcs_path)
        return gs_url, https_url

# -------------------------------
# Flask Endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        # === Main pipeline ===
        fact_data = data.get("fact")
        if fact_data:
            fact = fact_data
            source_code = data.get("source_code", "X")
        else:
            fact, source_code = get_unique_fact("tech")
        
        category = data.get("category") or infer_category_from_fact(fact)

        # Output path in GCS
        output_gcs_path = "gs://trivia-videos-output/output_tech.mp4"
        video_gs_url, video_https_url = create_trivia_video(fact, "tech", output_gcs_path)

        # Generate YouTube title and description
        title_options = [
            "Did you know?", "Trivia Time!", "Quick Fun Fact!", "Can You Guess This?",
            "Learn Something!", "Well Who Knew?", "Wow Really?", "Fun Fact Alert!",
            "Now You Know!", "Not Bad!", "Mind-Blowing Fact!"
        ]
        youtube_title = sanitize_for_youtube(random.choice(title_options), max_len=100)
        youtube_description = sanitize_for_youtube(fact, max_len=5000)

        # Upload to YouTube
        video_id = upload_video_to_youtube_gcs(
            video_gs_url,
            youtube_title,
            youtube_description,
            category,
            source_code,
            "tech"
        )

        main_result = {
            "fact": fact,
            "video_gcs": video_https_url,
            "youtube_video_id": video_id
        }

        # === Korean pipeline ===
        try:
            data = request.get_json(silent=True) or {}
            # === Korean pipeline ===
            fact_data = data.get("fact")
            if fact_data:
                fact = fact_data
                source_code = data.get("source_code", "X")
            else:
                fact, source_code = get_unique_fact("kk")
            
            category = data.get("category") or infer_category_from_fact(fact)    

            # Output path in GCS
            output_gcs_path = "gs://trivia-videos-output/output_kk.mp4"
            video_gs_url, video_https_url = create_trivia_video(fact, "kk", output_gcs_path)
    
            # Generate YouTube title and description
            title_options = [
                "Did you know?", "Trivia Time!", "Quick Fun Fact!", "Can You Guess This?",
                "Learn Something!", "Well Who Knew?", "Wow Really?", "Fun Fact Alert!",
                "Now You Know!", "Not Bad!", "Mind-Blowing Fact!"
            ]
            youtube_title = sanitize_for_youtube(random.choice(title_options), max_len=100)
            youtube_description = sanitize_for_youtube(fact, max_len=5000)
    
            # Upload to YouTube
            video_id = upload_video_to_youtube_gcs(
                video_gs_url,
                youtube_title,
                youtube_description,
                category,
                source_code,
                "kk"
            )

            qq_result = {
                "fact": fact,
                "video_gcs": video_https_url,
                "youtube_video_id": video_id
            }

        except Exception as qq_err:
            import traceback
            traceback.print_exc()
            qq_result = {"error": str(qq_err)}

        # === Combined result ===
        return jsonify({
            "status": "ok",
            "main": main_result,
            "qq": qq_result
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
