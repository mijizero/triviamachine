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
        speaking_rate=1.0,  # faster for excitement
        pitch=2.0,          # higher pitch
        volume_gain_db=2.0
    )

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    with open(output_path, "wb") as out:
        out.write(response.audio_content)

def split_text_into_pages(text, draw, font, max_width_ratio=0.8, img_width=1920):
    """Split text into pages that fit 80% width dynamically."""
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
    """Create trivia video with TTS-synced pages and gold text with black border."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # Generate TTS audio for the whole fact
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Load background image
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Font setup
        font_path = "Roboto-Regular.ttf"
        font_size = 25
        font = ImageFont.truetype(font_path, font_size)

        # Split text into pages that fit 80% width
        pages = split_text_pages(draw, fact_text, font, img.width, max_width_ratio=0.8)
        total_words = len(fact_text.split())
        total_pages = len(pages)

        # Calculate per-page durations proportional to number of words
        page_durations = []
        word_idx = 0
        words = fact_text.split()
        for page in pages:
            num_words = len(page.split())
            duration = (num_words / total_words) * audio_duration
            page_durations.append(duration)
            word_idx += num_words

        # Adjust last page duration to match audio exactly
        page_start_times = [0.0]
        for i in range(1, total_pages):
            page_start_times.append(page_start_times[i-1] + page_durations[i-1])
        page_durations[-1] = audio_duration - page_start_times[-1]

        # Create video clips for each page
        clips = []
        x_margin = img.width * 0.1  # 10% margin on left/right
        max_width = img.width * 0.8

        for idx, page in enumerate(pages):
            img_page = img.copy()
            draw_page = ImageDraw.Draw(img_page)

            # Center text within 80% width
            bbox = draw_page.textbbox((0,0), page, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = x_margin + (max_width - text_width)/2
            y = (img_page.height - text_height)/2

            # Draw gold text with thick black border
            draw_page.text(
                (x, y),
                page,
                font=font,
                fill="#FFD700",       # Gold
                stroke_width=3,
                stroke_fill="black"
            )

            annotated_path = os.path.join(tmpdir, f"page_{idx}.jpg")
            img_page.save(annotated_path)

            clip = ImageClip(annotated_path, duration=page_durations[idx])
            clips.append(clip)

        # Concatenate page clips
        video_clip = concatenate_videoclips(clips)
        video_clip = video_clip.set_audio(audio_clip)

        # Write final video
        output_path = os.path.join(tmpdir, "output.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path.replace("background.jpg", "output.mp4"))
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
