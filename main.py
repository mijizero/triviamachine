import os
import base64
import tempfile
from flask import Flask, jsonify
from moviepy.editor import AudioFileClip, TextClip, CompositeVideoClip, ColorClip
from google.cloud import texttospeech_v1beta1 as tts
from google.cloud import storage

app = Flask(__name__)

# Hardcoded GCS bucket
OUTPUT_BUCKET = "trivia-videos-output"

tts_client = tts.TextToSpeechClient()

def synthesize_ssml(ssml):
    input_text = tts.SynthesisInput(ssml=ssml)
    voice = tts.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-A"
    )
    audio_config = tts.AudioConfig(
        audio_encoding=tts.AudioEncoding.MP3,
        enable_time_pointing=[tts.TimepointType.SSML_MARK]
    )

    response = tts_client.synthesize_speech(
        input=input_text,
        voice=voice,
        audio_config=audio_config
    )

    audio_b64 = base64.b64encode(response.audio_content).decode("utf-8")
    marks = [{"markName": m.mark_name, "timeSeconds": m.time_seconds} for m in response.timepoints]

    return audio_b64, marks

def upload_to_gcs(local_path, destination_blob_name):
    client = storage.Client()
    bucket = client.bucket(OUTPUT_BUCKET)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(local_path)
    blob.make_public()
    return blob.public_url

@app.route("/generate", methods=["POST"])
def generate():
    try:
        pages = [
            "Formula One began in 1950.\nIt has grown into a global spectacle.",
            "Each race weekend attracts\nmillions of fans worldwide.",
            "Cars can reach over 350 kilometers\nper hour.",
            "Drivers push limits of speed,\nprecision, and endurance."
        ]

        ssml = "<speak>"
        for i, p in enumerate(pages):
            ssml += f'<mark name="p{i+1}"/>{p} '
        ssml += "</speak>"

        audio_b64, marks = synthesize_ssml(ssml)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as ta:
            ta.write(base64.b64decode(audio_b64))
            audio_path = ta.name

        timings = []
        for i, m in enumerate(marks):
            start = m["timeSeconds"]
            end = marks[i + 1]["timeSeconds"] if i + 1 < len(marks) else start + 2.5
            timings.append({"page": pages[i], "start": start, "end": end})

        audio_clip = AudioFileClip(audio_path)
        duration = audio_clip.duration
        bg = ColorClip(size=(1080, 1920), color=(0, 0, 0), duration=duration)

        txt_clips = []
        for tm in timings:
            c = (
                TextClip(tm["page"], fontsize=70, color="white", size=(1000, None), method="caption")
                .set_position(("center", "center"))
                .set_start(tm["start"])
                .set_end(tm["end"])
            )
            txt_clips.append(c)

        video = CompositeVideoClip([bg, *txt_clips])
        video = video.set_audio(audio_clip)

        out_path = os.path.join(tempfile.gettempdir(), "tts_test_video.mp4")
        video.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac")

        gcs_name = f"outputs/tts_test_video_{int(tempfile.mkstemp()[1])}.mp4"
        public_url = upload_to_gcs(out_path, gcs_name)

        os.remove(out_path)
        os.remove(audio_path)

        return jsonify({"video_url": public_url})

    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
