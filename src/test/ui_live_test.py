#!/usr/bin/env python3
"""A check of the live stream of ui.py. Run it: python3 src/test/ui_live_test.py

It makes a run directory, starts the server, adds speech to two talkgroup
WAVs, and reads the stream. It fails if old speech plays, if new speech does
not arrive, or if ?tg= does not limit the stream.
"""
import os
import sys
import tempfile
import threading
import time
import urllib.request
from array import array
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ui  # noqa: E402

assert ui.mix([array("h", [30000] * ui.BLOCK).tobytes()] * 2) == array("h", [32767] * ui.BLOCK).tobytes()
assert ui.mix([array("h", [5]).tobytes()])[:4] == array("h", [5, 0]).tobytes()

out = tempfile.mkdtemp()
calls = os.path.join(out, "2026-01-01T000000Z", "calls")
os.makedirs(calls)


def add(gssi, value, n=4000):
    """Add n samples to the WAV of a talkgroup. The header is there only for its size."""
    path = os.path.join(calls, "%d.wav" % gssi)
    with open(path, "ab") as f:
        if f.tell() == 0:
            f.write(bytes(ui.HDR))
        f.write(array("h", [value] * n).tobytes())


def listen(query, seconds, got):
    with urllib.request.urlopen("http://127.0.0.1:%d/live.pcm%s" % (port, query)) as r:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            got.frombytes(r.read(ui.BLOCK * 2))


add(1, 111)  # old speech: it is there before the stream starts
add(3, 0, 0)
ui.Handler.recordings = out
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
srv.daemon_threads = True
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

everything, only2 = array("h"), array("h")
threads = [threading.Thread(target=listen, args=a) for a in (("", 4.5, everything), ("?tg=2", 4.5, only2))]
for t in threads:
    t.start()
time.sleep(0.5)
add(1, 1000)  # 1 and 3 speak at the same time
add(3, 200)
time.sleep(1.5)
add(2, 50)  # a talkgroup that the run did not hear before: a new WAV
for t in threads:
    t.join()

assert 111 not in everything, "old speech played"
assert everything.count(1200) == 4000, "the two talkgroups did not mix: %d" % everything.count(1200)
assert everything.count(50) == 4000, "the new talkgroup did not play"
assert only2.count(50) == 4000 and set(only2) == {0, 50}, "?tg= did not limit the stream"
# The stream goes at the speed of a clock: about 4.5 s of sound in 4.5 s.
assert 4.0 * ui.RATE < len(everything) < 5.0 * ui.RATE, len(everything)
print("ui live stream: ok")
