from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
import tempfile
import os
from google.cloud import storage, texttospeech

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create a trivia video displaying one line at a time with TTS."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize GCS client
        client = storage.Client()

        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # Split fact into chunks that fit 80% of width
        img_tmp = Image.open(bg_path).convert("RGB")
        draw_tmp = ImageDraw.Draw(img_tmp)
        font_path = "Roboto-Regular.ttf"
        font_size = 60
        font = ImageFont.truetype(font_path, font_size)
        max_width = img_tmp.width * 0.8

        words = fact_text.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = f"{current_line} {word}".strip()
            bbox = draw_tmp.textbbox((0,0), test_line, font=font)
            line_width = bbox[2] - bbox[0]
            if line_width <= max_width:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)

        # Create video clips line by line
        clips = []
        for i, line in enumerate(lines):
            # Generate TTS for this line
            audio_path = os.path.join(tmpdir, f"audio_{i}.mp3")
            synthesize_speech(line, audio_path)
            audio_clip = AudioFileClip(audio_path)
            duration = audio_clip.duration

            # Create image with centered line
            img = Image.open(bg_path).convert("RGB")
            draw = ImageDraw.Draw(img)
            bbox = draw.textbbox((0,0), line, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            x = (img.width - w) / 2
            y = (img.height - h) / 2
            draw.text((x, y), line, font=font, fill="white")

            # Save image
            img_path = os.path.join(tmpdir, f"image_{i}.jpg")
            img.save(img_path)

            # Create video clip for this line
            clip = ImageClip(img_path, duration=duration).set_audio(audio_clip)
            clips.append(clip)

        # Concatenate all clips
        final_clip = concatenate_videoclips(clips)
        output_path = os.path.join(tmpdir, "output.mp4")
        final_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload final video to GCS
        output_blob_path = blob_path.replace("background.jpg", "output.mp4")
        output_blob = bucket.blob(output_blob_path)
        output_blob.upload_from_filename(output_path)
        return f"https://storage.googleapis.com/{bucket_name}/{output_blob.name}"
