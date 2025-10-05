import os
import tempfile
import requests
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

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
        name="en-AU-Neural2-D",  # Australian female Neural2
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

def split_text_pages(draw, text, font, img_width, max_width_ratio=0.8):
    """Split text into pages that fit within max_width_ratio of image width."""
    max_width = img_width * max_width_ratio
    words = text.split()
    pages = []
    current_line = []

    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_width = bbox[2] - bbox[0]
        if line_width + 10 <= max_width:  # buffer for stroke
            current_line.append(word)
        else:
            if not current_line:
                pages.append(word)
            else:
                pages.append(" ".join(current_line))
                current_line = [word]

    if current_line:
        pages.append(" ".join(current_line))

    return pages

def fetch_duckduckgo_images(query, max_results=10):
    """Fetch image URLs from DuckDuckGo without external packages."""
    search_url = "https://duckduckgo.com/i.js"
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {"q": query, "ia": "images", "iax": "images"}
    try:
        res = requests.get(search_url, params=params, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        urls = [item["image"] for item in data.get("results", [])]
        return urls[:max_results]
    except Exception:
        return []

# -------------------------------
# Core: Create Video
# -------------------------------
def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create trivia video with continuous TTS and gold text with black border, timed to TTS."""
    import tempfile
    from google.cloud import storage
    from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory() as tmpdir:
        # Fetch 5-10 images from DuckDuckGo
        bg_images = fetch_duckduckgo_images(fact_text, max_results=10)

        # Fallback to single GCS background if none found
        if not bg_images:
            bg_path = os.path.join(tmpdir, "background.jpg")
            bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.download_to_filename(bg_path)
            bg_images = [bg_path]

        # Generate full TTS audio
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Prepare font
        font_path = "Roboto-Regular.ttf"
        font_size = 25

        # Calculate per-image duration
        num_images = len(bg_images)
        per_image_dur = max(3, audio_duration / num_images)

        clips = []
        for i, bg in enumerate(bg_images):
            # Download remote image if URL
            if bg.startswith("http"):
                resp = requests.get(bg, stream=True, timeout=10)
                img_path = os.path.join(tmpdir, f"bg_{i}.jpg")
                with open(img_path, "wb") as f:
                    for chunk in resp.iter_content(1024):
                        f.write(chunk)
            else:
                img_path = bg

            img = Image.open(img_path).convert("RGB")
            draw = ImageDraw.Draw(img)
            font = ImageFont.truetype(font_path, font_size)

            # Draw centered text
            pages = split_text_pages(draw, fact_text, font, img.width)
            txt = " ".join(pages)
            bbox = draw.textbbox((0,0), txt, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (img.width - text_w) / 2
            y = (img.height - text_h) / 2
            draw.text((x, y), txt, font=font, fill="#FFD700", stroke_width=3, stroke_fill="black")

            page_path = os.path.join(tmpdir, f"page_{i}.png")
            img.save(page_path)

            clip = ImageClip(page_path).set_duration(per_image_dur)
            clips.append(clip)

        # Crossfade between clips
        final_clip = concatenate_videoclips(clips, method="compose").set_audio(audio_clip)

        # Export video
        output_path = os.path.join(tmpdir, "output.mp4")
        final_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        bucket_name, blob_path = output_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        out_blob = bucket.blob(blob_path)
        out_blob.upload_from_filename(output_path)

        return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

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
