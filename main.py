import os
import tempfile
from flask import Flask, jsonify
from google.cloud import texttospeech
from google.cloud.texttospeech_v1.types import SynthesizeSpeechRequest
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# -------------------------------
# Test fact
# -------------------------------
TEST_FACT = (
    "Did you know honey never spoils? "
    "Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. "
    "Its natural composition prevents bacteria from growing, keeping it preserved for millennia. "
    "Additionally, honey has been used for medicinal purposes in various cultures due to its antibacterial properties. "
    "The oldest known recipe, dating back to 2100 BC, included honey as a primary ingredient. "
    "Many insects and animals are attracted to honey because of its high sugar content. "
    "Modern beekeepers still harvest honey in ways similar to ancient practices, ensuring its quality."
)

# -------------------------------
# TTS + Video helper
# -------------------------------
def create_video_with_tts(text):
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Prepare TTS ---
        client = texttospeech.TextToSpeechClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-AU",
            name="en-AU-Neural2-D"
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.9,
            pitch=0.8,
            volume_gain_db=2.0
        )

        # ✅ Correct word-level timing
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
            enable_time_pointing=[SynthesizeSpeechRequest.TimepointType.WORD]
        )

        audio_path = os.path.join(tmpdir, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(response.audio_content)

        audio_clip = AudioFileClip(audio_path)

        # --- Generate video pages ---
        img_path = os.path.join(tmpdir, "bg.jpg")
        img = Image.new("RGB", (1080, 1920), color=(30, 30, 30))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)

        # Split text into words and 2 lines per page
        words = [wp.word for wp in response.timepoints]
        lines = []
        current_line = []
        max_words_per_line = 12
        for w in words:
            current_line.append(w)
            if len(current_line) >= max_words_per_line:
                lines.append(" ".join(current_line))
                current_line = []
        if current_line:
            lines.append(" ".join(current_line))

        pages = []
        for i in range(0, len(lines), 2):
            pages.append("\n".join(lines[i:i+2]))

        # Calculate durations from word timings
        page_durations = []
        word_idx = 0
        for page in pages:
            page_words = page.replace("\n", " ").split()
            if not page_words:
                page_durations.append(0.5)
                continue
            start_time = response.timepoints[word_idx].time_seconds
            word_idx_end = word_idx + len(page_words) - 1
            end_time = response.timepoints[word_idx_end].time_seconds
            page_durations.append(max(end_time - start_time, 0.5))
            word_idx += len(page_words)

        # --- Create clips ---
        clips = []
        for i, page_text in enumerate(pages):
            page_img = img.copy()
            draw_page = ImageDraw.Draw(page_img)
            text_w, text_h = draw_page.multiline_textsize(page_text, font=font, spacing=15)
            x = (page_img.width - text_w) / 2
            y = (page_img.height - text_h) / 2
            draw_page.multiline_text((x, y), page_text, font=font, fill="white", spacing=15, align="center")
            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img.save(page_path)
            clip = ImageClip(page_path).set_duration(page_durations[i])
            clips.append(clip)

        video_clip = concatenate_videoclips(clips).set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "trivia_video.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", verbose=False, logger=None)
        return output_path

# -------------------------------
# Flask endpoint
# -------------------------------
@app.route("/generate", methods=["POST"])
def generate():
    try:
        video_path = create_video_with_tts(TEST_FACT)
        return jsonify({"status": "ok", "video_path": video_path})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
