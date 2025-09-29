import os
import tempfile
from flask import Flask, request, jsonify
from google.cloud import texttospeech, storage
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip
from moviepy.video.fx.resize import resize
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)


# --- TTS function ---
def synthesize_tts(text, bucket_name, blob_name):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Neural2-D",  # male voice
    )

    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(response.audio_content, content_type="audio/mpeg")

    return f"gs://{bucket_name}/{blob_name}"


# --- Helper: create text image with Pillow ---
def create_text_image(text, fontsize=48, color="white", size=(1080, 200)):
    img = Image.new("RGBA", size, (0, 0, 0, 0))  # transparent
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", fontsize)
    except IOError:
        font = ImageFont.load_default()

    max_width = size[0] - 20
    words = text.split(" ")
    lines = []
    line = ""
    for word in words:
        test_line = f"{line} {word}".strip()
        w, _ = draw.textsize(test_line, font=font)
        if w <= max_width:
            line = test_line
        else:
            lines.append(line)
            line = word
    lines.append(line)

    y = 10
    for line in lines:
        w, h = draw.textsize(line, font=font)
        draw.text(((size[0] - w) / 2, y), line, font=font, fill=color)
        y += h + 5

    tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(tmpfile.name, "PNG")
    return tmpfile.name


# --- Video creation ---
def create_trivia_video(question, choices, answer, background_gcs, output_gcs):
    storage_client = storage.Client()

    # Parse GCS paths
    bucket_name, bg_blob_name = background_gcs.replace("gs://", "").split("/", 1)
    out_bucket_name, out_blob_name = output_gcs.replace("gs://", "").split("/", 1)

    # Download background
    bg_path = os.path.join(tempfile.gettempdir(), "background.jpg")
    storage_client.bucket(bucket_name).blob(bg_blob_name).download_to_filename(bg_path)

    # Generate narration
    narration_text = f"{question} Options: {choices}. The correct answer is {answer}!"
    tts_blob = "narration.mp3"
    synthesize_tts(narration_text, bucket_name, tts_blob)

    # Download narration
    tts_local = os.path.join(tempfile.gettempdir(), "narration.mp3")
    storage_client.bucket(bucket_name).blob(tts_blob).download_to_filename(tts_local)

    audio_clip = AudioFileClip(tts_local)
    duration = audio_clip.duration

    # Background
    base_clip = (
        ImageClip(bg_path)
        .set_duration(duration)
    )
    base_clip = resize(base_clip, height=1080)

    # Text overlays via Pillow
    q_img = create_text_image(question, fontsize=70, size=(1080, 300))
    c_img = create_text_image(choices, fontsize=50, size=(1080, 400))
    a_img = create_text_image(f"✅ {answer}", fontsize=60, size=(1080, 200), color="green")

    txt_question = ImageClip(q_img).set_duration(duration).set_position(("center", 50))
    txt_choices = ImageClip(c_img).set_duration(duration).set_position(("center", 400))
    txt_answer = ImageClip(a_img).set_duration(duration).set_position(("center", 800))

    # Final video
    final = CompositeVideoClip([base_clip, txt_question, txt_choices, txt_answer]).set_audio(audio_clip)

    video_path = os.path.join(tempfile.gettempdir(), "trivia.mp4")
    final.write_videofile(video_path, codec="libx264", audio_codec="aac", fps=24)

    # Upload
    storage_client.bucket(out_bucket_name).blob(out_blob_name).upload_from_filename(video_path)

    # Cleanup
    os.remove(q_img)
    os.remove(c_img)
    os.remove(a_img)
    os.remove(video_path)

    return output_gcs


@app.route("/", methods=["POST"])
def generate_video():
    request_json = request.get_json(silent=True)

    question = request_json.get("question", "What is the capital of France?")
    choices = request_json.get("choices", "A: Berlin\nB: Paris\nC: Madrid\nD: Rome")
    answer = request_json.get("answer", "Paris")
    background_gcs = request_json.get("background", "gs://trivia-videos-output/background.jpg")
    output_gcs = request_json.get("output", "gs://trivia-videos-output/trivia_quiz.mp4")

    result = create_trivia_video(question, choices, answer, background_gcs, output_gcs)

    return jsonify({"status": "success", "video": result})


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))