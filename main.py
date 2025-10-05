import os
import tempfile
import re
import subprocess
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import VideoFileClip, ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import DDGS  # open web search (no API key)

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

def split_text_pages(draw, text, font, img_width, max_width_ratio=0.8):
    """Split text into pages that fit within 80% width of the screen."""
    max_width = img_width * max_width_ratio
    words = text.split()
    pages, current_line = [], []

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
    return pages

# -------------------------------
# Open-Web Video Fetcher (No API)
# -------------------------------

def fetch_relevant_videos(keyword, limit=2, tmpdir="/tmp"):
    """
    Searches the web for videos (no API key required) and downloads short clips using yt-dlp.
    Returns list of local video paths.
    """
    video_paths = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.videos(keyword, max_results=limit))
        for idx, r in enumerate(results[:limit]):
            url = r.get("content", "")
            if "youtube" not in url.lower():
                continue
            local_path = os.path.join(tmpdir, f"video_{idx}.mp4")
            subprocess.run([
                "yt-dlp", "-f", "mp4", "--quiet", "--no-warnings",
                "--max-filesize", "15M", "-o", local_path, url
            ], check=False)
            if os.path.exists(local_path):
                video_paths.append(local_path)
    except Exception as e:
        print("Video fetch error:", e)
    return video_paths

# -------------------------------
# Core: Create Video
# -------------------------------

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create trivia video using dynamic videos + synced text & TTS."""
    from google.cloud import storage
    from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Generate TTS audio
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Font + layout
        font = ImageFont.truetype("Roboto-Regular.ttf", 25)
        dummy_img = Image.new("RGB", (1920, 1080))
        draw = ImageDraw.Draw(dummy_img)
        pages = split_text_pages(draw, fact_text, font, dummy_img.width, 0.8)

        num_pages = len(pages)
        per_page_dur = audio_duration / num_pages

        # Extract 2–3 main keywords for video fetching
        keywords = re.findall(r"[A-Za-z]+", fact_text)
        main_kw = " ".join(keywords[:3]) if keywords else "trivia facts"
        video_paths = fetch_relevant_videos(main_kw, limit=3, tmpdir=tmpdir)
        if not video_paths:
            # fallback to background image (if no videos found)
            bg_path = os.path.join(tmpdir, "background.jpg")
            bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.download_to_filename(bg_path)
            video_clip = ImageClip(bg_path).set_duration(audio_duration)
        else:
            # use found clips as background
            video_clips = []
            for p in video_paths:
                clip = VideoFileClip(p).without_audio()
                if clip.duration > per_page_dur * num_pages:
                    clip = clip.subclip(0, per_page_dur * num_pages)
                video_clips.append(clip)
            video_clip = concatenate_videoclips(video_clips).set_duration(audio_duration)

        # --- Text overlays per page ---
        overlay_clips = []
        for i, page in enumerate(pages):
            start_time = max(0, i * per_page_dur - 0.3)
            end_time = (i + 1) * per_page_dur
            dur = end_time - start_time

            # Create semi-transparent overlay
            txt_img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
            d = ImageDraw.Draw(txt_img)
            bbox = d.textbbox((0, 0), page, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (1920 - text_w) / 2
            y = (1080 - text_h) / 2

            # semi-transparent background rectangle
            rect_x1, rect_y1 = x - 30, y - 20
            rect_x2, rect_y2 = x + text_w + 30, y + text_h + 20
            d.rectangle([rect_x1, rect_y1, rect_x2, rect_y2], fill=(0, 0, 0, 160))

            # gold text with black border
            d.text((x, y), page, font=font, fill="#FFD700", stroke_width=3, stroke_fill="black")

            txt_path = os.path.join(tmpdir, f"text_{i}.png")
            txt_img.save(txt_path)

            clip = ImageClip(txt_path).set_start(start_time).set_duration(dur)
            overlay_clips.append(clip)

        final = CompositeVideoClip([video_clip, *overlay_clips]).set_audio(audio_clip)

        # Output
        output_path = os.path.join(tmpdir, "output.mp4")
        final.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        client = storage.Client()
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
            "Lewis Hamilton joins Ferrari for the 2025 Formula 1 season. The seven-time world champion will race in red next year."
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
