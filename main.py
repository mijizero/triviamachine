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
# Dynamic Fact (Firestore version)
# -------------------------------
from google.cloud import firestore

# Force Firestore client to use correct project
firestore_client = firestore.Client(project="trivia-machine-472207", database="(default)")

import re
import hashlib
from difflib import SequenceMatcher
from google.cloud import firestore

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
                    "Give one factual and engaging piece of technology trivia in 3 sentences. "
                    "Sentence 1 must start with 'Did you know'. "
                    "Sentences 2 and 3 should add interesting details or background."
                    "The fact should not be the same concept or main idea as any of the facts in the json file at " + json_firestore
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

            elif source == 2:
                prompt = (
                    "Give one short, factual explanation on how some piece of technology or everyday product works in 3 sentences. "
                    "The first must start with 'Did you know'. "
                    "The next 2 sentences should give interesting supporting info or context."
                    "The fact should not be the same concept or main idea as any of the entries in the json file at " + json_firestore
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
                    "The fact should not be the same concept or main idea as any of the facts in the json file at " + json_firestore
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

            elif source == 2:
                prompt = (
                    "Give one short, factual and engaging latest news or trivia about KPOP groups BTS, BlackPink, Twice, or other famous KPOP groups in 3 sentences. "
                    "The first must start with 'Did you know'. "
                    "The next 2 sentences should give interesting supporting info or context."
                    "The fact should not be the same concept or main idea as any of the entries in the json file at " + json_firestore
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
    elif ytdest == "kk":
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Neural2-F",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
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

# -------------------------------
# Gemini-only Image Fetch
# -------------------------------
def fetch_valid_image_with_gemini(fact_text, max_retries=30, timeout_head=10):
    """
    Uses Gemini as the sole image fetcher.
    Keeps retrying until Gemini returns a valid direct image URL or max_retries reached.

    Validation performed by runtime:
      - URL starts with http/https
      - URL ends with .jpg/.jpeg/.png/.webp (case-insensitive)
      - HEAD request returns 200 and Content-Type contains "image"
      - If HEAD returns 405 or 501 (some servers disallow HEAD), try a lightweight GET with stream=True and read minimal bytes.

    Returns the validated image URL string, or None if not found.
    """
    model = GenerativeModel("gemini-2.5-flash")

    # Trusted domain hint — Gemini should prefer these but it's not enforced by runtime
    trusted_domains_hint = (
        "Prefer images hosted on reliable public domains such as "
        "wikimedia.org, commons.wikimedia.org, unsplash.com, pexels.com, images.unsplash.com, "
        "or reputable news / official sources."
    )

    base_prompt = f"""
You are a visual retrieval agent for an automated trivia video system.
Given the fact below, return ONLY ONE direct image URL (no markdown, no commentary).
Requirements:
- The URL must be a direct image link that ends with .jpg, .jpeg, .png, or .webp.
- Prefer vertical/portrait images when available.
- Prefer images from publicly accessible, reliable domains (google images, twitter/X, pinterest, official press photos, reputable news sites).
- Do NOT return short links (bit.ly, t.co). Return the full URL.
- Do NOT include any other text, explanation, or punctuation — only the single URL on one line.

{trusted_domains_hint}

Fact:
{fact_text}
    """.strip()

    for attempt in range(1, max_retries + 1):
        try:
            # Ask Gemini for a direct image URL
            response = model.generate_content(base_prompt)

            # Extract text if present
            candidate = None
            if response and getattr(response, "text", None):
                candidate = response.text.strip()
            else:
                # Some response shapes may have candidates; try to extract text parts robustly
                try:
                    # attempt to read candidates if available
                    for cand in getattr(response, "candidates", []) or []:
                        for part in getattr(cand, "content", {}).get("parts", []) or []:
                            text_part = getattr(part, "text", None)
                            if text_part:
                                candidate = text_part.strip()
                                break
                        if candidate:
                            break
                except Exception:
                    candidate = None

            if not candidate:
                print(f"⚠️ [Gemini attempt {attempt}] Empty response — retrying...")
                continue

            # Take only the first line (some responses may include extra newlines)
            candidate = candidate.splitlines()[0].strip()

            # Quick pattern check
            if not re.match(r"^https?://", candidate, re.IGNORECASE):
                print(f"⚠️ [Gemini attempt {attempt}] Not an http/https URL: {candidate!r}")
                continue
            if not re.search(r"\.(jpg|jpeg|png|webp)(?:\?.*)?$", candidate, re.IGNORECASE):
                print(f"⚠️ [Gemini attempt {attempt}] URL does not end with an image extension: {candidate!r}")
                continue

            # Perform HTTP HEAD to validate content-type and status
            try:
                head = requests.head(candidate, allow_redirects=True, timeout=timeout_head)
            except requests.RequestException as e:
                # Some servers don't support HEAD; we'll try a small GET as fallback check below
                head = None
                head_err = e

            if head is not None:
                status = getattr(head, "status_code", None)
                ctype = head.headers.get("Content-Type", "") if head.headers else ""
                if status == 200 and ctype and "image" in ctype.lower():
                    print(f"✅ [Gemini attempt {attempt}] Valid image URL confirmed via HEAD: {candidate}")
                    return candidate
                # If HEAD returned 200 but content-type missing, we may still try GET below
                if status is not None and status >= 400:
                    print(f"⚠️ [Gemini attempt {attempt}] HEAD returned status {status} for {candidate!r}")
                else:
                    print(f"⚠️ [Gemini attempt {attempt}] HEAD content-type: {ctype!r} for {candidate!r}")

            # If HEAD not usable or inconclusive, try lightweight GET
            try:
                get_resp = requests.get(candidate, stream=True, allow_redirects=True, timeout=timeout_head)
                status = getattr(get_resp, "status_code", None)
                ctype = get_resp.headers.get("Content-Type", "") if get_resp.headers else ""
                if status == 200 and ctype and "image" in ctype.lower():
                    # read a small chunk to be sure (and then close)
                    try:
                        next(get_resp.iter_content(1024))
                    except StopIteration:
                        # no content
                        print(f"⚠️ [Gemini attempt {attempt}] GET returned empty body for {candidate!r}")
                        get_resp.close()
                        continue
                    get_resp.close()
                    print(f"✅ [Gemini attempt {attempt}] Valid image URL confirmed via GET: {candidate}")
                    return candidate
                else:
                    print(f"⚠️ [Gemini attempt {attempt}] GET status {status}, content-type {ctype!r} for {candidate!r}")
                    try:
                        get_resp.close()
                    except Exception:
                        pass
            except requests.RequestException as e_get:
                # network/connectivity error for this candidate
                print(f"⚠️ [Gemini attempt {attempt}] GET request failed: {e_get}")

            # If we reach here, candidate failed validation
            print(f"⚠️ [Gemini attempt {attempt}] Candidate failed validation: {candidate!r}")
            # continue to next attempt

        except Exception as e:
            print(f"⚠️ [Gemini attempt {attempt}] Exception while asking Gemini: {e}")

    # exhausted attempts
    print(f"❌ Gemini failed to produce a valid image after {max_retries} attempts.")
    return None


# -------------------------------
# Core: Create Video with Text (Gemini-only image source)
# -------------------------------
def create_trivia_video(fact_text, ytdest, output_gcs_path="gs://trivia-videos-output/output.mp4"):
    with tempfile.TemporaryDirectory() as tmpdir:
        fact_text = fact_text.replace("*", "").strip()

        bg_path = os.path.join(tmpdir, "background.jpg")
        valid_image = False
        img_url = None

        try:
            print(f"[{ytdest.upper()}] 🧠 Fetching image using Gemini only...")
            img_url = fetch_valid_image_with_gemini(fact_text, max_retries=30)

            if img_url:
                resp = requests.get(img_url, stream=True, timeout=20)
                if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", "").lower():
                    with open(bg_path, "wb") as f:
                        for chunk in resp.iter_content(8192):
                            if chunk:
                                f.write(chunk)
                    valid_image = True
                    print(f"[{ytdest.upper()}] ✅ Image downloaded and saved: {img_url}")
                else:
                    print(f"[{ytdest.upper()}] ⚠️ Final GET failed: status={getattr(resp,'status_code',None)}, ctype={resp.headers.get('Content-Type')}")
                    resp.close()

            if not valid_image:
                # strictly per your request, no fallbacks — raise here
                raise RuntimeError("Gemini failed to produce a valid image after 30 attempts.")

        except Exception as e:
            print(f"[{ytdest.upper()}] 🔥 Gemini image fetch failed: {e}")
            # re-raise so upstream continues to see failure as before
            raise

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
