from flask import Flask, request, jsonify
from google.cloud import texttospeech, storage, secretmanager
from moviepy.editor import AudioFileClip, ImageClip, CompositeVideoClip, CompositeAudioClip
from PIL import Image, ImageDraw, ImageFont
import tempfile
import os
import html
import numpy as np
import requests
import random
import datetime
import vertexai
from vertexai.generative_models import GenerativeModel
import json
import re
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# Initialize Google Cloud clients
tts_client = texttospeech.TextToSpeechClient()
gcs_client = storage.Client()

# Target Shorts resolution
SHORTS_WIDTH, SHORTS_HEIGHT = 1080, 1920

# Playlist mapping for each category
PLAYLIST_MAP = {
    "pop culture": "PLdQe9EVdFVKZEmVz0g4awpwP5-dmGutGT",  # News/Trends
    "sports": "PLdQe9EVdFVKao0iff_0Nq5d9C6oS63OqR",
    "history": "PLdQe9EVdFVKYxA4D9eXZ39rxWZBNtwvyD",
    "science": "PLdQe9EVdFVKY4-FVQYpXBW2mo-o8y7as3",
    "tech": "PLdQe9EVdFVKZkoqcmP_Tz3ypCfDy1Z_14"
}

def get_credentials(secret_name: str):
    """
    Fetch credentials from Secret Manager and return a Credentials object.
    """
    client = secretmanager.SecretManagerServiceClient()
    project_id = "trivia-machine-472207"  # your GCP project
    secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    
    response = client.access_secret_version(request={"name": secret_path})
    secret_payload = response.payload.data.decode("UTF-8")
    
    creds_dict = json.loads(secret_payload)
    return Credentials.from_authorized_user_info(creds_dict)

# -------------------------
# AI Explanation Generator
# -------------------------
vertexai.init(project=os.getenv("trivia-machine-472207"), location="asia-southeast1")
gemini_model = GenerativeModel("gemini-2.5-flash")

def ai_generate_explanation(question, answer):
    """
    Generates a 2-sentence trivia-style explanation using Vertex AI Gemini.
    """
    prompt = f"""
    Question: {question}
    Correct Answer: {answer}

    Write exactly 2 engaging sentences (maximum 60 words) that explain why {answer} is correct. 
    Make it sound trivia-like and fun, educational, not like a textbook. 
    Include 1 surprising or lesser-known detail if possible. Also, put a <BREAK> at the start 
    of each sentence.
    """
    response = gemini_model.generate_content(prompt)
    return response.text.strip()

# -------------------------
# Trivia Fetcher
# -------------------------
def fetch_trivia_with_explanation():
    """
    Fetch trivia from OpenTDB, TriviaAPI, Gemini Tech, or Gemini News.
    If API source fails, fallback to Gemini general trivia.
    Ensures uniform schema: question, 4 choices, answer, explanation, category, playlist_id.
    """
    def clean_field(value):
        """Safely strip strings or lists of strings."""
        if isinstance(value, list):
            return [v.strip() if isinstance(v, str) else v for v in value]
        if isinstance(value, str):
            return value.strip()
        return value

    def normalize_choice(c):
        """Remove any leading letter + punctuation from Gemini output."""
        if isinstance(c, str):
            return re.sub(r"^[A-Da-d][\)\.\-]\s*", "", c).strip()
        return c

    sources = ["opentdb", "triviaapi", "gemini_tech", "gemini_news"]
    source = random.choice(sources)
    categories = ["pop culture", "sports", "history", "science", "tech"]

    # Default random category
    category = random.choice(categories)
    trivia = None

    try:
        if source == "opentdb":
            category_map = {"pop culture": 11, "sports": 21, "history": 23, "science": 17, "tech": 18}
            url = f"https://opentdb.com/api.php?amount=1&type=multiple&category={category_map.get(category, 9)}"
            resp = requests.get(url, timeout=10).json()
            results = resp.get("results", [])
            if results:
                q = results[0]
                question = html.unescape(q["question"])
                correct = html.unescape(q["correct_answer"])
                incorrects = [html.unescape(c) for c in q["incorrect_answers"]]
                choices = incorrects + [correct]
                random.shuffle(choices)

                trivia = {
                    "question": clean_field(question),
                    "choices": [f"{chr(65+i)}. {clean_field(c)}" for i, c in enumerate(choices)],
                    "answer": clean_field(correct),
                    "category": category,
                    "playlist_id": PLAYLIST_MAP[category]
                }

        if source == "triviaapi":
            category_map = {
                "pop culture": "film_and_tv",
                "sports": "sport_and_leisure",
                "history": "history",
                "science": "science",
                "tech": "science:computers"
            }
            url = f"https://the-trivia-api.com/v2/questions?limit=1&categories={category_map.get(category,'general_knowledge')}"
            resp = requests.get(url, timeout=10).json()
            if resp:
                q = resp[0]
                question = html.unescape(q["question"]["text"])
                correct = html.unescape(q["correctAnswer"])
                incorrects = [html.unescape(c) for c in q["incorrectAnswers"]]
                choices = incorrects + [correct]
                random.shuffle(choices)

                trivia = {
                    "question": clean_field(question),
                    "choices": [f"{chr(65+i)}. {clean_field(c)}" for i, c in enumerate(choices)],
                    "answer": clean_field(correct),
                    "category": category,
                    "playlist_id": PLAYLIST_MAP[category]
                }

        if source == "gemini_tech":
            category = "tech"
            gemini_prompt = """
                Create one multiple-choice trivia question about trending technology, gadgets, AI, or computing.
                Provide 1 correct answer and 3 wrong but plausible answers.
                Respond strictly in JSON:
                {
                  "question": "...",
                  "choices": ["choice1","choice2","choice3","choice4"],
                  "answer": "..."
                }
            """
            trivia = call_gemini_for_trivia(gemini_prompt)

        if source == "gemini_news":
            category = "pop culture"
            gemini_prompt = """
                Create one multiple-choice trivia question about recent global news, current events, or pop culture.
                Provide 1 correct answer and 3 wrong but plausible answers.
                Respond strictly in JSON:
                {
                  "question": "...",
                  "choices": ["choice1","choice2","choice3","choice4"],
                  "answer": "..."
                }
            """
            trivia = call_gemini_for_trivia(gemini_prompt)

        # ✅ Normalize Gemini results
        if source in ["gemini_tech", "gemini_news"] and trivia:
            trivia["question"] = clean_field(html.unescape(trivia.get("question", "")))
            trivia["answer"] = clean_field(html.unescape(trivia.get("answer", "")))

            raw_choices = trivia.get("choices", [])
            if not isinstance(raw_choices, list) or len(raw_choices) != 4:
                raw_choices = ["", "", "", ""]
            raw_choices = [normalize_choice(html.unescape(c)) for c in raw_choices]

            trivia["choices"] = [
                f"{chr(65+i)}. {clean_field(c)}" for i, c in enumerate(raw_choices)
            ]
            trivia["category"] = category
            trivia["playlist_id"] = PLAYLIST_MAP[category]

    except Exception as e:
        print(f"Source {source} failed with error: {e}")

    # Fallback to Gemini general if all else fails
    if not trivia:
        fallback_prompt = f"""
        Create one general trivia question in the category: {category}.
        Provide 1 correct answer and 3 wrong but plausible answers.
        Respond strictly in JSON:
        {{
          "question": "...",
          "choices": ["choice1","choice2","choice3","choice4"],
          "answer": "..."
        }}
        """
        trivia = call_gemini_for_trivia(fallback_prompt)
        if trivia:
            trivia["question"] = clean_field(html.unescape(trivia.get("question", "")))
            trivia["answer"] = clean_field(html.unescape(trivia.get("answer", "")))

            raw_choices = trivia.get("choices", [])
            if not isinstance(raw_choices, list) or len(raw_choices) != 4:
                raw_choices = ["", "", "", ""]
            raw_choices = [normalize_choice(html.unescape(c)) for c in raw_choices]

            trivia["choices"] = [
                f"{chr(65+i)}. {clean_field(c)}" for i, c in enumerate(raw_choices)
            ]
            trivia["category"] = category
            trivia["playlist_id"] = PLAYLIST_MAP[category]

    # Add uniform explanation
    if trivia:
        trivia["explanation"] = ai_generate_explanation(trivia["question"], trivia["answer"])
        print(f"✅ Source used: {source}")
        print("✅ Final trivia object (with explanation):", trivia)
        return trivia

    print("⚠️ Trivia sources exhausted. Last result:", trivia)
    raise RuntimeError("No trivia could be fetched from any source.")

# -------------------------
# Gemini Call
# -------------------------

def call_gemini_for_trivia(prompt: str):
    """Wrapper for Vertex AI Gemini call that returns a fully patterned trivia dict."""
    import json, re, html
    from vertexai.generative_models import GenerativeModel

    model = GenerativeModel("gemini-2.5-flash")

    try:
        response = model.generate_content(prompt)
        raw_text = response.text.strip()
        print("📝 Gemini raw response:", raw_text)

        # Remove ```json``` blocks
        raw_text = re.sub(r"^```json\s*|```$", "", raw_text, flags=re.IGNORECASE).strip()

        trivia = json.loads(raw_text)

        # Ensure proper format for fetch_trivia_with_explanation
        question = html.unescape(trivia.get("question", ""))
        answer = html.unescape(trivia.get("answer", ""))
        raw_choices = [html.unescape(c) for c in trivia.get("choices", [])]

        # Always format with letters
        choices = [f"{chr(65+i)}. {c}" for i, c in enumerate(raw_choices)]

        return {
            "question": question,
            "answer": answer,
            "choices": choices
        }

    except Exception as e:
        print("❌ Gemini call failed:", e)
        return None

# -------------------------
# Speech Synthesis
# -------------------------
def synthesize_speech(text, voice_name="en-AU-Neural2-B", output_gcs_path=None, use_ssml=False):
    def format_ssml(raw_text: str) -> str:
        ssml = raw_text.replace("<BREAK>", '<break time="500ms"/>')
        if not ssml.strip().startswith("<speak>"):
            ssml = f"<speak>{ssml}</speak>"
        ssml = ssml.replace("&", "and").replace("<>", "")
        return ssml

    if use_ssml:
        synthesis_input = texttospeech.SynthesisInput(ssml=format_ssml(text))
    else:
        synthesis_input = texttospeech.SynthesisInput(text=text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name=voice_name
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )

    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(response.audio_content)
        tmp_path = tmp.name

    if output_gcs_path:
        bucket_name, blob_name = output_gcs_path.replace("gs://", "").split("/", 1)
        bucket = gcs_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(tmp_path)

    return tmp_path

# -------------------------
# Helper: split <BREAK>
# -------------------------
def split_text_for_display_and_tts(raw_text, short_pause="100ms"):
    display_text = raw_text
    tts_text = raw_text.replace("<BREAK>", f"<break time='{short_pause}'/>")
    return display_text, tts_text

# -------------------------
# Resize Helper
# -------------------------
def resize_to_shorts(img: Image.Image) -> Image.Image:
    img_ratio = img.width / img.height
    target_ratio = SHORTS_WIDTH / SHORTS_HEIGHT
    if img_ratio > target_ratio:
        new_width = SHORTS_WIDTH
        new_height = int(SHORTS_WIDTH / img_ratio)
    else:
        new_height = SHORTS_HEIGHT
        new_width = int(SHORTS_HEIGHT * img_ratio)
    resized = img.resize((new_width, new_height), Image.LANCZOS)
    canvas = Image.new("RGB", (SHORTS_WIDTH, SHORTS_HEIGHT), "black")
    offset = ((SHORTS_WIDTH - new_width) // 2, (SHORTS_HEIGHT - new_height) // 2)
    canvas.paste(resized, offset)
    return canvas

# -------------------------
# Video Generator
# -------------------------
def create_trivia_video(question, choices, answer, explanation, background_gcs_path, output_gcs_path):
    BASE_FONT_Q = 60
    BASE_FONT_A = 45

    # Download background
    bucket_name, blob_name = background_gcs_path.replace("gs://", "").split("/", 1)
    bucket = gcs_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    bg_tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    blob.download_to_filename(bg_tmp.name)

    # Choices
    choices_text = "\n".join(choices)  # <-- updated to use input argument
    choice_lines = [line.strip() for line in choices_text.splitlines() if line.strip()]
    formatted_choices_display = "<BREAK>".join(choice_lines)
    overlay_text = f"{question}<BREAK><BREAK><BREAK><BREAK>{formatted_choices_display}"

    overlay_display, overlay_tts = split_text_for_display_and_tts(overlay_text, short_pause="200ms")
    tts_text_question = f"<speak>{overlay_tts}</speak>"

    explanation_display, explanation_tts = split_text_for_display_and_tts(explanation, short_pause="200ms")
    tts_text_answer = (
        f"<speak>The answer is: {answer}.<break time='800ms'/>"
        f"{explanation_tts}</speak>"
    )

    # TTS
    audio_tmp_question = synthesize_speech(tts_text_question, use_ssml=True)
    audio_tmp_answer = synthesize_speech(tts_text_answer, use_ssml=True)
    audio_clip_question = AudioFileClip(audio_tmp_question)
    audio_clip_answer = AudioFileClip(audio_tmp_answer)

    img = Image.open(bg_tmp.name).convert("RGB")
    portrait_img = resize_to_shorts(img)
    total_duration = audio_clip_question.duration + 3 + audio_clip_answer.duration
    img_clip = ImageClip(np.array(portrait_img)).set_duration(total_duration)

    # Render text
    def render_text_box(text, max_width, max_height, font_path, max_fontsize, min_fontsize=24, line_spacing=15):
        max_fontsize = int(max_fontsize * 1.2)
        paragraphs = text.split("<BREAK>")
        for fontsize in range(max_fontsize, min_fontsize, -2):
            font = ImageFont.truetype(font_path, fontsize)
            lines = []
            for i, paragraph in enumerate(paragraphs):
                if not paragraph.strip():
                    lines.append(" ")
                    continue
                wrapped = []
                words = paragraph.split()
                current_line = ""
                for word in words:
                    test_line = f"{current_line} {word}".strip()
                    line_width = font.getbbox(test_line)[2]
                    if line_width <= max_width - 100:
                        current_line = test_line
                    else:
                        if current_line:
                            wrapped.append(current_line)
                        current_line = word
                if current_line:
                    wrapped.append(current_line)
                lines.extend(wrapped if wrapped else [" "])
                if i < len(paragraphs) - 1:
                    lines.append("")
            dummy_img = Image.new("RGBA", (max_width, max_height), (0,0,0,0))
            draw = ImageDraw.Draw(dummy_img)
            total_height = sum(draw.textbbox((0,0), line, font=font)[3] for line in lines) + (len(lines)-1)*line_spacing
            if total_height <= max_height - 50:
                break
        img = Image.new("RGBA", (max_width, max_height), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        y = (max_height - total_height)//2
        for line in lines:
            bbox = draw.textbbox((0,0), line, font=font)
            w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
            x = (max_width - w)//2
            border = max(2, fontsize//20)
            for dx in [-border,0,border]:
                for dy in [-border,0,border]:
                    if dx != 0 or dy != 0:
                        draw.text((x+dx, y+dy), line, font=font, fill="black")
            draw.text((x, y), line, font=font, fill=(204,204,0))
            y += h + line_spacing
        return img

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    text_img_q = render_text_box(overlay_display, SHORTS_WIDTH, SHORTS_HEIGHT, font_path, max_fontsize=BASE_FONT_Q)
    text_clip_question = ImageClip(np.array(text_img_q)).set_duration(audio_clip_question.duration + 3)

    # Countdown
    countdown_clips = []
    for i, sec in enumerate(["3","2","1"]):
        countdown_img = Image.new("RGBA", (SHORTS_WIDTH, SHORTS_HEIGHT), (0,0,0,0))
        draw_count = ImageDraw.Draw(countdown_img)
        font_count = ImageFont.truetype(font_path, 350)
        bbox = draw_count.textbbox((0,0), sec, font=font_count)
        w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
        x, y = (SHORTS_WIDTH - w)//2, (SHORTS_HEIGHT - h)//2
        border = 8
        for dx in [-border,0,border]:
            for dy in [-border,0,border]:
                if dx != 0 or dy != 0:
                    draw_count.text((x+dx, y+dy), sec, font=font_count, fill=(255,0,0,180))
        draw_count.text((x, y), sec, font=font_count, fill=(204,204,0,112))
        countdown_clips.append(
            ImageClip(np.array(countdown_img)).set_start(audio_clip_question.duration + i).set_duration(1)
        )

    # Answer + explanation text
    answer_text = f"The answer is:<BREAK>{answer}<BREAK><BREAK>{explanation_display}"
    text_img_a = render_text_box(answer_text, SHORTS_WIDTH, SHORTS_HEIGHT, font_path, max_fontsize=BASE_FONT_A)
    text_clip_answer = ImageClip(np.array(text_img_a)).set_start(audio_clip_question.duration + 3).set_duration(audio_clip_answer.duration)

    # Final composition
    final_clip = CompositeVideoClip([img_clip, text_clip_question, text_clip_answer, *countdown_clips], size=(SHORTS_WIDTH, SHORTS_HEIGHT))
    final_audio = CompositeAudioClip([
        audio_clip_question.set_start(0),
        audio_clip_answer.set_start(audio_clip_question.duration + 3)
    ])
    final_clip = final_clip.set_audio(final_audio)

    video_tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    final_clip.write_videofile(video_tmp.name, fps=24, codec="libx264", audio_codec="aac")
    bucket_name, blob_name = output_gcs_path.replace("gs://","").split("/",1)
    bucket = gcs_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(video_tmp.name)

    return output_gcs_path

# -------------------------
# YouTube Upload Helper
# -------------------------
def upload_video_to_youtube_gcs(gcs_path, title, description, category, credentials, tags=None, privacy="public"):
    # Map categories to YouTube categoryId + your playlistId
    category_map = {
        "pop culture": {"categoryId": "24", "playlistId": "PLdQe9EVdFVKZEmVz0g4awpwP5-dmGutGT"},
        "sports": {"categoryId": "17", "playlistId": "PLdQe9EVdFVKao0iff_0Nq5d9C6oS63OqR"},
        "history": {"categoryId": "22", "playlistId": "PLdQe9EVdFVKYxA4D9eXZ39rxWZBNtwvyD"},
        "science": {"categoryId": "28", "playlistId": "PLdQe9EVdFVKY4-FVQYpXBW2mo-o8y7as3"},
        "tech": {"categoryId": "28", "playlistId": "PLdQe9EVdFVKZkoqcmP_Tz3ypCfDy1Z_14"},
    }

    # Fallback mapping for unexpected categories
    fallback_map = {
        "film": "pop culture",
        "movies": "pop culture",
        "tv": "pop culture",
        "history": "history",
        "science": "science",
        "tech": "tech",
        "computers": "tech",
        "sports": "sports"
    }

    category_key = category.lower()
    if category_key not in category_map:
        # Try to map via fallback, else default to pop culture
        category_key = fallback_map.get(category_key, "pop culture")
        print(f"⚠️ Unknown category '{category}' mapped to '{category_key}'")

    category_id = category_map[category_key]["categoryId"]
    playlist_id = category_map[category_key]["playlistId"]

    # Load YouTube credentials
    youtube = build("youtube", "v3", credentials=credentials)

    # Download video temporarily from GCS
    bucket_name, blob_name = gcs_path.replace("gs://", "").split("/", 1)
    bucket = gcs_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        blob.download_to_filename(tmp.name)
        tmp_path = tmp.name

    # Upload to YouTube
    media_body = MediaFileUpload(tmp_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or ["trivia", "quiz", "fun"],
                "categoryId": category_id
            },
            "status": {"privacyStatus": privacy}
        },
        media_body=media_body
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    os.remove(tmp_path)
    video_id = response["id"]
    print("Upload complete! Video ID:", video_id)

    # Add video to correct playlist
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
    ).execute()
    print(f"Added to playlist {playlist_id}")

    return video_id

def upload_video_to_youtube_wp(video_path, title, description, category, tags=None, privacy="public"):
    """
    Upload a video to the Wordplay channel on YouTube using a separate credentials file.
    """
    creds_file = "wordplay.json"  # Path to second channel's credentials

    youtube = get_authenticated_service(creds_file)  # Use helper to auth with correct creds

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": str(category),
        },
        "status": {"privacyStatus": privacy},
    }

    insert_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True),
    )
    response = insert_request.execute()
    return response["id"]

# -------------------------
# /generate endpoint
# -------------------------
@app.route("/generate", methods=["POST"])
def generate_trivia():
    trivia = fetch_trivia_with_explanation()
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    output_path = f"gs://trivia-videos-output/trivia_{ts}.mp4"

    # Generate the video
    video_path = create_trivia_video(
        question=trivia["question"],
        choices=trivia["choices"],
        answer=trivia["answer"],
        explanation=trivia["explanation"],
        background_gcs_path="gs://trivia-videos-output/background.jpg",
        output_gcs_path=output_path
    )

    # Randomized titles
    title_options = [
        "Did you know?",
        "Trivia Time!",
        "Quick Fun Fact!",
        "Can You Guess This?",
        "Learn Something!",
        "Well Who Knew?",
        "Wow Really?",
        "Fun Fact Alert!",
        "Now You Know!",
        "Not Bad Hmmm!",
        "Mind-Blowing Fact!"
    ]
    youtube_title = random.choice(title_options)

    # Consistent description
    youtube_description = f"{trivia['question']} Did you get it right? What do you think of the fun fact? Now you know! See you at the comments!"

     # Fetch credentials for each channel
    creds_ch1 = get_credentials("Credentials_Trivia")
    creds_ch2 = get_credentials("Credentials_Wordplay")
    
    # Upload to YouTube - CHannel 1
    #video_id = upload_video_to_youtube_gcs(video_path, youtube_title, youtube_description, trivia["category"])
    video_id1 = upload_video_to_youtube_gcs(video_path, youtube_title, youtube_description, trivia["category"], creds_ch1)


    return jsonify({
        "video_gcs": video_path,
        "trivia": trivia,
        "youtube_video_id": video_id1
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
