"""A viewer can't pile up camera streams: six leaked MJPEG connections at
30 fps once saturated the rover's Wi-Fi and froze all of them."""
import asyncio

from app.routes.camera import StreamSlots


def test_new_stream_ends_the_viewers_oldest_beyond_the_limit():
    slots = StreamSlots(limit=2)
    a, b = slots.open("mac"), slots.open("mac")
    assert not a.is_set() and not b.is_set()
    c = slots.open("mac")
    assert a.is_set(), "oldest stream should have been told to stop"
    assert not b.is_set() and not c.is_set()
    assert slots.count("mac") == 2


def test_viewers_dont_evict_each_other():
    slots = StreamSlots(limit=2)
    phone = slots.open("phone")
    for _ in range(5):
        slots.open("mac")
    assert not phone.is_set()
    assert slots.count("mac") == 2 and slots.count("phone") == 1


def test_closed_streams_free_their_slot():
    slots = StreamSlots(limit=2)
    a = slots.open("mac")
    slots.close("mac", a)
    assert slots.count("mac") == 0
    b, c = slots.open("mac"), slots.open("mac")
    assert not b.is_set() and not c.is_set()


def test_stream_generator_stops_when_evicted_and_respects_stream_fps(monkeypatch):
    import app.routes.camera as cam

    frames = iter(range(1, 10_000))
    monkeypatch.setattr(cam.camera_driver, "available", True)
    monkeypatch.setattr(cam.camera_driver, "get_frame_if_new", lambda last: (b"jpeg", next(frames)))
    monkeypatch.setattr(cam.settings, "stream_fps", 20)
    monkeypatch.setattr(cam, "_streams", StreamSlots(limit=1))

    class Req:
        scope = {}
        headers = {}
        client = type("C", (), {"host": "10.0.0.5"})()
        session = {}

    async def run():
        resp = await cam.mjpeg_stream(Req())
        gen = resp.body_iterator
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        sent = 0
        async for _chunk in gen:
            sent += 1
            if sent == 5:
                cam._streams.open("10.0.0.5|")  # a newer stream from the same viewer
        return sent, loop.time() - t0

    sent, took = asyncio.run(run())
    # 5 frames, then the eviction is noticed before a 6th goes out
    assert sent == 5
    # 20 fps → at least 4 gaps of 50 ms between the 5 frames
    assert took >= 0.19
