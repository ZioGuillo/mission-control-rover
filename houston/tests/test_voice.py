import numpy as np
import pytest

from houston import voice as voice_mod
from houston.voice import Voice, beep


class FakeTensor:
    def __init__(self, a):
        self.a = a

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class FakeModel:
    sr = 24000

    def __init__(self):
        self.calls = []

    def generate(self, text, **kw):
        self.calls.append((text, kw))
        t = np.arange(24000) / 24000
        return FakeTensor((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)[None, :])


@pytest.fixture
def make(tmp_path):
    model = FakeModel()
    loads = []

    def factory(device):
        loads.append(device)
        return model

    def build(device="cpu"):
        return Voice(device, model_factory=factory, cache_root=tmp_path / "cache"), model, loads

    return build


def test_synth_is_48k_mono_s16_slowed(make):
    v, model, _ = make()
    raw = v.synth("Hola", voice_mod.Path("reed_mx.wav"))
    expected = 1 / 0.9 * 48000 * 2
    assert abs(len(raw) - expected) <= expected * 0.02
    text, kw = model.calls[0]
    assert text == "Hola"
    assert (kw["language_id"], kw["cfg_weight"], kw["exaggeration"]) == ("es", 0.3, 0.5)
    assert str(kw["audio_prompt_path"]) == "reed_mx.wav"


def test_model_loads_lazily_once(make):
    v, _, loads = make("mps")
    assert loads == []
    v.synth("a", voice_mod.Path("x.wav"))
    v.synth("b", voice_mod.Path("x.wav"))
    assert loads == ["mps"]


def test_synth_keeps_pitch(make):
    v, _, _ = make()
    raw = np.frombuffer(v.synth("a", voice_mod.Path("x.wav")), dtype="<i2").astype(float)
    spec = np.abs(np.fft.rfft(raw[4800:-4800]))
    peak = np.argmax(spec) * 48000 / (len(raw) - 9600)
    assert abs(peak - 220) < 8


def test_cached_generates_once_and_survives_new_instance(make, tmp_path):
    v, model, _ = make()
    w = tmp_path / "paulina_mx.wav"
    w.write_bytes(b"a")
    a = v.cached("¿Hola, Fred?", w)
    b = v.cached("¿Hola, Fred?", w)
    assert a == b and len(model.calls) == 1
    files = list((tmp_path / "cache" / "paulina_mx").glob("*.raw"))
    assert len(files) == 1 and files[0].name.startswith("hola_fred")
    v2, model2, _ = make()  # new Voice, same fake model: no further generate call
    assert v2.cached("¿Hola, Fred?", w) == a and len(model2.calls) == 1
    assert not list((tmp_path / "cache").rglob("*.tmp*"))


def test_cache_key_depends_on_text_and_voice(make, tmp_path):
    v, model, _ = make()
    a, b = tmp_path / "reed_mx.wav", tmp_path / "paulina_mx.wav"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    v.cached("hola", a)
    v.cached("hola", b)
    v.cached("hola!", a)
    assert len(model.calls) == 3
    long = "palabra " * 40
    v.cached(long, a)
    v.cached(long + "otra", a)
    assert len(model.calls) == 5
    assert all(len(p.stem) <= 60 + 1 + 12 for p in (tmp_path / "cache").rglob("*.raw"))


def test_beep_is_48k_raw_and_cached():
    for name in ("in", "out"):
        raw = beep(name)
        assert len(raw) % 2 == 0 and len(raw) > 4800
        assert beep(name) is raw


@pytest.mark.parametrize("knob, value", [("TEMPO", 0.8), ("EXAGGERATION", 0.7), ("CFG_WEIGHT", 0.5)])
def test_cache_key_depends_on_voice_settings(make, tmp_path, monkeypatch, knob, value):
    v, model, _ = make()
    w = tmp_path / "reed_mx.wav"
    w.write_bytes(b"1")
    v.cached("hola", w)
    v.cached("hola", w)
    assert len(model.calls) == 1
    monkeypatch.setattr(voice_mod, knob, value)
    v.cached("hola", w)  # a retuned voice must not replay the old renders
    assert len(model.calls) == 2


class FakeKokoro:
    """KPipeline stand-in: yields (graphemes, phonemes, audio) chunks at 24 kHz."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        yield text, "", np.zeros(12000, dtype=np.float32)   # 0.5 s
        yield text, "", np.zeros(12000, dtype=np.float32)   # 0.5 s


def kokoro_voice(tmp_path):
    pipe = FakeKokoro()
    chatter = []
    v = Voice("cpu", model_factory=lambda d: chatter.append(d), cache_root=tmp_path / "cache",
              kokoro_factory=lambda: pipe)
    return v, pipe, chatter


def test_named_voice_uses_kokoro_not_chatterbox(tmp_path):
    v, pipe, chatter = kokoro_voice(tmp_path)
    raw = v.synth("Hola base.", "em_alex")
    assert pipe.calls == [("Hola base.", "em_alex", voice_mod.KOKORO_SPEED)]
    assert chatter == []                      # Chatterbox never loaded
    assert abs(len(raw) - 48000 * 2) < 48000 * 2 * 0.02   # 1 s of 24 kHz → 48 kHz S16 mono


def test_kokoro_cache_is_per_voice_name(tmp_path):
    v, pipe, _ = kokoro_voice(tmp_path)
    v.cached("¿Sí? Te escucho.", "em_alex")
    v.cached("¿Sí? Te escucho.", "em_alex")
    v.cached("¿Sí? Te escucho.", "ef_dora")
    assert [c[1] for c in pipe.calls] == ["em_alex", "ef_dora"]
    assert (tmp_path / "cache" / "em_alex").is_dir()


def test_sound_file_becomes_48k_raw_and_is_cached(tmp_path):
    import wave
    p = tmp_path / "barks.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x01" * 24000)  # 1 s at 24 kHz
    raw = voice_mod.sound(p)
    assert abs(len(raw) - 96000) < 96000 * 0.02
    assert voice_mod.sound(p) is raw
