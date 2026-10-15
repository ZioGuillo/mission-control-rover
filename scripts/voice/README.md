# Mission-control voice

`app/audio/*.wav` are rendered here, not on the robot (it only plays them —
`app/hardware/voice.py`).

```bash
docker run --rm -v "$PWD/../..":/repo -w /repo/scripts/voice python:3.11-slim \
    sh -c "pip install -q piper-tts scipy numpy && python render_voice.py"
```

Linux (Docker) because piper-tts's macOS wheels (1.2–1.8) look for their
espeak-ng data at the path of the machine that built them and fail with
`Error processing file '/Users/runner/…/phontab'`. On Linux a plain
`pip install piper-tts scipy numpy && python render_voice.py` works too.

Voice: Piper `en_US-joe-medium` (training data CC0). Edit `LINES` in
`render_voice.py` to change what it says; the Quindar beeps and the radio
sound are added by the script.
