import os
import random
import tempfile
import requests
from flask import Flask, jsonify
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, ImageClip
from google.cloud import texttospeech, storage
from pydub import AudioSegment
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# -------------------------------
# Pexels API Key - replace with your real key
# -------------------------------
PEXELS_API_KEY = "zXJ9dAVT3F0TLcEqMkGXtE5H8uePbhEvuq0kBnWnbq8McMpIKTQeWnDQ"

# -------------------------------
# Helper: Generate speech
# -------------------------------
def synthesize_speech(text, output_path):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name="en-AU-Neural2-A",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=0.95,
        pitch=-1.0,
        volume_gain_db=1.5
    )
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    with open(output_path, "wb") as out:
        out.write(response.audio_content)
    
    # Convert MP3 to WAV for MoviePy
    wav_path = output_path.replace(".mp3", ".wav")
    AudioSegment.from_mp3(output_path).export(wav_path, format="wav")
    return wav_path

# -------------------------------
# Helper: Download random video from Pexels
# -------------------------------
def get_random_video(query="nature"):
    headers = {"Authorization": PEXELS_API_KEY}
    url = f"https://api.pexels.com/videos/search?query={query}&per_page=10"
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError("No Pexels videos found for query.")
    
    video_data = random.choice(videos)
    video_url = video_data["video_files"][-1]["link"]
    print(f"Downloading Pexels video: {video_url}")
    video_path = os.path.join(tempfile.gettempdir(), "background.mp4")
    with requests.get(video_url, stream=True) as vid_r:
        vid_r.raise_for_status()
        with open(video_path, "wb") as f:
            for chunk in vid_r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return video_path

# -------------------------------
# Helper: Upload to GCS
# -------------------------------
def upload_to_gcs(local_path, bucket_name):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob_name = os.path.basename(local_path)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    blob.make_public()
    return blob.public_url

# -------------------------------
# Create video
# -------------------------------
def create_trivia_video():
    fact = (
        "Did you know?\n"
        "Honey never spoils — archaeologists found 3000-year-old honey still edible.\n"
        "Bananas are berries, but strawberries aren’t!\n"
        "Octopuses have three hearts.\n"
        "And wombat poop is cube-shaped!"
    )
    print("Creating video with fact:\n", fact)

    # --- Background video ---
    bg_video_path = get_random_video("nature")
    bg_clip = VideoFileClip(bg_video_path).subclip(0, 20)

    # Resize/crop to vertical 1080x1920
    bg_clip = bg_clip.resize(height=1920)
    # Center crop width
    bg_clip = bg_clip.crop(x_center=bg_clip.w/2, width=1080)

    # --- Generate speech ---
    audio_path = os.path.join(tempfile.gettempdir(), "speech.mp3")
    wav_path = synthesize_speech(fact, audio_path)
    audio_clip = AudioFileClip(wav_path)
    audio_duration = audio_clip.duration

    # --- Split fact into 2-line pages ---
    tmp_dir = tempfile.gettempdir()
    font_path = "Roboto-Regular.ttf"
    font = ImageFont.truetype(font_path, 55)

    lines = fact.replace("*","").split("\n")
    pages = ["\n".join(lines[i:i+2]) for i in range(0, len(lines), 2)]

    # --- Create images for each page with start times ---
    clips = []
    page_duration = audio_duration / len(pages)
    for i, page_text in enumerate(pages):
        img = Image.new("RGB", (1080, 1920), color=(0,0,0,0))
        draw = ImageDraw.Draw(img)
        bbox = draw.multiline_textbbox((0,0), page_text, font=font, spacing=15)
        text_w, text_h = bbox[2]-bbox[0], bbox[3]-bbox[1]
        text_x = (1080 - text_w)/2
        text_y = (1920 - text_h)/2
        draw.multiline_text(
            (text_x, text_y),
            page_text,
            font=font,
            fill="white",
            stroke_width=3,
            stroke_fill="black",
            spacing=15,
            align="center"
        )
        img_path = os.path.join(tmp_dir, f"page_{i}.png")
        img.save(img_path)

        clip = ImageClip(img_path).set_duration(page_duration).set_start(i*page_duration)
        clips.append(clip)

    # --- Composite with background video ---
    composite = CompositeVideoClip([bg_clip, *clips], size=(1080,1920))
    composite = composite.set_audio(audio_clip)
    output_path = os.path.join(tmp_dir, "output.mp4")
    composite.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

    # --- Upload to GCS ---
    public_url = upload_to_gcs(output_path, "trivia-videos-output")
    return public_url

# -------------------------------
# Flask endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_video():
    try:
        video_url = create_trivia_video()
        return jsonify({"status": "ok", "video_url": video_url})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

# -------------------------------
# Main Entry
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
