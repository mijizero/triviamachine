import os
import random
import tempfile
import requests
from flask import Flask, jsonify
from moviepy.editor import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip, concatenate_audioclips
from google.cloud import texttospeech, storage

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
    return output_path

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
# Create video with synced TTS
# -------------------------------
def create_trivia_video_synced():
    lines = [
        "Did you know?",
        "Honey never spoils — archaeologists found 3000-year-old honey still edible.",
        "Bananas are berries, but strawberries aren’t!",
        "Octopuses have three hearts.",
        "And wombat poop is cube-shaped!"
    ]

    # Background video
    bg_video_path = get_random_video("nature")
    bg_clip = VideoFileClip(bg_video_path).subclip(0, 30)  # adjust length

    # Generate TTS for each line and collect durations
    audio_clips = []
    text_clips = []
    current_start = 0

    for i, line in enumerate(lines):
        line_audio_path = os.path.join(tempfile.gettempdir(), f"line_{i}.mp3")
        synthesize_speech(line, line_audio_path)
        line_audio = AudioFileClip(line_audio_path)
        audio_clips.append(line_audio)

        txt_clip = TextClip(
            line,
            fontsize=50,
            color="white",
            stroke_color="black",
            stroke_width=2,
            font="DejaVu-Sans-Bold"
        ).set_position("center").set_duration(line_audio.duration).set_start(current_start)

        text_clips.append(txt_clip)
        current_start += line_audio.duration

    # Concatenate audio clips
    final_audio = concatenate_audioclips(audio_clips)

    # Composite video with synced text
    composite = CompositeVideoClip([bg_clip, *text_clips])
    composite = composite.set_audio(final_audio)

    # Output video
    output_path = os.path.join(tempfile.gettempdir(), "output_synced.mp4")
    composite.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

    # Upload to GCS
    public_url = upload_to_gcs(output_path, "trivia-videos-output")
    return public_url

# -------------------------------
# Flask endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_video():
    try:
        video_url = create_trivia_video_synced()
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
