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
    """Save a new fact to Firestore (for future duplicate detection)."""
    normalized = normalize_fact(fact)
    try:
        db.collection(FACTS_COLLECTION).add({
            "fact": fact,
            "normalized": normalized
        })
    except Exception as e:
        print(f"⚠️ Could not save fact to Firestore: {e}")


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

def save_fact(fact_text):
    try:
        db.collection(FACTS_COLLECTION).add({
            "fact": fact_text,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("Error saving fact to Firestore:", str(e))

def get_unique_fact():
    recent = load_recent_facts()
    for _ in range(5):
        fact, source_code = get_dynamic_fact()
        if not is_duplicate_fact(fact) and fact not in recent:
            save_fact(fact)
            return fact, source_code
    # fallback if all failed
    fact, source_code = get_dynamic_fact()
    save_fact(fact)
    return fact, source_code

def get_dynamic_fact():
    """Try the 4 sources in random order and return (fact_text, source_label).
    If every source attempt fails, return the honey fallback with source 'Z'."""
    sources = [1, 2, 3, 4]
    random.shuffle(sources)
    source_label_map = {1: "A", 2: "B", 3: "C", 4: "D"}

    def gemini_fact(prompt):
        model = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip() if response and getattr(response, "text", None) else ""

    # Try each source once in a random order
    for source in sources:
        label = source_label_map[source]
        try:
            if source == 1:
                try:
                    res = requests.get(
                        "https://en.wikipedia.org/api/rest_v1/page/random/summary",
                        timeout=10
                    )
                    if res.ok:
                        data = res.json()
                        title = data.get("title", "")
                        extract = data.get("extract", "")
                        wiki_text = f"{title}: {extract}"
                        prompt = (
                            "Rewrite the following Wikipedia summary into a 3-sentence trivia fact. "
                            "Start with 'Did you know', then add 2 supporting sentences that give background or interesting details.\n\n"
                            f"Summary: {wiki_text}"
                        )
                        fact = gemini_fact(prompt)
                        if fact:
                            return fact, label
                except Exception:
                    # try next source
                    continue

            elif source == 2:
                prompt = (
                    "Give one factual and engaging piece of technology trivia in 3 sentences. "
                    "Sentence 1 must start with 'Did you know'. "
                    "Sentences 2 and 3 should add interesting details or background."
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

            elif source == 3:
                prompt = (
                    "Give one true and engaging trivia, fact, or recent news about kdrama, kpop, or korean celebrities in 3 sentences. "
                    "Start with 'Did you know', then add 2 supporting sentences with factual context or significance."
                )
                fact = gemini_fact(prompt)
                if fact:
                    return fact, label

            elif source == 4:
                prompt = (
                    "Give one short, factual trivia about trending international media, movies, or celebrities in 3 sentences. "
                    "The first must start with 'Did you know'. "
                    "The next 2 sentences should give interesting supporting info or context."
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
        gcs_path += "output.mp4"
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
        speaking_rate=0.9,
        pitch=-3,
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

def upload_video_to_youtube_gcs(gcs_path, title, description, category, source_code, tags=None, privacy="public"):
    try:
        if not gcs_path.startswith("gs://"):
            raise ValueError(f"Invalid GCS path: {gcs_path}")

        bucket_name, blob_name = gcs_path[5:].split("/",1)
        creds = get_youtube_creds_from_secret()
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

# -------------------------------
# Core: Create Video with Text
# -------------------------------
from difflib import SequenceMatcher

def is_similar(a, b, threshold=0.8):
    """Returns True if two strings are semantically similar."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > threshold

def create_trivia_video(fact_text, output_gcs_path="gs://trivia-videos-output/output.mp4"):
    with tempfile.TemporaryDirectory() as tmpdir:
        fact_text = fact_text.replace("*", "").strip()
        search_query = extract_search_query(fact_text)
        bg_path = os.path.join(tmpdir, "background.jpg")
        valid_image = False
        img_url = None

        # 1️⃣ Try DuckDuckGo first
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

        # 2️⃣ Pexels fallback
        if not valid_image:
            try:
                simplified_query = search_query.lower()
                for word in ["in", "of", "the", "from", "at", "on", "a", "an"]:
                    simplified_query = simplified_query.replace(f" {word} ", " ")
                simplified_query = simplified_query.strip().split()[:2]
                simplified_query = " ".join(simplified_query) or search_query

                headers = {"Authorization": "zXJ9dAVT3F0TLcEqMkGXtE5H8uePbhEvuq0kBnWnbq8McMpIKTQeWnDQ"}
                pexels_url = f"https://api.pexels.com/v1/search?query={simplified_query}&orientation=portrait&per_page=1"
                r = requests.get(pexels_url, headers=headers, timeout=10)
                if r.ok:
                    data = r.json()
                    if data.get("photos"):
                        img_url = data["photos"][0]["src"]["original"]
                        response = requests.get(img_url, stream=True, timeout=10)
                        if response.status_code == 200 and "image" in response.headers.get("Content-Type", ""):
                            with open(bg_path, "wb") as f:
                                for chunk in response.iter_content(8192):
                                    f.write(chunk)
                            valid_image = True
            except Exception as e:
                print("Pexels fallback failed:", e)

        # 3️⃣ Final fallback
        if not valid_image:
            fallback_url = "https://storage.googleapis.com/trivia-videos-output/background.jpg"
            response = requests.get(fallback_url)
            with open(bg_path, "wb") as f:
                f.write(response.content)

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
        synthesize_speech(fact_text, audio_path)
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
            logo_url = "https://storage.googleapis.com/trivia-videos-output/trivia_logo.png"
            logo_path = os.path.join(tmpdir, "trivia_logo.png")
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
        # Use provided fact or generate a new unique one
        fact_data = data.get("fact")
        if fact_data:
            fact = fact_data
            source_code = data.get("source_code", "X")
        else:
            fact, source_code = get_unique_fact()
        
        category = data.get("category") or infer_category_from_fact(fact)

        # Output path in GCS
        output_gcs_path = "gs://trivia-videos-output/output.mp4"
        video_gs_url, video_https_url = create_trivia_video(fact, output_gcs_path)

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
            source_code
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
