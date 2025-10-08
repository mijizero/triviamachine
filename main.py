from google.cloud import texttospeech

def main():
    client = texttospeech.TextToSpeechClient()

    # Long hardcoded fact for multiple pages
    fact_text = (
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

    synthesis_input = texttospeech.SynthesisInput(text=fact_text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-AU",
        name="en-AU-Neural2-D",
        ssml_gender=texttospeech.SsmlVoiceGender.MALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
        enable_time_pointing=[texttospeech.TimepointType.WORD]  # ✅ include timings
    )

    # Save the audio
    with open("test_audio.mp3", "wb") as f:
        f.write(response.audio_content)

    # Print word-level timings
    print("Word timings:")
    for wp in response.timepoints:
        print(f"{wp.word} -> {wp.time_seconds:.2f}s")

if __name__ == "__main__":
    main()
