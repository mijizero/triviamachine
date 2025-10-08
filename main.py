import os
import tempfile
from flask import Flask, jsonify
from google.cloud import texttospeech
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips

app = Flask(__name__)

# Hardcoded long fact for multi-page testing
FACT_TEXT = (
    "Did you know honey never spoils? Archaeologists have found edible honey in ancient Egyptian tombs over 3000 years old. "
    "Its natural composition prevents bacteria from growing, keeping it preserved for millennia. "
    "Honey was used not just as food but also in medicine and ritual ceremonies. "
    "The ancient Egyptians even used it as an offering to their gods. "
    "Interestingly, bees make honey by evaporating nectar and adding enzymes, creating a substance that resists microbial growth. "
    "Modern scientists study honey's preservation properties to develop antibacterial treatments. "
    "There are over 300 unique types of honey worldwide, each with its own flavor and health benefits. "
    "Some rare honeys, like Manuka honey, are prized for medicinal properties. "
    "Honey continues to be a symbol of sweetness, longevity, and natural preservation in cultures across the globe."
)

@app.route("/generate", methods=["POST"])
def generate():
    with tempfile.TemporaryDirectory() as tmpdir:
        # --- TTS ---
        audio_path = os.path.join(tmpdir, "audio.mp3")
        client = texttospeech.TextToSpeechClient()  # uses Cloud Run default SA
        synthesis_input = texttospeech.SynthesisInput(text=FACT_TEXT)
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-AU",
            name="en-AU-Neural2-D"
        )
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)

        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
            enable_time_pointing=[texttospeech.TimepointType.WORD]
        )

        with open(audio_path, "wb") as f:
            f.write(response.audio_content)

        # --- Extract word timings ---
        word_timings = [(wp.word, wp.time_seconds) for wp in response.timepoints]
        words = [w for w, t in word_timings]

        # --- Create dummy image pages with 2 lines per page ---
        target_size = (1080, 1920)
        img = Image.new("RGB", target_size, color=(0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 60)

        # Split words into lines (~5 words per line for testing)
        lines = []
        line = []
        for word in words:
            line.append(word)
            if len(line) >= 5:
                lines.append(" ".join(line))
                line = []
        if line:
            lines.append(" ".join(line))

        # 2 lines per page
        pages = []
        for i in range(0, len(lines), 2):
            pages.append("\n".join(lines[i:i+2]))

        # --- Page durations based on word timings ---
        page_durations = []
        word_idx = 0
        for page in pages:
            page_words = page.replace("\n", " ").split()
            start_time = word_timings[word_idx][1]
            end_time = word_timings[word_idx + len(page_words) - 1][1]
            page_durations.append(max(end_time - start_time, 0.5))
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
                (x, y),
                page_text,
                font=font,
                fill="#FFD700",
                spacing=15,
                stroke_width=10,
                stroke_fill="black",
                align="center"
            )
            page_path = os.path.join(tmpdir, f"page_{i}.png")
            page_img.save(page_path)
            clip = ImageClip(page_path).set_duration(page_durations[i])
            clips.append(clip)

        audio_clip = AudioFileClip(audio_path)
        video_clip = concatenate_videoclips(clips).set_audio(audio_clip)
        output_path = os.path.join(tmpdir, "trivia_test_video.mp4")
        video_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac",
                                   verbose=False, logger=None)

        return jsonify({
            "status": "ok",
            "fact": FACT_TEXT,
            "word_timings": word_timings,
            "pages": pages,
            "page_durations": page_durations,
            "video_path": output_path
        })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
