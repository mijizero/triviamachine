from flask import Flask, request, jsonify
from google.cloud import texttospeech
import tempfile

app = Flask(__name__)

@app.route("/tts", methods=["POST"])
def tts_endpoint():
    data = request.get_json() or {}
    text = data.get("text", "Hello! This is a test of Google Cloud Text-to-Speech.")

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
        enable_time_pointing=[texttospeech.SynthesizeSpeechRequest.TimepointType.WORD]
    )

    # Save audio temporarily
    tmp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_file.write(response.audio_content)
    tmp_file.close()

    # Return word timings and path
    word_times = []
    if response.timepoints:
        word_times = [{"word": wp.word, "time_seconds": wp.time_seconds} for wp in response.timepoints]

    return jsonify({
        "status": "ok",
        "audio_file": tmp_file.name,
        "word_timings": word_times
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
