import os
import tempfile
import random
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DDGS

app = Flask(__name__)

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
# Image Search (DuckDuckGo)
# -------------------------------

def fetch_background_images(query, limit=10):
    """Fetch up to 10 image URLs from DuckDuckGo based on the query."""
    try:
        with DDGS() as ddgs:
            results = [r["image"] for r in ddgs.images(query, max_results=limit)]
        return results
    except Exception as e:
        print(f"⚠️ Image fetch failed: {e}")
        return []

# -------------------------------
# Core: Create Video
# -------------------------------

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    import requests
    from io import BytesIO

    with tempfile.TemporaryDirectory() as tmpdir:
        client = storage.Client()

        # --- Generate TTS ---
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # --- Try fetching dynamic images ---
        image_urls = fetch_background_images(fact_text, limit=10)
        if not image_urls:
            # fallback: single GCS image
            print("⚠️ Using fallback background from GCS.")
            bg_path = os.path.join(tmpdir, "background.jpg")
            bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.download_to_filename(bg_path)
            image_urls = [f"file://{bg_path}"]

        # --- Download and prepare background slides ---
        images = []
        for idx, url in enumerate(image_urls):
            try:
                if url.startswith("file://"):
                    img = Image.open(url.replace("file://", ""))
                else:
                    resp = requests.get(url, timeout=10)
                    img = Image.open(BytesIO(resp.content))
                img = img.convert("RGB").resize((1920, 1080))
                img_path = os.path.join(tmpdir, f"bg_{idx}.jpg")
                img.save(img_path)
                images.append(img_path)
            except Exception as e:
                print(f"⚠️ Failed to load {url}: {e}")

        if not images:
            raise Exception("No usable background images found.")

        # --- Calculate duration per image ---
        per_image_duration = max(3, audio_duration / len(images))
        clips = []

        # --- Create slideshow with crossfade ---
        for i, img_path in enumerate(images):
            clip = ImageClip(img_path).set_duration(per_image_duration)
            if i > 0:
                clip = clip.crossfadein(1.0)
            clips.append(clip)

        slideshow = concatenate_videoclips(clips, method="compose").set_duration(audio_duration)

        # --- Prepare text overlay ---
        base_img = Image.open(images[0]).convert("RGB")
        draw = ImageDraw.Draw(base_img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 25)

        words = fact_text.split()
        max_width = base_img.width * 0.8
        lines, current = [], []
        for w in words:
            test = " ".join(current + [w])
            if draw.textbbox((0, 0), test, font=font)[2] < max_width:
                current.append(w)
            else:
                lines.append(" ".join(current))
                current = [w]
        if current:
            lines.append(" ".join(current))
        text = "\n".join(lines)

        txt_clip = (ImageClip(images[0])
                    .set_duration(audio_duration)
                    .set_opacity(0)
                    .on_color(size=(1920, 1080))
                    .set_position("center"))

        # Overlay the text
        txt_overlay = (ImageClip(base_img)
                       .set_duration(audio_duration)
                       .set_opacity(0)
                       .on_color(size=(1920, 1080))
                       .set_position("center"))

        # --- Composite video with audio ---
        final_video = CompositeVideoClip([slideshow], size=(1920, 1080))
        final_video = final_video.set_audio(audio_clip)

        # --- Export and upload ---
        output_path = os.path.join(tmpdir, "output.mp4")
        final_video.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        bucket = client.bucket(bucket_name)
        out_blob = bucket.blob(blob_path.replace("background.jpg", "output.mp4"))
        out_blob.upload_from_filename(output_path)

        return f"https://storage.googleapis.com/{bucket_name}/{out_blob.name}"

# -------------------------------
# Flask Endpoint
# -------------------------------

@app.route("/generate", methods=["POST"])
def generate_endpoint():
    try:
        data = request.get_json(silent=True) or {}

        fact = data.get("fact") or os.environ.get("DEFAULT_FACT") or \
            "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
        background_gcs_path = data.get("background") or os.environ.get("BACKGROUND_REF") or \
            "gs://trivia-videos-output/background.jpg"
        output_gcs_path = data.get("output") or os.environ.get("OUTPUT_GCS") or \
            "gs://trivia-videos-output/output.mp4"

        video_url = create_trivia_video(fact, background_gcs_path, output_gcs_path)
        return jsonify({"status": "ok", "fact": fact, "video_url": video_url})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
