# Houston

Houston lets the rovers answer spoken Spanish. Fred (Jetson, unit 2) and Eva (Pi Zero W, unit 1) each stream
their microphone to this Mac over SSH. The Mac transcribes (whisper-server), thinks (Ollama: `gpt-oss:120b`,
vision `qwen3.8:27b`), speaks (Chatterbox) and plays the answer back through the rover's speaker. Everything
runs locally; both servers bind 127.0.0.1 only.

## Requirements

- Apple Silicon Mac with Homebrew: `ollama`, `whisper-cpp` (`/opt/homebrew/bin/whisper-server`), `ffmpeg`.
- Whisper model at `~/Library/Caches/houston/ggml-large-v3-turbo.bin`.
- Python 3.11 venv at `houston/.venv` with `houston/requirements.txt` installed.
- Each rover's app password in the file named in `houston/rovers.toml` (for the camera).

## Install / uninstall

    make houston-install      # houston/install.sh
    make houston-uninstall    # houston/install.sh --uninstall

The installer checks the requirements, starts Ollama (`brew services`), pulls the two models if missing,
renders the launchd templates in `houston/launchd/` into `~/Library/LaunchAgents` and loads them:
`local.missioncontrol.whisper` (whisper-server on 127.0.0.1:8178) and `local.missioncontrol.houston`
(`python -m houston`). Both start at login and restart if they die. Uninstall removes only these two jobs.

Run by hand (stop the launchd job first, the mic is single-owner): `houston/.venv/bin/python -m houston`.
A crashed rover session is logged and restarted after 5 s; the others keep running.

## Logs

    make houston-logs         # tail -f ~/Library/Logs/houston.log

`houston.log` rotates at 5 MB (3 backups). Crashes outside Python land in `~/Library/Logs/houston.stderr.log`
and `whisper.stderr.log`.

## Voice commands

- "Hey Fred" / "Oye Eva" plus a question: answer, then the conversation stays open for 60 s without
  repeating the name. Say only the name to get "¿Sí? Te escucho."
- "Gracias" (alone, or "gracias Fred"): closes the conversation.
- "Fred, descansa": that rover stops listening. "Fred, despierta": back on.
- A rover ignores speech addressed to the other one.

## Add a rover

Add a `[[rover]]` block to `houston/rovers.toml` (`name`, `unit`, `ssh`, `app_url`, `app_user`,
`password_file`, `voice`), put its voice WAV under `houston/voices/`, then restart:
`launchctl kickstart -k gui/$(id -u)/local.missioncontrol.houston`. On the rover:

1. Create the app user (name = `app_user`) in the rover app, with the password in `password_file`.
2. SSH user is in the `audio` group.
3. PulseAudio autospawn off (`autospawn = no` in `/etc/pulse/client.conf`), so ALSA stays free.
4. Set mixer levels (mic capture and speaker volume) with `alsamixer`/`amixer`.
5. `sudo alsactl store` so the levels survive reboot.

## Privacy

The log holds only wake phrases, the question after the wake, answers, timings and errors. Other speech in
the room is transcribed in memory and dropped; it is never written anywhere.

## Benchmarks

Measured 2026-10-28 on the Mac (Apple Silicon), real models, nothing else heavy running.

**Transcriber** (`whisper-server`, `ggml-large-v3-turbo.bin`, `-l es`, localhost): a 3.2 s Paulina `say`
clip, "Hey Fred, ¿cuántos días dura un año en Marte?", came back word-perfect, with punctuation, at
374 ms cold and about 250 ms warm.

**Voice.synth** (Chatterbox Multilingual, `cfg_weight=0.3`, `exaggeration=0.5`, tempo 0.9; model load plus
one warm-up synth: mps 52 s, cpu 17 s):

| device | 3 words (~2 s of audio) | 21 words (~7-8 s of audio) |
|--------|-------------------------|----------------------------|
| mps    | 4.3 s, 5.9 s            | 18.8 s, 11.8 s             |
| cpu    | 8.0 s, 7.6 s            | 15.7 s, 16.0 s             |

Two runs each, in order. mps is faster on short and (warm) long sentences but varies more; cpu is steady at
roughly 2x real time. Default stays mps (`Voice(device=None)` picks it when available); pass `"cpu"` to force.
Synth is slower than real time, so cache fixed lines (`Voice.cached`) and speak long answers sentence by sentence.
