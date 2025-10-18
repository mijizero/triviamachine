import os
import random
import tempfile
import requests
from flask import Flask, jsonify
from moviepy.editor import VideoFileClip, AudioFileClip
from moviepy.video.tools.drawing import color_gradient
from moviepy.editor import TextClip, CompositeVideoClip
from google.cloud import texttospeech, storage

app = Flask(__name__)

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

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

    with open(output_path, "wb") as out:
        out.write(response.audio_content)
    return output_path


# -------------------------------
# Helper: Download random video
# -------------------------------
def get_random_video():
    sample_videos = [
        "https://cdn.pixabay.com/vimeo/2987/forest-36802.mp4?width=640&hash=2c1b7efc7b8a6c3f5a9f3b6c38a8bb5b8c7b1e5f",
        "https://cdn.pixabay.com/vimeo/4519/waterfall-22497.mp4?width=640&hash=9b59dfd33f7c776cdb31e5c32c1f7dbf4235b42e",
        "https://cdn.pixabay.com/vimeo/2568/sky-11570.mp4?width=640&hash=7ef12db29f8358b0f414d9a6d4c6218f4bda74e3"
    ]
    for url in random.sample(sample_videos, len(sample_videos)):
        print(f"Trying background: {url}")
        r = requests.get(url, stream=True)
        content_type = r.headers.get("Content-Type", "")
        if "video" in content_type:
            video_path = os.path.join(tempfile.gettempdir(), "background.mp4")
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return video_path
        else:
            print(f"⚠️ Not a valid video: {content_type}")
    raise RuntimeError("No valid background video found.")


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

    bg_video_path = get_random_video()
    bg_clip = VideoFileClip(bg_video_path).subclip(0, 20)

    audio_path = os.path.join(tempfile.gettempdir(), "speech.mp3")
    synthesize_speech(fact, audio_path)
    audio_clip = AudioFileClip(audio_path)

    lines = fact.split("\n")
    clips = []
    segment_duration = audio_clip.duration / len(lines)

    for i, line in enumerate(lines):
        txt = TextClip(
            line,
            fontsize=50,
            color="white",
            stroke_color="black",
            stroke_width=2,
            font="DejaVu-Sans-Bold"
        ).set_position("center").set_duration(segment_duration).set_start(i * segment_duration)
        clips.append(txt)

    composite = CompositeVideoClip([bg_clip, *clips])
    composite = composite.set_audio(audio_clip)
    output_path = os.path.join(tempfile.gettempdir(), "output.mp4")
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
        video_url = create_trivia_video()
        return jsonify({
            "status": "ok",
            "video_url": video_url
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


# -------------------------------
# Main Entry
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
