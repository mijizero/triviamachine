import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
from duckduckgo_search import ddg_images

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

from duckduckgo_search import DuckDuckGoSearch

def fetch_background_images(query, max_images=10):
    """Fetch image URLs from DuckDuckGo; fallback to empty list."""
    try:
        searcher = DuckDuckGoSearch()
        results = searcher.search_images(query, max_results=max_images)
        urls = [r["image"] for r in results if "image" in r]
        return urls
    except Exception:
        return []

# -------------------------------
# Core: Create Video
# -------------------------------

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create trivia video with TTS audio and crossfaded background slideshow."""
    import requests
    import tempfile
    from io import BytesIO
    from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory() as tmpdir:
        # Generate TTS audio
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = audio_clip.duration

        # Fetch images from DuckDuckGo
        image_urls = fetch_background_images(fact_text, max_images=10)

        # Download images, fallback to bucket background if empty
        img_clips = []
        if not image_urls:
            # Use single background
            bg_path = os.path.join(tmpdir, "background.jpg")
            bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.download_to_filename(bg_path)
            img = Image.open(bg_path).convert("RGB")
            img_clips.append(ImageClip(bg_path).set_duration(audio_duration))
        else:
            # Determine per-image duration
            per_img_dur = audio_duration / len(image_urls)
            for i, url in enumerate(image_urls):
                try:
                    resp = requests.get(url, timeout=5)
                    img = Image.open(BytesIO(resp.content)).convert("RGB")
                    img_path = os.path.join(tmpdir, f"bg_{i}.jpg")
                    img.save(img_path)
                    clip = ImageClip(img_path).set_duration(per_img_dur)
                    img_clips.append(clip)
                except Exception:
                    continue
            if not img_clips:
                # Fallback if all failed
                bg_path = os.path.join(tmpdir, "background.jpg")
                bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
                client = storage.Client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(blob_path)
                blob.download_to_filename(bg_path)
                img_clips.append(ImageClip(bg_path).set_duration(audio_duration))

        # Crossfade images
        video_clip = concatenate_videoclips(img_clips, method="compose").crossfadein(0.5).set_audio(audio_clip)

        # Overlay text (gold with black border)
        # Using first image size as reference
        ref_img = img_clips[0].img
        draw_img = Image.fromarray(ref_img)
        draw = ImageDraw.Draw(draw_img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 25)
        x_margin = int(draw_img.width * 0.1)
        max_width = int(draw_img.width * 0.8)
        pages = fact_text.split(".")  # simple sentence split
        page_text = " ".join(pages)  # keep text same
        bbox = draw.textbbox((0, 0), page_text, font=font)
        text_w, text_h = bbox[2]-bbox[0], bbox[3]-bbox[1]
        x = x_margin + (max_width - text_w) / 2
        y = (draw_img.height - text_h) / 2

        # Draw on a transparent overlay
        txt_clip = ImageClip(np.array(draw_img)).set_duration(audio_duration)
        video_clip = CompositeVideoClip([video_clip, txt_clip])

        # Export
        output_path = os.path.join(tmpdir, "output.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        bucket = storage.Client().bucket(bucket_name)
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
