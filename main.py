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
    """Create trivia video with near-perfect TTS sync (last page included)."""
    import tempfile, os
    from PIL import Image, ImageDraw, ImageFont
    from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
    from google.cloud import storage

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Download background ---
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # --- Generate full TTS ---
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        # More reliable than audio_clip.duration
        audio_duration = float(audio_clip.end or audio_clip.duration)

        # --- Prepare drawing setup ---
        img = Image.open(bg_path).convert("RGB")
        font_path = "Roboto-Regular.ttf"
        font_size = 25
        font = ImageFont.truetype(font_path, font_size)
        draw = ImageDraw.Draw(img)
        max_width = img.width * 0.8
        x_margin = img.width * 0.1

        # --- Split text into width-limited pages ---
        words = fact_text.split()
        pages, current = [], []
        for w in words:
            test = " ".join(current + [w])
            bbox = draw.textbbox((0, 0), test, font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                current.append(w)
            else:
                pages.append(" ".join(current))
                current = [w]
        if current:
            pages.append(" ".join(current))
        if not pages:
            pages = [fact_text]

        # --- Compute start times & durations per page ---
        total_words = len(words)
        cumulative = 0
        page_infos = []
        for i, p in enumerate(pages):
            wc = len(p.split())
            start = (cumulative / total_words) * audio_duration
            end = ((cumulative + wc) / total_words) * audio_duration
            page_infos.append([p, start, end])
            cumulative += wc

        # --- Fix: trim last page 0.2s earlier to match speech end ---
        page_infos[-1][2] = max(0, audio_duration - 0.2)

        # --- Build page clips ---
        clips = []
        for i, (txt, start, end) in enumerate(page_infos):
            dur = max(0.3, end - start)
            # Small lead so text appears just before speech
            start_time = max(0, start - 0.25)

            page_img = img.copy()
            draw_page = ImageDraw.Draw(page_img)
            bbox = draw_page.textbbox((0, 0), txt, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = x_margin + (max_width - text_w) / 2
            y = (page_img.height - text_h) / 2
            draw_page.text(
                (x, y), txt, font=font,
                fill="#FFD700", stroke_width=3, stroke_fill="black"
            )
            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img.save(page_path)

            clips.append(
                ImageClip(page_path).set_start(start_time).set_duration(dur)
            )

        # --- Combine ---
        composite = CompositeVideoClip(clips, size=(img.width, img.height))
        composite = composite.set_audio(audio_clip).set_duration(audio_duration)
        out_path = os.path.join(tmpdir, "output.mp4")
        composite.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac")

        # --- Upload output ---
        out_blob = client.bucket(bucket_name).blob(
            blob_path.replace("background.jpg", "output.mp4")
        )
        out_blob.upload_from_filename(out_path)
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
