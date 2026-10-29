# Houston

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
