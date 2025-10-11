import os
import base64
import tempfile
import requests
from flask import Flask, jsonify, send_file
from moviepy.editor import AudioFileClip, TextClip, CompositeVideoClip, ColorClip

app = Flask(__name__)

# 🔧 Your existing Google Cloud Function / TTS endpoint
TTS_ENDPOINT = "https://us-central1-trivia-machine-472207.cloudfunctions.net/tts_generate"

@app.route("/generate", methods=["POST"])
def generate():
    try:
        # Example pages (2 lines each)
        pages = [
            "Formula One began in 1950.\nIt has grown into a global spectacle.",
            "Each race weekend attracts\nmillions of fans worldwide.",
            "Cars can reach over 350 kilometers\nper hour.",
            "Drivers push limits of speed,\nprecision, and endurance."
        ]

        # Build SSML with <mark> tags
        ssml = "<speak>"
        for i, p in enumerate(pages):
            ssml += f'<mark name="p{i+1}"/>{p} '
        ssml += "</speak>"

        # Call your TTS endpoint
        payload = {
            "ssml": ssml,
            "voice": "en-US-Neural2-A",
            "encoding": "MP3",
            "enableTimePointing": True
        }
        r = requests.post(TTS_ENDPOINT, json=payload)
        tts_result = r.json()

        audio_b64 = tts_result.get("audioContent", "")
        marks = tts_result.get("timepoints", [])

        if not audio_b64:
            raise Exception("No audio returned from TTS endpoint")

        # Save audio
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as ta:
            ta.write(base64.b64decode(audio_b64))
            audio_path = ta.name

        # Extract timepoints
        timings = []
        for i, m in enumerate(marks):
            start = m["timeSeconds"]
            end = marks[i + 1]["timeSeconds"] if i + 1 < len(marks) else start + 2.5
            timings.append({"page": pages[i], "start": start, "end": end})

        # Create video
        audio_clip = AudioFileClip(audio_path)
        duration = audio_clip.duration
        bg = ColorClip(size=(1080, 1920), color=(0, 0, 0), duration=duration)

        txt_clips = []
        for tm in timings:
            txt = tm["page"]
            c = (
                TextClip(txt, fontsize=70, color="white", size=(1000, None), method="caption")
                .set_position(("center", "center"))
                .set_start(tm["start"])
                .set_end(tm["end"])
            )
            txt_clips.append(c)

        video = CompositeVideoClip([bg, *txt_clips])
        video = video.set_audio(audio_clip)

        # Save to file
        out_path = os.path.join(tempfile.gettempdir(), "tts_test_video.mp4")
        video.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac")

        # Return video
        return send_file(out_path, mimetype="video/mp4")

    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
