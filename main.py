import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DDGS
import requests
import random
import vertexai
from vertexai.generative_models import GenerativeModel

app = Flask(__name__)

# -------------------------------
# Dynamic Fact
# -------------------------------
import random
import requests
import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init(project=os.getenv("trivia-machine-472207"), location="asia-southeast1")

def get_dynamic_fact():
    """Randomly choose a trivia source (WikiData or Gemini) and return a 3-sentence 'Did you know' fact with supporting context."""
    source = random.choice([1, 2, 3, 4])

    def gemini_fact(prompt):
        model = GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip()

    # -------------------------------
    # 1. Wikipedia → Gemini Rewrite
    # -------------------------------
    if source == 1:
        try:
            res = requests.get("https://en.wikipedia.org/api/rest_v1/page/random/summary", timeout=10)
            data = res.json()
            title = data.get("title", "")
            extract = data.get("extract", "")
            wiki_text = f"{title}: {extract}"

            # Pass it to Gemini for a polished 3-sentence fact
            prompt = (
                "Rewrite the following Wikipedia summary into a 3-sentence trivia fact. "
                "Start with 'Did you know', then add 2 supporting sentences that give background or interesting details.\n\n"
                f"Summary: {wiki_text}"
            )
            return gemini_fact(prompt)
        except Exception:
            pass

    # -------------------------------
    # 2. Gemini – Technology
    # -------------------------------
    if source == 2:
        prompt = (
            "Give one factual and engaging piece of technology trivia in 3 sentences. "
            "Sentence 1 must start with 'Did you know'. "
            "Sentences 2 and 3 should add interesting details or background."
        )
        return gemini_fact(prompt)

    # -------------------------------
    # 3. Gemini – Science/History/Culture
    # -------------------------------
    if source == 3:
        prompt = (
            "Give one true and engaging trivia about science, history, or culture in 3 sentences. "
            "Start with 'Did you know', then add 2 supporting sentences with factual context or significance."
        )
        return gemini_fact(prompt)

    # -------------------------------
    # 4. Gemini – Trending Media/News
    # -------------------------------
    if source == 4:
        prompt = (
            "Give one short, factual trivia about trending media, movies, or celebrities in 3 sentences. "
            "The first must start with 'Did you know'. "
            "The next 2 sentences should give interesting supporting info or context."
        )
        return gemini_fact(prompt)

    # -------------------------------
    # Fallback
    # -------------------------------
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
    """Extract a clean and relevant search keyword/phrase from the generated fact text."""
    # Clean up the fact text to make it image-search friendly
    fact_clean = fact_text.replace("Did you know", "").replace("did you know", "").replace("?", "")
    fact_clean = fact_clean.strip()

    # Ask Gemini to summarize this into a 2-5 word subject keyword
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
        # If Gemini ever outputs a long explanation, fallback to first 5 words of fact
        if len(text.split()) > 6:
            text = " ".join(fact_clean.split()[:5])
        return text or fact_clean
    except Exception:
        return fact_clean

# -------------------------------
# Helpers
# -------------------------------

def upload_to_gcs(local_path, gcs_path):
    """Upload file to GCS and return public URL."""
    client = storage.Client()
    bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

def synthesize_speech(text, output_path):
    """Generate speech using Google Cloud Text-to-Speech."""
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
# Core: Create Video
# -------------------------------
def create_trivia_video(fact_text, output_gcs_path):
    """Create Shorts-format trivia video with DuckDuckGo background, TTS audio, gold text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Determine better image search query via Gemini ---
        search_query = extract_search_query(fact_text)

        # --- Fetch background from DuckDuckGo safely ---
        bg_path = os.path.join(tmpdir, "background.jpg")
        valid_image = False

        with DDGS() as ddgs:
            results = list(ddgs.images(search_query, max_results=1))

        if results:
            img_url = results[0].get("image")
            try:
                response = requests.get(img_url, stream=True, timeout=10)
                if response.status_code == 200 and "image" in response.headers.get("Content-Type", ""):
                    with open(bg_path, "wb") as f:
                        for chunk in response.iter_content(8192):
                            f.write(chunk)
                    valid_image = True
            except Exception:
                valid_image = False

        if not valid_image:
            # fallback background
            fallback_url = "https://storage.googleapis.com/trivia-videos-output/background.jpg"
            response = requests.get(fallback_url)
            with open(bg_path, "wb") as f:
                f.write(response.content)

        # --- Resize / crop to YouTube Shorts (1080x1920) ---
        target_size = (1080, 1920)
        img = Image.open(bg_path).convert("RGB")
        img_ratio = img.width / img.height
        target_ratio = target_size[0] / target_size[1]

        if img_ratio > target_ratio:
            # too wide → crop sides
            new_width = int(img.height * target_ratio)
            left = (img.width - new_width) // 2
            right = left + new_width
            img = img.crop((left, 0, right, img.height))
        else:
            # too tall → crop top/bottom
            new_height = int(img.width / target_ratio)
            top = (img.height - new_height) // 2
            bottom = top + new_height
            img = img.crop((0, top, img.width, bottom))

        img = img.resize(target_size, Image.LANCZOS)
        bg_path = os.path.join(tmpdir, "background_resized.jpg")
        img.save(bg_path)

        # --- Generate TTS ---
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # --- Prepare text ---
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 50)
        x_margin = int(img.width * 0.1)
        max_width = int(img.width * 0.8)

        # Split text into pages
        words = fact_text.split()
        pages = []
        current_line = []
        for word in words:
            test_line = " ".join(current_line + [word])
            bbox = draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_width:
                current_line.append(word)
            else:
                pages.append(" ".join(current_line))
                current_line = [word]
        if current_line:
            pages.append(" ".join(current_line))

        num_pages = len(pages)
        per_page_dur = audio_duration / num_pages

        # --- Build clips ---
        clips = []
        for i, txt in enumerate(pages):
            dur = max(0.5, per_page_dur)
            page_img = img.copy()
            draw_page = ImageDraw.Draw(page_img)
            bbox = draw_page.textbbox((0, 0), txt, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (page_img.width - text_w) / 2
            y = (page_img.height - text_h) / 2

            draw_page.text(
                (x, y),
                txt,
                font=font,
                fill="#FFD700",
                stroke_width=4,
                stroke_fill="black",
            )

            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img.save(page_path)
            clip = ImageClip(page_path).set_duration(dur)
            clips.append(clip)

        video_clip = concatenate_videoclips(clips).set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "output.mp4")
        video_clip.write_videofile(
            output_path,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            threads=4,
            preset="ultrafast",
        )

        # --- Upload to GCS ---
        client = storage.Client()
        bucket_name, blob_path = output_gcs_path.replace("gs://", "").split("/", 1)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(output_path)

        return f"https://storage.googleapis.com/{bucket_name}/{blob.name}"

# -------------------------------
# Flask Endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        fact = data.get("fact") or get_dynamic_fact()
        output_gcs_path = data.get("output") or os.environ.get("OUTPUT_GCS") or \
            "gs://trivia-videos-output/output.mp4"

        video_url = create_trivia_video(fact, output_gcs_path)
        return jsonify({"status": "ok", "fact": fact, "video_url": video_url})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
