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

def split_text_pages(draw, text, font, img_width, max_width_ratio=0.8):
    """
    Split text into pages that fit within max_width_ratio of image width.
    Greedy approach: add words until it exceeds max width, then start a new page.
    """
    max_width = img_width * max_width_ratio
    words = text.split()
    pages = []
    current_line = []

    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_width = bbox[2] - bbox[0]
        # include extra margin for stroke
        if line_width + 10 <= max_width:  # 10 px buffer for stroke
            current_line.append(word)
        else:
            # if current_line is empty (word itself too long), force it in
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
    """Create trivia video with continuous TTS and gold text with black border, timed to TTS."""
    import re
    import tempfile
    from google.cloud import storage
    from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # Generate full TTS audio
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Prepare image + drawing
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 25)

        # Set margins and layout
        x_margin = int(img.width * 0.1)
        max_width = int(img.width * 0.8)

        # --- Split text into pages ---
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

        # Assign rough timestamps per page
        page_infos = []
        for i, txt in enumerate(pages):
            start = i * per_page_dur
            end = (i + 1) * per_page_dur
            page_infos.append((txt, start, end))

        # --- Build page clips with improved sync ---
        clips = []
        for i, (txt, start, end) in enumerate(page_infos):
            dur = max(0.3, end - start)

            # 👇 Small smarter lead adjustment
            if i == len(page_infos) - 1:
                start_time = max(0, start - 0.5)   # last page: 0.5 s earlier
            else:
                start_time = max(0, start - 0.3)   # others: 0.3 s earlier

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

            clip = ImageClip(page_path).set_start(start_time).set_duration(dur)
            clips.append(clip)

        # Combine and export
        video_clip = CompositeVideoClip(clips).set_audio(audio_clip)
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
            "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
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
