from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
from PIL import Image, ImageDraw, ImageFont
import tempfile
import os
from google.cloud import storage

def create_trivia_video(fact_text, background_gcs_path, output_gcs_path):
    """Create a trivia video displaying and reading one line at a time."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download background
        bg_path = os.path.join(tmpdir, "background.jpg")
        bucket_name, blob_path = background_gcs_path.replace("gs://", "").split("/", 1)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(bg_path)

        # Wrap text to fit 80% width
        img = Image.open(bg_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font_path = "Roboto-Regular.ttf"
        font_size = 60
        font = ImageFont.truetype(font_path, font_size)
        max_width = int(img.width * 0.8)

        words = fact_text.split()
        lines = []
        line = ""
        for word in words:
            test_line = f"{line} {word}".strip()
            bbox = draw.textbbox((0,0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_width:
                line = test_line
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)

        # For each line: generate audio, create clip
        clips = []
        for i, line in enumerate(lines):
            # Generate TTS for this line
            audio_path = os.path.join(tmpdir, f"audio_{i}.mp3")
            synthesize_speech(line, audio_path)
            audio_clip = AudioFileClip(audio_path)
            duration = audio_clip.duration

            # Draw line centered
            img_copy = img.copy()
            draw_copy = ImageDraw.Draw(img_copy)
            bbox = draw_copy.textbbox((0,0), line, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            draw_copy.text(
                ((img_copy.width - w)/2, (img_copy.height - h)/2),
                line,
                font=font,
                fill="white"
            )

            # Save annotated image
            annotated_path = os.path.join(tmpdir, f"annotated_{i}.jpg")
            img_copy.save(annotated_path)

            # Create video clip
            clip = ImageClip(annotated_path, duration=duration)
            clip = clip.set_audio(audio_clip)
            clips.append(clip)

        # Concatenate all clips
        final_clip = concatenate_videoclips(clips)
        output_path = os.path.join(tmpdir, "output.mp4")
        final_clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

        # Upload to GCS
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path.replace("background.jpg", "output.mp4"))
        blob.upload_from_filename(output_path)
        return f"https://storage.googleapis.com/{bucket_name}/{blob.name}"
