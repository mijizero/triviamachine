import os
import re
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify
from google.cloud import storage, texttospeech
from moviepy.editor import (
    ImageClip, VideoFileClip, AudioFileClip, CompositeVideoClip,
    concatenate_videoclips, concatenate_videoclips as concat_clips
)
from PIL import Image, ImageDraw, ImageFont

# duckduckgo_search is used for open-web searching (no API keys)
# yt-dlp is used to download YouTube / public video links
from duckduckgo_search import DDGS

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
        name="en-AU-Neural2-D",  # Australian voice; change if unavailable
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
# Video fetch helpers (open web, no API key)
# -------------------------------

def ddg_video_search(query, max_results=5):
    """Return list of video URLs from DuckDuckGo video search (if available)."""
    urls = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.videos(query, max_results=max_results):
                # ddg.videos returns dicts with 'content' field usually containing the video URL
                url = r.get("content") or r.get("url") or r.get("href")
                if url:
                    urls.append(url)
    except Exception as e:
        print("DDG video search error:", e)
    return urls

def ddg_image_search(query, max_results=6):
    """Return list of image URLs from DuckDuckGo image search as fallback."""
    urls = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.images(query, max_results=max_results):
                url = r.get("image") or r.get("thumbnail") or r.get("url")
                if url:
                    urls.append(url)
    except Exception as e:
        print("DDG image search error:", e)
    return urls

def download_with_requests(url, dest_path, timeout=30):
    """Download a URL (images or direct video) into dest_path using requests."""
    try:
        resp = requests.get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024*32):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"download_with_requests failed for {url}: {e}")
        return False

def download_video_via_ytdlp(url, dest_path, tmpdir):
    """Attempt to download a usable mp4 via yt-dlp. Returns True on success."""
    try:
        # prefer mp4, reasonable size
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio/best",
            "-o", dest_path,
            url
        ]
        # run in temporary dir to avoid clutter
        subprocess.run(cmd, cwd=tmpdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(dest_path)
    except Exception as e:
        print("yt-dlp download failed:", e)
        return False

# -------------------------------
# Core: Create Video (updated)
# -------------------------------
def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create trivia video with dynamic fact-matched background (video when possible; slideshow fallback)."""
    from google.cloud import storage
    from moviepy.editor import VideoFileClip, ImageClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips

    with tempfile.TemporaryDirectory() as tmpdir:
        # -----------------------------
        # Synthesize full TTS audio (single file)
        # -----------------------------
        audio_path = os.path.join(tmpdir, "audio.mp3")
        synthesize_speech(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)
        audio_duration = float(getattr(audio_clip, "end", audio_clip.duration))

        # -----------------------------
        # Prepare font / measured canvas
        # -----------------------------
        # Use the provided Roboto file in the container, or fallback to default
        font_path = "Roboto-Regular.ttf"
        if not os.path.exists(font_path):
            # try common system path or fallback
            font_path = "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf" if os.path.exists("/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf") else None

        if font_path:
            font = ImageFont.truetype(font_path, 25)
        else:
            font = ImageFont.load_default()

        # create a dummy image to measure split widths
        # choose HD canvas to match final rendering (1920x1080)
        canvas_w, canvas_h = 1920, 1080
        dummy = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
        draw = ImageDraw.Draw(dummy)

        # -----------------------------
        # Split text into pages (width-limited)
        # -----------------------------
        pages = split_text_pages(draw, fact_text, font, canvas_w, max_width_ratio=0.8)
        if not pages:
            pages = [fact_text]

        # compute per-page nominal start/end by word-proportions (keeps earlier behavior)
        words = fact_text.split()
        total_words = len(words) or 1
        page_infos = []
        cumulative = 0.0
        for p in pages:
            wc = len(p.split())
            start_nom = (cumulative / total_words) * audio_duration
            end_nom = ((cumulative + wc) / total_words) * audio_duration
            page_infos.append({"text": p, "start": start_nom, "end": end_nom, "wc": wc})
            cumulative += wc
        # ensure last page ends almost with audio
        page_infos[-1]["end"] = max(0.0, audio_duration - 0.05)
        for p in page_infos:
            p["duration"] = max(0.3, p["end"] - p["start"])

        # -----------------------------
        # Decide search query for the entire fact
        # -----------------------------
        # derive a compact query from prominent words (first nouns / capitalized groups)
        # simple heuristic: take up to first 4 relevant tokens (alphanumeric, length>2)
        tokens = re.findall(r"[A-Za-z0-9\-]+", fact_text)
        tokens = [t for t in tokens if len(t) > 2]
        query = " ".join(tokens[:6]) if tokens else fact_text[:80]

        # try to find video urls
        video_urls = ddg_video_search(query, max_results=6)

        chosen_video_path = None
        # try to download first valid video via yt-dlp
        for idx, vurl in enumerate(video_urls):
            # create a target path
            raw_path = os.path.join(tmpdir, f"raw_video_{idx}.mp4")
            ok = download_video_via_ytdlp(vurl, raw_path, tmpdir)
            if ok and os.path.exists(raw_path):
                # try to load with moviepy and trim to audio_duration
                try:
                    vclip = VideoFileClip(raw_path).without_audio()
                    # if clip shorter than needed, we can loop it; else trim
                    if vclip.duration < audio_duration:
                        # loop the clip to reach audio_duration
                        loops = int(audio_duration // vclip.duration) + 1
                        clips_to_concat = [vclip] * loops
                        full = concatenate_videoclips(clips_to_concat).subclip(0, audio_duration)
                    else:
                        full = vclip.subclip(0, audio_duration)
                    # save trimmed version to chosen_video_path
                    chosen_video_path = os.path.join(tmpdir, "chosen_bg.mp4")
                    full.write_videofile(chosen_video_path, codec="libx264", audio=False, verbose=False, logger=None)
                    vclip.close()
                    full.close()
                    break
                except Exception as e:
                    print("moviepy load/trim error for raw_path:", raw_path, e)
                    # try next url
                    continue

        # -----------------------------
        # If no video found, build animated slideshow from images
        # -----------------------------
        if not chosen_video_path:
            # find some images via ddg image search
            image_urls = ddg_image_search(query, max_results=6)
            downloaded_images = []
            for i, img_url in enumerate(image_urls[:6]):
                img_path = os.path.join(tmpdir, f"img_{i}.jpg")
                ok = download_with_requests(img_url, img_path)
                if ok and os.path.exists(img_path):
                    # ensure a loadable image (Pillow)
                    try:
                        with Image.open(img_path) as im:
                            im.verify()
                        downloaded_images.append(img_path)
                    except Exception:
                        continue
            # If we have at least 1 image, create slideshow clips
            if downloaded_images:
                slide_clips = []
                # distribute audio duration across slides proportions (equal durations)
                per_slide = max(0.5, audio_duration / max(1, len(downloaded_images)))
                for img_path in downloaded_images:
                    ic = ImageClip(img_path).set_duration(per_slide)
                    slide_clips.append(ic)
                # concat and ensure total length = audio_duration (trim/pad)
                slideshow = concatenate_videoclips(slide_clips)
                if slideshow.duration < audio_duration:
                    # pad by repeating last frame
                    last = ImageClip(downloaded_images[-1]).set_duration(audio_duration - slideshow.duration)
                    slideshow = concatenate_videoclips([slideshow, last])
                else:
                    slideshow = slideshow.subclip(0, audio_duration)
                chosen_bg_clip = slideshow
            else:
                # fallback to a single background image downloaded from background_gcs_path
                bg_path = os.path.join(tmpdir, "background.jpg")
                try:
                    bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
                    client = storage.Client()
                    bucket = client.bucket(bucket_name)
                    blob = bucket.blob(blob_path)
                    blob.download_to_filename(bg_path)
                    chosen_bg_clip = ImageClip(bg_path).set_duration(audio_duration)
                except Exception as e:
                    # ultimate fallback: create a blank background
                    print("Fallback background error:", e)
                    blank = Image.new("RGB", (canvas_w, canvas_h), (20, 20, 20))
                    blank_path = os.path.join(tmpdir, "blank.jpg")
                    blank.save(blank_path)
                    chosen_bg_clip = ImageClip(blank_path).set_duration(audio_duration)
        else:
            # load chosen_video_path as clip
            chosen_bg_clip = VideoFileClip(chosen_video_path).without_audio()
            if chosen_bg_clip.duration < audio_duration:
                # loop to fill
                loops = int(audio_duration // chosen_bg_clip.duration) + 1
                clips_to_concat = [chosen_bg_clip] * loops
                chosen_bg_clip = concatenate_videoclips(clips_to_concat).subclip(0, audio_duration)
            else:
                chosen_bg_clip = chosen_bg_clip.subclip(0, audio_duration)

        # -----------------------------
        # Build overlay images for each page and composite with background
        # Use small lead so text shows slightly before spoken words
        # -----------------------------
        overlay_clips = []
        x_margin = int(canvas_w * 0.1)
        max_width = int(canvas_w * 0.8)

        for i, p in enumerate(page_infos):
            txt = p["text"]
            start_nom = p["start"]
            end_nom = p["end"]
            dur_nom = p["duration"]

            # compute start time with smarter lead (last page gets bigger lead)
            if i == len(page_infos) - 1:
                lead = 0.5
            else:
                lead = 0.3
            start_time = max(0.0, start_nom - lead)
            duration = max(0.3, end_nom - start_time)

            # create overlay PNG with semi-transparent rounded rectangle + text
            overlay_img = Image.new("RGBA", (canvas_w, canvas_h), (0,0,0,0))
            odraw = ImageDraw.Draw(overlay_img)

            # measure text bounding box for center
            bbox = odraw.textbbox((0, 0), txt, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            box_w = int(min(max_width, text_w + 80))
            box_h = int(text_h + 56)
            box_x = int(x_margin + (max_width - box_w) / 2)
            box_y = int((canvas_h - box_h) / 2)

            # Draw semi-transparent rounded rectangle (approx using rectangle)
            rect_color = (0, 0, 0, int(255 * 0.55))
            odraw.rectangle([box_x, box_y, box_x + box_w, box_y + box_h], fill=rect_color)

            # Draw the gold text with a black stroke
            text_x = box_x + (box_w - text_w) / 2
            text_y = box_y + (box_h - text_h) / 2
            odraw.text((text_x, text_y), txt, font=font, fill=(255,215,0,255), stroke_width=3, stroke_fill=(0,0,0,255))

            overlay_path = os.path.join(tmpdir, f"overlay_{i}.png")
            overlay_img.save(overlay_path)

            # Create image clip overlay timed to start_time and duration
            overlay_clip = ImageClip(overlay_path).set_start(start_time).set_duration(duration)
            overlay_clips.append(overlay_clip)

        # -----------------------------
        # Composite background + overlays and attach audio
        # -----------------------------
        # Ensure background is first layer, overlays above
        all_clips = [chosen_bg_clip] + overlay_clips
        composite = CompositeVideoClip(all_clips, size=(canvas_w, canvas_h))
        composite = composite.set_duration(audio_duration)
        composite = composite.set_audio(audio_clip)

        # -----------------------------
        # Output final video
        # -----------------------------
        output_path = os.path.join(tmpdir, "output.mp4")
        composite.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        client = storage.Client()
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
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
            "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old."
        background_gcs_path = data.get("background") or os.environ.get("BACKGROUND_REF") or \
            "gs://trivia-videos-output/background.jpg"
        output_gcs_path = data.get("output") or os.environ.get("OUTPUT_GCS") or \
            "gs://trivia-videos-output/output.mp4"

        video_url = create_trivia_video(fact, background_gcs_path, output_gcs_path)
        return jsonify({"status": "ok", "fact": fact, "video_url": video_url})

    except Exception as e:
        # print traceback to logs for debugging
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
