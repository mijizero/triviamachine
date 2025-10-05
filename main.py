import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip
from PIL import Image, ImageDraw, ImageFont

# Updated DuckDuckGo search
from duckduckgo_search import DuckDuckGoSearch

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
    """Generate speech using Google Cloud Text-to-Speech (Neural2)."""
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

def fetch_duckduckgo_images(query, max_results=10):
    """Fetch image URLs from DuckDuckGo search."""
    try:
        ddgs = DuckDuckGoSearch()
        results = ddgs.search_images(query, max_results=max_results)
        return [r["image"] for r in results if "image" in r]
    except Exception:
        return []

def split_text_pages(draw, text, font, img_width, max_width_ratio=0.8):
    """Split text into pages that fit within max width."""
    max_width = img_width * max_width_ratio
    words = text.split()
    pages = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_width = bbox[2] - bbox[0]
        if line_width + 10 <= max_width:
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

# -------------------------------
# Core: Create Video
# -------------------------------

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create trivia video with TTS and changing backgrounds."""
    import requests
    import io

    with tempfile.TemporaryDirectory() as tmpdir:
        # Generate TTS audio
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Load fallback background
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        fallback_bg_path = os.path.join(tmpdir, "fallback.jpg")
        blob = bucket.blob(blob_path)
        blob.download_to_filename(fallback_bg_path)

        # Prepare font
        font = ImageFont.truetype("Roboto-Regular.ttf", 25)

        # Fetch images from DuckDuckGo
        query = fact_text.split(".")[0]  # simple query: first sentence
        urls = fetch_duckduckgo_images(query, max_results=20)
        if not urls:
            urls = [None]  # fallback

        # Determine number of slides based on 3s per slide
        num_slides = max(1, int(audio_duration // 3))
        urls = urls[:num_slides]

        clips = []
        for idx, url in enumerate(urls):
            if url:
                try:
                    resp = requests.get(url, timeout=5)
                    bg_img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                except Exception:
                    bg_img = Image.open(fallback_bg_path).convert("RGB")
            else:
                bg_img = Image.open(fallback_bg_path).convert("RGB")

            # Resize to 1920x1080
            bg_img = bg_img.resize((1920, 1080))
            draw = ImageDraw.Draw(bg_img)

            # Draw text centered
            pages = split_text_pages(draw, fact_text, font, img_width=1920)
            y_start = 540 - (len(pages) * 30) // 2
            for i, line in enumerate(pages):
                bbox = draw.textbbox((0, 0), line, font=font)
                text_w, text_h = bbox[2]-bbox[0], bbox[3]-bbox[1]
                x = (1920 - text_w)/2
                y = y_start + i*40
                draw.text(
                    (x, y),
                    line,
                    font=font,
                    fill="#FFD700",
                    stroke_width=3,
                    stroke_fill="black"
                )

            slide_path = os.path.join(tmpdir, f"slide_{idx}.png")
            bg_img.save(slide_path)
            clip = ImageClip(slide_path).set_duration(3)
            clips.append(clip)

        # Adjust last clip duration to match audio
        total_clip_dur = sum(c.duration for c in clips)
        if total_clip_dur < audio_duration:
            clips[-1] = clips[-1].set_duration(clips[-1].duration + (audio_duration - total_clip_dur))

        video_clip = concatenate_videoclips(clips, method="compose").set_audio(audio_clip)

        output_path = os.path.join(tmpdir, "output.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
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
