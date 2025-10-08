import os
import tempfile
from flask import Flask, jsonify
from google.cloud import texttospeech_v1beta1 as texttospeech
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# -------------------------------
# Hardcoded Trivia Fact
# -------------------------------
TRIVIA_TEXT = (
    "Did you know honey never spoils? "
    "Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. "
    "Its natural composition prevents bacteria from growing, keeping it preserved for millennia. "
    "Honey has natural antioxidants and enzymes that contribute to its preservation. "
    "Even in modern times, honey stored properly can last indefinitely. "
    "People have used honey historically for both nutrition and medicinal purposes. "
    "The unique properties of honey make it one of the only foods that can truly last forever."
)

# -------------------------------
# Synthesize Speech with Word Timing
# -------------------------------
def synthesize_speech_with_timing(text, output_path):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name="en-AU-Neural2-D",
        ssml_gender=texttospeech.SsmlVoiceGender.MALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=0.9,
        pitch=0.8,
        volume_gain_db=2.0
    )
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
        enable_time_pointing=[texttospeech.TimepointType.WORD]
    )
    with open(output_path, "wb") as out:
        out.write(response.audio_content)
    return [(wp.word, wp.time_seconds) for wp in response.timepoints]

# -------------------------------
# Generate Video
# -------------------------------
def create_trivia_video(fact_text):
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Background Image ---
        bg_path = os.path.join(tmpdir, "background.jpg")
        img = Image.new("RGB", (1080, 1920), color=(30, 30, 30))
        img.save(bg_path)

        # --- TTS ---
        audio_path = os.path.join(tmpdir, "audio.mp3")
        word_timings = synthesize_speech_with_timing(fact_text, audio_path)
        audio_clip = AudioFileClip(audio_path)

        # --- Split into words and lines ---
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("Roboto-Regular.ttf", 60)
        max_width = int(img.width * 0.8)

        words = [w for w, t in word_timings]
        lines = []
        current_line = []
        for word in words:
            test_line = " ".join(current_line + [word])
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line.append(word)
            else:
                lines.append(" ".join(current_line))
                current_line = [word]
        if current_line:
            lines.append(" ".join(current_line))

        # --- Pages (2 lines per page) ---
        pages = []
        for i in range(0, len(lines), 2):
            pages.append("\n".join(lines[i:i+2]))

        # --- Calculate page durations ---
        page_durations = []
        word_idx = 0
        for page in pages:
            page_words = page.replace("\n", " ").split()
            start_time = word_timings[word_idx][1]
            end_time = word_timings[word_idx + len(page_words) - 1][1]
            duration = max(end_time - start_time, 0.5)
            page_durations.append(duration)
            word_idx += len(page_words)

        # --- Create video clips ---
        clips = []
        for i, page_text in enumerate(pages):
            page_img = img.copy()
            draw_page = ImageDraw.Draw(page_img)
            bbox = draw_page.multiline_textbbox((0, 0), page_text, font=font, spacing=15)
            text_w, text_h = bbox[2]-bbox[0], bbox[3]-bbox[1]
            x = (page_img.width - text_w)/2
            y = (page_img.height - text_h)/2
            draw_page.multiline_text(
                (x, y), page_text, font=font, fill="#FFD700", spacing=15,
                stroke_width=10, stroke_fill="black", align="center"
            )
            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img.save(page_path)
            clip = ImageClip(page_path).set_duration(page_durations[i])
            clips.append(clip)

        video_clip = concatenate_videoclips(clips).set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "trivia_video.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)

        return output_path

# -------------------------------
# Flask Endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate():
    try:
        video_path = create_trivia_video(TRIVIA_TEXT)
        return jsonify({"status": "ok", "video_path": video_path})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

# -------------------------------
# Run App
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
