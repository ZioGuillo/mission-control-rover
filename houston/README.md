# Houston

Houston lets the rovers answer spoken Spanish. Fred (Jetson, unit 2) and Eva (Pi Zero W, unit 1) each stream
their microphone to this Mac over SSH. The Mac transcribes (whisper-server), thinks (Ollama: `gpt-oss:120b`,
vision `qwen3.8:27b`), speaks (Chatterbox) and plays the answer back through the rover's speaker. Everything
runs locally; both servers bind 127.0.0.1 only.

## Requirements

- Apple Silicon Mac with Homebrew: `ollama`, `whisper-cpp` (`/opt/homebrew/bin/whisper-server`), `ffmpeg`.
- Whisper model `ggml-large-v3-turbo.bin` (about 1.6 GB) at `~/Library/Caches/houston/`. Download it once:

      mkdir -p ~/Library/Caches/houston
      curl -L -o ~/Library/Caches/houston/ggml-large-v3-turbo.bin \
          https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

  The path is set in one place, `MODEL` in `houston/install.sh` (whisper-server loads it, not houston).
- Chatterbox's weights already in the Hugging Face cache (`~/.cache/huggingface`). The service runs with
  `HF_HUB_OFFLINE=1` (no network at startup) and `TQDM_DISABLE=1` (no progress bars in the stderr log),
  so the first model download must happen in a run by hand. On this Mac it is already there.
- `uv` (`brew install uv`); the installer builds the service's venv (Python 3.11) itself. For running tests by hand keep a `houston/.venv`.
- Each rover's app password in the file named in `houston/rovers.toml` (for the camera), readable only by
  you: `chmod 600 ~/.config/fred/rover_password`.
- Optional: an api.nasa.gov key in `~/.config/houston/nasa_key` (`chmod 600` too). Without it Houston uses
  NASA's `DEMO_KEY`, which works but is rate-limited and sometimes slow.
- Each rover's SSH host key in `~/.ssh/known_hosts`. SSH runs with `BatchMode=yes`, so it cannot ask
  "are you sure you want to continue connecting?": run `ssh <user@host> true` once by hand for every rover
  (and again whenever its address changes).

Before the first install, stop the spike prototype if it is still running (`fred-spike/houston.py` and its
`tts_worker.py`; find them with `pgrep -fl 'houston.py|tts_worker.py'`, then Ctrl-C its terminal or
`kill` the PIDs). It holds the rover's mic and speaker, and only one program can use them.

## Install / uninstall

    make houston-install      # houston/install.sh
    make houston-uninstall    # houston/install.sh --uninstall

The installer checks the requirements, starts Ollama (`brew services`), pulls the two models if missing,
copies `houston/` (plus the two quindar beeps from `app/audio/`) to `~/Library/Application Support/houston/runtime/`
(the repo lives in iCloud, where launchd jobs may be denied and files evicted), builds the venv at
`~/Library/Application Support/houston/venv`, renders the launchd templates in `houston/launchd/` into
`~/Library/LaunchAgents` and loads them (it first empties `houston.stderr.log` / `whisper.stderr.log` if either
has grown past 10 MB):
`local.missioncontrol.whisper` (whisper-server on 127.0.0.1:8178) and `local.missioncontrol.houston`
(`python -m houston`). Both start at login and restart if they crash (houston: not after a clean exit, 10 s throttle).
**launchd runs the runtime copy, not the repo: every change, code or `rovers.toml`, goes live only after
`make houston-install`** (it redeploys the copy and restarts both jobs). Uninstall removes only the two jobs;
`houston/install.sh --uninstall --purge` also deletes the runtime copy and venv. `install.sh --render-only DIR`
just renders the plists into DIR for inspection.

SSH runs with `BatchMode=yes` and never prompts: each rover's key must work without a passphrase prompt
(unencrypted key, or loaded in the agent/keychain available to the launchd job), and its host key must
already be in `~/.ssh/known_hosts` (see Requirements).

Run by hand (stop the launchd job first, the mic is single-owner): `houston/.venv/bin/python -m houston`.
A crashed rover session is logged and restarted after 5 s; the others keep running.

## Logs

    make houston-logs         # tail -f ~/Library/Logs/houston.log

`houston.log` rotates at 5 MB (3 backups). Crashes outside Python land in `~/Library/Logs/houston.stderr.log`
and `whisper.stderr.log` (houston's stdout is discarded; nothing prints transcripts). A rover that stays
unreachable shows up in `houston.log` as "reconnecting in N s" with the SSH reason (host key, refused,
no route); ssh's own messages land in `houston.stderr.log`.

## Voices

Each rover's `voice` in `rovers.toml` is either a **Kokoro** voice name (`em_alex`, `em_santa`, `ef_dora`:
~20x faster than real time, Fred uses `em_alex`) or a `.wav` sample that **Chatterbox** clones (more human,
but ~2x slower than real time, so long answers stall). Kokoro needs `brew install espeak-ng`.

**Filler sound:** a rover can set `filler_sound` (a mono WAV under `houston/`) to play it instead of
the second "please wait" line. Bentley's `audio/barks_soft.wav` is two barks cut from
["Barking of a dog"](https://commons.wikimedia.org/wiki/File:Barking_of_a_dog.ogg) by Amada44,
CC BY-SA 3.0 (trimmed and made quieter); the derived file is under the same licence.

## Voice commands

- "Hey Fred" / "Oye Eva" plus a question: answer, then the conversation stays open for 60 s without
  repeating the name. Say only the name to get "¿Sí? Te escucho."
- "Gracias" (alone, or "gracias Fred"): closes the conversation.
- "Fred, descansa": that rover stops listening. "Fred, despierta": back on.
- A rover ignores speech addressed to the other one, and no rover listens while any rover is speaking
  (so Fred and Eva in one room never answer each other).
- In a conversation, one-word noises ("Mmm.", "¿Qué?") are ignored; commands like "Gracias" still work.

## Why each rover has an ssh tunnel job

macOS Local Network privacy blocks a launchd-started Python from reaching the LAN ("No route to host"),
even with "Python" allowed in System Settings → Privacy & Security → Local Network; `/usr/bin/ssh`
started by launchd is not blocked. So `make houston-install` also runs one job per rover,
`local.missioncontrol.tunnel.<name>`: an ssh master that owns the connection. Houston's own ssh calls
(mic, speaker) ride its ControlPath socket, and the camera reaches the rover app through its
`-L 127.0.0.1:<tunnel_port>` forward. Its errors go to `~/Library/Logs/houston-tunnel.stderr.log`.

## Add a rover

Add a `[[rover]]` block to `houston/rovers.toml` (`name`, `unit`, `ssh`, `app_url`, `app_user`,
`password_file`, `voice`, and `tunnel_port` = 18000 + unit), put its voice WAV under `houston/voices/`, accept its host key once
(`ssh <user@host> true`), then `make houston-install` (a `launchctl kickstart` alone would restart the old
runtime copy, which never sees the edit). On the rover:

1. Create the app user (name = `app_user`) in the rover app, with the password in `password_file`.
2. SSH user is in the `audio` group.
3. PulseAudio autospawn off (`autospawn = no` in `/etc/pulse/client.conf`), so ALSA stays free.
4. Set mixer levels (mic capture and speaker volume) with `alsamixer`/`amixer`.
5. `sudo alsactl store` so the levels survive reboot.

## Change a rover's address / new network (e.g. the CITIC hotspot)

1. Edit `ssh` and `app_url` for that rover in `houston/rovers.toml`. A `.local` name (e.g.
   `ec2-user@fred.local`, if the rover answers mDNS) survives DHCP changing its IP.
2. `ssh <user@new-host> true` once by hand, to accept the host key (BatchMode can't prompt).
3. `make houston-install`.

If the rover still doesn't answer, look for the SSH failure reason ("Host key verification failed",
"Connection refused", "No route to host") in `~/Library/Logs/houston.stderr.log` (ssh's own messages) and in
`houston.log` (each failed connect, then "reconnecting in N s").

## Privacy

The log holds only wake phrases, the question after the wake, answers, timings and errors. Other speech in
the room is transcribed in memory and dropped; it is never written anywhere. While resting ("Fred, descansa"),
Fred only listens for short phrases, to hear "despierta": anything longer than 2.5 s is dropped without even
being transcribed, and nothing is logged.

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
