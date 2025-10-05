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
    """Create trivia video with precise TTS-synced pages (font 25, 80% width)."""
    import tempfile
    import os
    from PIL import Image, ImageDraw, ImageFont
    from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
    from google.cloud import storage

    with tempfile.TemporaryDirectory() as tmpdir:
        # -----------------------------
        # Download background
        # -----------------------------
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # -----------------------------
        # Generate full TTS audio (one file)
        # -----------------------------
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # -----------------------------
        # Load background and text settings
        # -----------------------------
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font_path = "Roboto-Regular.ttf"
        font_size = 25
        font = ImageFont.truetype(font_path, font_size)
        max_width = img.width * 0.8          # 80% usable width
        x_margin = img.width * 0.1          # 10% left/right margin

        # -----------------------------
        # Split text into pages (width-limited, greedy)
        # -----------------------------
        words = fact_text.split()
        pages = []
        current = []
        for w in words:
            test = " ".join(current + [w]).strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                current.append(w)
            else:
                # if current empty, force the long word onto a page
                if not current:
                    pages.append(w)
                else:
                    pages.append(" ".join(current))
                    current = [w]
        if current:
            pages.append(" ".join(current))

        if len(pages) == 0:
            pages = [fact_text]

        # -----------------------------
        # Compute exact page start times and durations (word-proportional)
        # -----------------------------
        total_words = len(words)
        cumulative_words = 0
        page_infos = []  # list of (page_text, start_nominal, end_nominal, duration_nominal)
        for i, page in enumerate(pages):
            page_words = len(page.split())
            start_nominal = (cumulative_words / total_words) * audio_duration
            duration_nominal = (page_words / total_words) * audio_duration
            end_nominal = start_nominal + duration_nominal
            cumulative_words += page_words
            page_infos.append([page, start_nominal, end_nominal, duration_nominal])

        # Ensure last page ends exactly at audio end (avoid trailing)
        if page_infos:
            page_infos[-1][2] = audio_duration
            page_infos[-1][3] = page_infos[-1][2] - page_infos[-1][1]
            if page_infos[-1][3] < 0.05:
                page_infos[-1][3] = max(0.5, page_infos[-1][3])  # fallback min duration

        # -----------------------------
        # Build clips with precise start times (apply small lead so text appears slightly early)
        # -----------------------------
        clips = []
        for i, (page_text, start_nominal, end_nominal, duration_nominal) in enumerate(page_infos):
            # adaptive lead: small fraction of nominal duration, capped to 0.25s
            lead = min(0.25, duration_nominal * 0.25)
            start_time = max(0.0, start_nominal - lead)
            # clip should end at end_nominal (so it spans until nominal end)
            duration = end_nominal - start_time
            if duration <= 0:
                # safety fallback
                duration = max(0.3, duration_nominal)

            # create page image
            img_page = img.copy()
            draw_page = ImageDraw.Draw(img_page)
            bbox = draw_page.textbbox((0, 0), page_text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = x_margin + (max_width - text_w) / 2
            y = (img_page.height - text_h) / 2

            draw_page.text(
                (x, y),
                page_text,
                font=font,
                fill="#FFD700",       # Gold
                stroke_width=3,
                stroke_fill="black"
            )

            img_path = os.path.join(tmpdir, f"page_{i}.png")
            img_page.save(img_path)

            clip = ImageClip(img_path).set_start(start_time).set_duration(duration)
            clips.append(clip)

        # -----------------------------
        # Composite clips so overlapping start times are respected,
        # set continuous audio and force total duration = audio_duration
        # -----------------------------
        if clips:
            composite = CompositeVideoClip(clips, size=(img.width, img.height))
            composite = composite.set_duration(audio_duration)
            composite = composite.set_audio(audio_clip)
        else:
            # fallback: single still with audio
            single = ImageClip(bg_path).set_duration(audio_duration).set_audio(audio_clip)
            composite = single

        # -----------------------------
        # Write final video and upload
        # -----------------------------
        output_path = os.path.join(tmpdir, "output.mp4")
        composite.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        out_blob = client.bucket(bucket_name).blob(blob_path.replace("background.jpg", "output.mp4"))
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
