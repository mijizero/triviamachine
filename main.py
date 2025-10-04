import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
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
    """Generate speech using Google Cloud TTS."""
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-C"
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        pitch=0.0
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    with open(output_path, "wb") as f:
        f.write(response.audio_content)

def split_text_into_pages(text, draw, font, max_width_ratio=0.8, img_width=1920):
    """Split text into pages that fit 80% width."""
    max_width = img_width * max_width_ratio
    words = text.split()
    pages = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0,0), test_line, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current_line.append(word)
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
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # Load background image to get dimensions
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font_path = "Roboto-Regular.ttf"
        font_size = 60
        font = ImageFont.truetype(font_path, font_size)

        # Split text into pages
        pages = split_text_into_pages(fact_text, draw, font, img_width=img.width)

        clips = []

        for i, page_text in enumerate(pages):
            # Synthesize TTS for this page
            audio_path = os.path.join(tmpdir, f"audio_{i}.mp3")
            synthesize_speech(page_text, audio_path)
            audio_clip = AudioFileClip(audio_path)
            duration = audio_clip.duration

            # Create image for this page
            page_img = img.copy()
            page_draw = ImageDraw.Draw(page_img)
            bbox = page_draw.textbbox((0,0), page_text, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            x = (page_img.width - w) / 2
            y = (page_img.height - h) / 2
            page_draw.text((x, y), page_text, font=font, fill="white")

            # Save annotated image
            annotated_path = os.path.join(tmpdir, f"annotated_{i}.jpg")
            page_img.save(annotated_path)

            # Create video clip
            clip = ImageClip(annotated_path, duration=duration)
            clip = clip.set_audio(audio_clip)
            clips.append(clip)

        # Concatenate all clips
        final_clip = concatenate_videoclips(clips)
        output_path = os.path.join(tmpdir, "output.mp4")
        final_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        return upload_to_gcs(output_path, output_gcs_path)

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
