import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import (
    ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips, concatenate_videoclips
)
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DDGS

app = Flask(__name__)

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
    """Generate speech using Google Cloud Text-to-Speech (Neural2) with excited Australian voice."""
    client = texttospeech.TextToSpeechClient()

    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name="en-AU-Neural2-D",  # Australian male Neural2
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


def fetch_images_from_web(query, num_images=10):
    """Fetch image URLs from DuckDuckGo for given query."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=num_images))
        urls = [r["image"] for r in results if "image" in r]
        return urls
    except Exception as e:
        print("DuckDuckGo fetch failed:", e)
        return []


def download_image(url, path):
    """Download image from URL safely."""
    import requests
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except Exception as e:
        print(f"Failed to download {url}: {e}")
    return False


# -------------------------------
# Core: Create Video
# -------------------------------

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create trivia video with slideshow background and static text."""
    import re
    import tempfile
    from google.cloud import storage
    from moviepy.editor import (
        ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips, crossfadein, crossfadeout
    )
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory() as tmpdir:
        client = storage.Client()

        # --- Step 1: Generate audio ---
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # --- Step 2: Try to fetch web images ---
        query = fact_text.split(".")[0][:60]  # use the first sentence as topic
        urls = fetch_images_from_web(query)
        img_paths = []

        for i, url in enumerate(urls[:15]):  # cap at 15
            img_path = os.path.join(tmpdir, f"img_{i}.jpg")
            if download_image(url, img_path):
                img_paths.append(img_path)

        # --- Step 3: Fallback to default background if no images found ---
        if not img_paths:
            bg_path = os.path.join(tmpdir, "background.jpg")
            bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.download_to_filename(bg_path)
            img_paths = [bg_path]

        # --- Step 4: Prepare slideshow with crossfade ---
        per_img_duration = 3.0
        total_needed = int(audio_duration // per_img_duration) + 1
        slideshow_imgs = (img_paths * ((total_needed // len(img_paths)) + 1))[:total_needed]

        clips = []
        for i, path in enumerate(slideshow_imgs):
            clip = ImageClip(path).set_duration(per_img_duration)
            if i > 0:
                clip = clip.crossfadein(1)
            clips.append(clip)

        slideshow = concatenate_videoclips(clips, method="compose").set_duration(audio_duration)

        # --- Step 5: Add static text overlay ---
        font = ImageFont.truetype("Roboto-Regular.ttf", 40)
        example_img = Image.open(slideshow_imgs[0]).convert("RGB")
        txt_clip_path = os.path.join(tmpdir, "text.png")

        # Create transparent text overlay
        overlay = Image.new("RGBA", example_img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Draw semi-transparent rounded rectangle behind text
        text = fact_text
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (overlay.width - text_w) / 2
        y = (overlay.height - text_h) / 2

        padding = 30
        rect_x0 = x - padding
        rect_y0 = y - padding
        rect_x1 = x + text_w + padding
        rect_y1 = y + text_h + padding
        draw.rounded_rectangle(
            [rect_x0, rect_y0, rect_x1, rect_y1],
            radius=30,
            fill=(0, 0, 0, 160)
        )

        # Draw gold text with black outline
        draw.text((x, y), text, font=font, fill="#FFD700", stroke_width=3, stroke_fill="black")

        overlay.save(txt_clip_path, "PNG")

        text_clip = (
            ImageClip(txt_clip_path, transparent=True)
            .set_duration(audio_duration)
            .set_position("center")
        )

        final = CompositeVideoClip([slideshow, text_clip]).set_audio(audio_clip)

        # --- Step 6: Export and upload ---
        output_path = os.path.join(tmpdir, "output.mp4")
        final.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

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
