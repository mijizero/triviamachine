import base64
from flask import Flask, jsonify, request
from google.cloud import texttospeech as tts

app = Flask(__name__)

@app.route('/generate', methods=['POST'])
def generate():
    try:
        # 🧠 Sample pages — 2 lines per page
        pages = [
            "Formula One began in 1950. It has grown into a global spectacle.",
            "Each race weekend attracts millions of fans worldwide.",
            "Cars can reach over 350 kilometers per hour.",
            "Drivers push limits of speed, precision, and endurance."
        ]

        # 🧩 Build SSML text with <mark> tags before each 2-line page
        ssml_text = "<speak>"
        for i, page in enumerate(pages):
            ssml_text += f'<mark name="p{i+1}"/>{page} '
        ssml_text += "</speak>"

        # 🎤 Initialize TTS client
        client = tts.TextToSpeechClient()

        # 🗣️ TTS request with timepoint tracking
        response = client.synthesize_speech(
            input=tts.SynthesisInput(ssml=ssml_text),
            voice=tts.VoiceSelectionParams(
                language_code="en-US",
                name="en-US-Neural2-A"
            ),
            audio_config=tts.AudioConfig(audio_encoding=tts.AudioEncoding.MP3),
            enable_time_pointing=[tts.SynthesizeSpeechRequest.TimepointType.SSML_MARK]
        )

        # 💾 Save audio file for testing
        with open("tts_test.mp3", "wb") as out:
            out.write(response.audio_content)
        print("✅ Saved TTS audio as tts_test.mp3")

        # 🕒 Parse mark timestamps
        timepoints = []
        for i, tp in enumerate(response.timepoints):
            start = tp.time_seconds
            end = (
                response.timepoints[i + 1].time_seconds
                if i + 1 < len(response.timepoints)
                else tp.time_seconds + 2.5
            )
            timepoints.append({
                "page": pages[i],
                "start": round(start, 2),
                "end": round(end, 2)
            })

        print("\n📄 Page Timing Segments:")
        for t in timepoints:
            print(f"{t['page']}\n   → between({t['start']}, {t['end']})")

        # Return same structure as your production endpoint
        return jsonify({
            "status": "success",
            "pages": pages,
            "timings": timepoints
        })

    except Exception as e:
        print("❌ Error in /generate:", str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # ✅ Same runtime format as your Cloud Run service
    app.run(host='0.0.0.0', port=8080)
