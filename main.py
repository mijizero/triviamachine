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

        payload = {
            "ssml": ssml,
            "voice": "en-US-Neural2-A",
            "encoding": "MP3",
            "enableTimePointing": True
        }

        # Call your TTS endpoint safely
        r = requests.post(TTS_ENDPOINT, json=payload)

        if r.status_code != 200:
            print("TTS endpoint failed:", r.status_code, r.text)
            return jsonify({"error": f"TTS endpoint returned {r.status_code}", "details": r.text}), 500

        try:
            tts_result = r.json()
        except Exception:
            print("Invalid JSON response from TTS endpoint:", r.text)
            return jsonify({"error": "Invalid JSON from TTS endpoint", "raw": r.text}), 500

        audio_b64 = tts_result.get("audioContent")
        marks = tts_result.get("timepoints", [])

        if not audio_b64:
            raise Exception("No audio returned from TTS endpoint")

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

        return send_file(out_path, mimetype="video/mp4")

    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500
