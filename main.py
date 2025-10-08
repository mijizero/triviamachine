from google.cloud import texttospeech

def main():
    # Initialize client (credentials auto-detected from Cloud environment)
    client = texttospeech.TextToSpeechClient()

    # The text to synthesize
    text = "Did you know honey never spoils? Archaeologists found 3000-year-old honey in tombs."

    # Configure input
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # Configure voice
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name="en-AU-Neural2-D",
        ssml_gender=texttospeech.SsmlVoiceGender.MALE
    )

    # Configure audio output
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=0.9,
        pitch=0.8,
        volume_gain_db=2.0
    )

    # ✅ Request word-level timepoints
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
        enable_time_pointing=[texttospeech.SynthesizeSpeechRequest.TimepointType.WORD]
    )

    # Write audio to file
    with open("output.mp3", "wb") as out:
        out.write(response.audio_content)
    print("Audio saved to output.mp3")

    # Print word timings
    if response.timepoints:
        print("Word timings:")
        for wp in response.timepoints:
            print(f"{wp.word}: {wp.time_seconds:.2f} sec")

if __name__ == "__main__":
    main()
