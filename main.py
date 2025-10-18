import os
import random
import tempfile
import requests
from flask import Flask, jsonify, request
from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip, AudioFileClip
from google.cloud import texttospeech

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
# Helper: Get random background video (Pexels direct MP4s)
# -------------------------------
def get_random_video():
    # ✅ Real, direct MP4 files from Pexels CDN (no redirects, stable)
    sample_videos = [
        "https://videos.pexels.com/video-files/856331/856331-hd_1920_1080_24fps.mp4",
        "https://videos.pexels.com/video-files/857195/857195-hd_1920_1080_24fps.mp4",
        "https://videos.pexels.com/video-files/855427/855427-hd_1920_1080_24fps.mp4",
        "https://videos.pexels.com/video-files/1526909/1526909-hd_1920_1080_24fps.mp4",
        "https://videos.pexels.com/video-files/1307711/1307711-hd_1920_1080_24fps.mp4"
    ]

    random.shuffle(sample_videos)
    video_path = os.path.join(tempfile.gettempdir(), "background.mp4")

    for url in sample_videos:
        print(f"Trying background: {url}")
        try:
            r = requests.get(url, stream=True, timeout=15)
            content_type = r.headers.get("Content-Type", "")
            if "video" not in content_type.lower():
                print("⚠️ Not a valid video:", content_type)
                continue

            with open(video_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            if os.path.getsize(video_path) > 100_000:
                print("✅ Video downloaded successfully:", video_path)
                return video_path
            else:
                print("⚠️ File too small, trying another...")
        except Exception as e:
            print("⚠️ Error downloading video:", e)

    raise RuntimeError("No valid background video found.")


# -------------------------------
# Create video
# -------------------------------
def create_trivia_video():
    # Hardcoded 5-line fact
    fact = (
        "Did you know?\n"
        "Honey never spoils — archaeologists found 3000-year-old honey still edible.\n"
        "Bananas are berries, but strawberries aren’t!\n"
        "Octopuses have three hearts.\n"
        "And wombat poop is cube-shaped!"
    )

    print("Creating video with fact:\n", fact)

    # 1. Download background video
    bg_video_path = get_random_video()
    bg_clip = VideoFileClip(bg_video_path).subclip(0, 20)  # 20 sec limit

    # 2. Generate TTS audio
    audio_path = os.path.join(tempfile.gettempdir(), "speech.mp3")
    synthesize_speech(fact, audio_path)
    audio_clip = AudioFileClip(audio_path)

    # 3. Prepare caption text
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
            font=DejaVu-Sans-Bold"
        ).set_position("center").set_duration(segment_duration).set_start(i * segment_duration)
        clips.append(txt)

    # 4. Combine background + captions + audio
    composite = CompositeVideoClip([bg_clip, *clips])
    composite = composite.set_audio(audio_clip)
    output_path = os.path.join(tempfile.gettempdir(), "output.mp4")
    composite.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

    return output_path


# -------------------------------
# Flask endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate_video():
    try:
        output_path = create_trivia_video()
        return jsonify({
            "status": "ok",
            "output_path": output_path
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
