import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DuckDuckGo
import requests

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
    """Create trivia video with dynamic DuckDuckGo background, TTS audio, gold text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Fetch background from DuckDuckGo ---
        searcher = DuckDuckGo()
        results = searcher.images(fact_text, max_results=1)
        if results:
            img_url = results[0]["image"]
            response = requests.get(img_url)
            bg_path = os.path.join(tmpdir, "background.jpg")
            with open(bg_path, "wb") as f:
                f.write(response.content)
        else:
            # fallback background
            bg_path = os.path.join(tmpdir, "background.jpg")
            fallback_url = "https://storage.googleapis.com/trivia-videos-output/background.jpg"
            response = requests.get(fallback_url)
            with open(bg_path, "wb") as f:
                f.write(response.content)

        # Generate TTS
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Prepare image + drawing
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 25)
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

        # Build clips
        clips = []
        for i, txt in enumerate(pages):
            dur = max(0.3, per_page_dur)
            page_img = img.copy()
            draw_page = ImageDraw.Draw(page_img)
            bbox = draw_page.textbbox((0, 0), txt, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = x_margin + (max_width - text_w) / 2
            y = (page_img.height - text_h) / 2

            # Gold text with black outline
            draw_page.text(
                (x, y), txt, font=font,
                fill="#FFD700", stroke_width=3, stroke_fill="black"
            )

            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img.save(page_path)
            clip = ImageClip(page_path).set_duration(dur)
            clips.append(clip)

        # Combine into final video
        video_clip = CompositeVideoClip(clips).set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "output.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
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
        fact = data.get("fact") or os.environ.get("DEFAULT_FACT") or \
            "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
        output_gcs_path = data.get("output") or os.environ.get("OUTPUT_GCS") or \
            "gs://trivia-videos-output/output.mp4"

        video_url = create_trivia_video(fact, output_gcs_path)
        return jsonify({"status": "ok", "fact": fact, "video_url": video_url})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
