#!/usr/bin/env python3
"""A read-only web overview of a tetra-analyze run.

It parses the files that a run writes and serves one page. It never writes to
the run directory and it never talks to the recorder, so it can be started,
stopped or edited while a capture is going on.

The page can also play the speech live. The server follows the end of each
talkgroup WAV and sends what the recorder adds, see send_live.

usage:
  ui.py [--in DIR] [--bind ADDR] [--port N]

  --in     the parent of the run directories, which is the same directory that
           "run" was given as --out. Default "recordings".
  --bind   the address to listen on. Default 0.0.0.0, which is every network.
           Give the address of one interface to limit it to that network.
  --port   default 8080.

There is no authentication. Anyone who can reach the address can hear the
recordings, so bind it to a network you trust.
"""

import argparse
import calendar
import html
import json
import os
import re
import socket
import struct
import time
from array import array
from bisect import bisect_right
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VFO_RATE = 36000.0  # capture samples for each second, see demod_chain.h
FRAME = 480  # WAV samples for each calls.log line, see stitch.cpp
HDR = 44  # bytes of WAV header, see recorder.cpp
RATE = 8000.0  # WAV sample rate
MISSED = ("OUTSIDE", "BADFREQ", "NOSLOT", "UNTUNED")
TICK = 0.1  # seconds of sound in each block of the live stream
BLOCK = int(RATE * TICK)  # samples in one block
# A voice frame is 60 ms and its write time has jitter. A talkgroup starts to
# play when this much time passed after its first new sample, so a late frame
# does not make a gap in the speech. Tune it if the live speech has gaps.
LIVE_LEAD = 0.3
# The recorder appends to a WAV while a talkgroup speaks. The page shows a
# talkgroup as "talking" for this many seconds after the last append.
TALKING_S = 3


def newest_run(out):
    """The run directory that holds the most recent capture."""
    try:
        runs = [
            d
            for d in os.scandir(out)
            if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}Z", d.name)
        ]
    except OSError:
        return None
    return max(runs, key=lambda d: d.name).path if runs else None


_DAY = {}


def epoch(ts):
    """2026-09-12T22:15:49.076Z as epoch seconds. The field is fixed width, so
    slice it. time.strptime does a locale lookup for every row, and on a log of
    one row each second it was most of the cost of loading the page."""
    day = _DAY.get(ts[:10])
    if day is None:
        day = calendar.timegm((int(ts[0:4]), int(ts[5:7]), int(ts[8:10]), 0, 0, 0, 0, 1, 0))
        _DAY[ts[:10]] = day
    return (day + int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19])
            + (float(ts[19:-1]) if len(ts) > 20 else 0.0))


def tail(path, off, ino):
    """The complete lines added since off, the new offset, the inode, and
    whether the read had to start over. A run appends to these files for hours,
    so re-reading them whole on every refresh is the other half of the cost. A
    changed inode or a file that shrank means start again."""
    reset = False
    try:
        st = os.stat(path)
    except OSError:
        return [], off, ino, reset
    if st.st_ino != ino or st.st_size < off:
        off, reset = 0, ino is not None
    if st.st_size == off:
        return [], off, st.st_ino, reset
    try:
        with open(path, "rb") as f:
            f.seek(off)
            buf = f.read(st.st_size - off)
    except OSError:
        return [], off, st.st_ino, reset
    # The recorder may be part way through a line. Stop at the last newline.
    cut = buf.rfind(b"\n")
    if cut < 0:
        return [], off, st.st_ino, reset
    return (buf[:cut + 1].decode("utf-8", "replace").splitlines(),
            off + cut + 1, st.st_ino, reset)


def parse_clock(path, st):
    """The header of clock.log, its rows as (sample, epoch) for UTC, and the
    two numbers that only the whole file holds: the highest queue the run ever
    reached, and how many times the wall clock stepped. A page that reads the
    last row alone would show a queue of 0 through a spike that dropped data.
    st carries what earlier calls already read, so only new rows are parsed."""
    lines, st["clock_off"], st["clock_ino"], reset = tail(
        path, st["clock_off"], st["clock_ino"])
    if reset:
        st.update(meta={}, rows=[], keys=[], last=None, peak=0, steps=0)
    meta, rows, keys = st["meta"], st["rows"], st["keys"]
    last, peak, steps = st["last"], st["peak"], st["steps"]
    for line in lines:
        if line.startswith("#"):
            for k, v in re.findall(r"(\w+)=(\S+)", line):
                meta[k] = v
            continue
        c = line.split()
        if len(c) < 5:
            continue
        try:
            when = epoch(c[0])
        except (ValueError, IndexError):
            continue
        rows.append((int(c[1]), when))
        keys.append(int(c[1]))
        peak = max(peak, int(c[2]))
        steps += c[4] == "STEP"
        last = c
    st["last"], st["peak"], st["steps"] = last, peak, steps
    return meta, rows, keys, last, peak, steps



def sample_to_utc(rows, keys, sample):
    """A capture sample as a UTC epoch, interpolated inside one clock.log row.
    keys is the sample column of rows, built once: rebuilding it here cost a
    pass over the whole log for every talkgroup and every missed call."""
    if not rows:
        return None
    i = bisect_right(keys, sample) - 1
    if i < 0:
        return rows[0][1] + (sample - rows[0][0]) / VFO_RATE
    if i + 1 < len(rows):
        s0, t0 = rows[i]
        s1, t1 = rows[i + 1]
        if s1 > s0:
            return t0 + (t1 - t0) * (sample - s0) / (s1 - s0)
    s0, t0 = rows[i]
    return t0 + (sample - s0) / VFO_RATE


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)) if epoch else None


def cpu_temp():
    """The CPU temperature, because a host that throttles starts dropping."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def wav_samples(path):
    try:
        return max(0, (os.path.getsize(path) - HDR) // 2)
    except OSError:
        return 0


def mix(parts):
    """One block of the live stream from the blocks of the talkgroups that
    speak at the same time: the sum of the samples, with a limit at 16 bits.
    A short part gets silence at its end."""
    if len(parts) == 1 and len(parts[0]) == BLOCK * 2:
        return parts[0]
    total = [0] * BLOCK
    for part in parts:
        for i, v in enumerate(array("h", part)):
            total[i] += v
    return array("h", (max(-32768, min(32767, v)) for v in total)).tobytes()


# What each run directory has been read up to, so a refresh parses only what
# the recorder appended since the last one.
_STATE = {}


def scan(out):
    """Everything the page shows, from the files of the newest run."""
    run = newest_run(out)
    if not run:
        return {"error": "no run directory under %s" % out}
    st = _STATE.get(run)
    if st is None:
        # Only the newest run is ever shown, so one record is enough.
        _STATE.clear()
        st = {"calls_off": 0, "calls_ino": None, "clock_off": 0, "clock_ino": None,
              "meta": {}, "rows": [], "keys": [], "last": None, "peak": 0, "steps": 0,
              "talkgroups": {}, "carriers": {}, "controls": {}, "missed": [],
              "missed_total": 0}
        _STATE[run] = st

    meta, rows, keys, last, peak_queue, steps = parse_clock(os.path.join(run, "clock.log"), st)
    center = float(meta.get("center", 0)) or None
    iq_rate = float(meta.get("iq_rate", 0)) or None

    lines, st["calls_off"], st["calls_ino"], reset = tail(
        os.path.join(run, "calls.log"), st["calls_off"], st["calls_ino"])
    if reset:
        st.update(talkgroups={}, carriers={}, controls={}, missed=[], missed_total=0)
    talkgroups, carriers = st["talkgroups"], st["carriers"]
    controls, missed = st["controls"], st["missed"]
    # Each line of calls.log is one voice frame, or one call that was not taken.
    for line in lines:
        if line.startswith("#"):
            continue
        c = line.split()
        if len(c) < 9:
            continue
        sample, issi, gssi, hz, ctl, tn, status = (
            int(c[0]),
            c[2],
            c[3],
            c[4],
            c[5],
            c[6],
            c[8],
        )
        if status in MISSED:
            st["missed_total"] += 1
            missed.append(
                {
                    "sample": sample,
                    "gssi": gssi,
                    "issi": issi,
                    "hz": int(hz),
                    "control_hz": int(ctl),
                    "status": status,
                }
            )
            # The page shows the last 200, so older ones need not be kept.
            if len(missed) > 400:
                del missed[:200]
            continue
        t = talkgroups.setdefault(
            gssi,
            {
                "gssi": gssi,
                "frames": 0,
                "first": sample,
                "last": sample,
                "carriers": {},
                "issis": {},
            },
        )
        t["frames"] += 1
        t["last"] = sample
        t["carriers"][hz] = t["carriers"].get(hz, 0) + 1
        if issi != "-":
            t["issis"][issi] = t["issis"].get(issi, 0) + 1
            if issi != "0":
                t["talker"] = issi
        carriers[hz] = carriers.get(hz, 0) + 1
        if ctl != "-":
            controls[ctl] = controls.get(ctl, 0) + 1

    out_tg = []
    for base in talkgroups.values():
        t = dict(base, carriers=dict(base["carriers"]), issis=dict(base["issis"]))
        out_tg.append(t)
        wav = os.path.join(run, "calls", t["gssi"] + ".wav")
        t["seconds"] = wav_samples(wav) / RATE
        t["first_utc"] = iso(sample_to_utc(rows, keys, t["first"]))
        t["last_utc"] = iso(sample_to_utc(rows, keys, t["last"]))
        t["carriers"] = sorted(
            ({"hz": int(h), "frames": n} for h, n in t["carriers"].items()),
            key=lambda x: -x["frames"],
        )
        t["issis"] = sorted(
            ({"issi": i, "frames": n} for i, n in t["issis"].items()),
            key=lambda x: -x["frames"],
        )[:4]
        try:
            wav_st = os.stat(wav)
        except OSError:
            wav_st = None
        t["has_audio"] = wav_st is not None
        t["talking"] = bool(wav_st and time.time() - wav_st.st_mtime < TALKING_S)
    talkgroups = {t["gssi"]: t for t in out_tg}

    for m in missed:
        m["utc"] = iso(sample_to_utc(rows, keys, m["sample"]))

    # The pool, as the run was given it and as grants filled it.
    followed = sorted(int(h) for h in carriers)
    # RUN/carriers holds every carrier a slot follows. A line marked "learned"
    # is one that a grant revealed; the rest came from the command line.
    pool, learned = [], []
    try:
        with open(os.path.join(run, "carriers")) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                part = line.split()
                try:
                    hz = int(part[0])
                except ValueError:
                    continue
                pool.append(hz)
                if len(part) > 1 and part[1] == "learned":
                    learned.append(hz)
    except OSError:
        pass

    half = iq_rate / 2 if iq_rate else None
    span = {
        "center": center,
        "low": center - half if half else None,
        "high": center + half if half else None,
        # Past 80% of Nyquist the dongle rolls off and a carrier loses SNR.
        "good_low": center - 0.8 * half if half else None,
        "good_high": center + 0.8 * half if half else None,
    }

    # Every frequency worth drawing in the band, in span or out of it.
    marks = {}
    for h, n in carriers.items():
        marks[int(h)] = {"hz": int(h), "frames": n, "role": "carrier", "gssis": []}
    for h in controls:
        marks.setdefault(
            int(h), {"hz": int(h), "frames": 0, "role": "carrier", "gssis": []}
        )["role"] = "control"
    for m in missed:
        mk = marks.setdefault(
            m["hz"], {"hz": m["hz"], "frames": 0, "role": m["status"], "gssis": []}
        )
        if mk["role"] in MISSED and m["gssi"] not in mk["gssis"]:
            mk["gssis"].append(m["gssi"])
    for mk in marks.values():
        mk["in_span"] = bool(half and abs(mk["hz"] - center) + 15e3 < half)

    health = {}
    if last:
        health = {
            "utc": last[0],
            "samples": int(last[1]),
            "queue": int(last[2]),
            "dropped": int(last[3]),
            "event": last[4],
            "peak_queue": peak_queue,
            "steps": steps,
            "temp_c": cpu_temp(),
            "seconds": int(last[1]) / VFO_RATE,
        }

    return {
        "run": os.path.basename(run),
        # The recorder writes one row of clock.log each second while it runs.
        "active": bool(last and time.time() - epoch(last[0]) < 15),
        "start_utc": meta.get("start_utc"),
        "iq_rate": iq_rate,
        "span": span,
        "health": health,
        # Sorted by GSSI, not by activity: a row that moves under the cursor is
        # both annoying and, for a row holding an <audio>, a stopped playback.
        "talkgroups": sorted(talkgroups.values(), key=lambda t: int(t["gssi"])),
        "carriers": followed,
        "pool": pool,
        "learned": learned,
        "controls": sorted(int(h) for h in controls),
        "marks": sorted(marks.values(), key=lambda m: m["hz"]),
        "missed": missed[-200:],
        "missed_total": st["missed_total"],
        "generated": iso(time.time()),
    }


def local_addresses():
    """Every IPv4 address this host answers on, so the start line prints a URL
    that can be pasted. The name of the host is no good: on Debian it resolves
    to 127.0.1.1. A connected UDP socket finds only the address of the default
    route, which misses a second network such as an access point."""
    found = []
    try:
        import fcntl

        SIOCGIFADDR = 0x8915  # Linux
        for _, name in socket.if_nameindex():
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = fcntl.ioctl(
                    sk.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15])
                )
                found.append(socket.inet_ntoa(packed[20:24]))
            except OSError:
                pass  # an interface with no IPv4 address of its own
            finally:
                sk.close()
    except (ImportError, AttributeError, OSError):
        pass
    if not found:
        # Not Linux. One address beats none: the one that would carry a packet
        # out. Connecting a UDP socket sends nothing, so the host is never used.
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.connect(("192.0.2.1", 9))  # TEST-NET-1, which is never routed
            found.append(sk.getsockname()[0])
        except OSError:
            pass
        finally:
            sk.close()
    # Loopback last: it is a true answer but it is not the one to paste.
    return sorted(set(found), key=lambda x: (x.startswith("127."), x))


class Handler(BaseHTTPRequestHandler):
    recordings = "recordings"
    server_version = "tetra-analyze-ui"

    # A browser abandons a request whenever it seeks in the audio, pauses it,
    # or leaves the page, and the next write to that socket fails. That is
    # ordinary traffic for a media element, not a fault, so it must not put a
    # traceback in the log of a program somebody is watching.
    GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except self.GONE:
            self.close_connection = True

    def log_message(self, fmt, *args):
        pass  # one line for each request is noise, not a log

    def do_GET(self):
        if self.path.split("?")[0] == "/api/overview":
            body = json.dumps(scan(self.recordings)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/audio/"):
            self.send_audio()
        elif self.path.split("?")[0] == "/live.pcm":
            self.send_live()
        elif self.path.split("?")[0] in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def send_live(self):
        """The speech of the newest run while the recorder writes it: 16-bit
        mono samples at 8 kHz, little-endian, with no header and no end.
        ?tg=1,2 limits the stream to those talkgroups; without it, the stream
        has all of them. The stream goes at the speed of a clock and sends
        silence when nobody speaks, so the page can play each block when it
        comes."""
        query = self.path.partition("?")[2]
        want = set()
        for item in query.split("&"):
            if item.startswith("tg="):
                want = {g for g in item[3:].split(",") if g.isdigit()}
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # nginx must not hold blocks
        self.end_headers()
        silence = bytes(BLOCK * 2)
        run, offs, pend, ready = None, {}, {}, {}
        # A WAV that is there at the start holds old speech, so the stream
        # starts at its end. A WAV that comes later is new from its first sample.
        old = True
        due, n = time.monotonic(), 0
        while True:
            if n % 10 == 0:  # each second: a new run, or a new talkgroup
                newest = newest_run(self.recordings)
                if newest != run:
                    run, offs, pend, ready = newest, {}, {}, {}
                try:
                    files = list(os.scandir(os.path.join(run, "calls"))) if run else []
                except OSError:
                    files = []
                for f in files:
                    gssi = f.name[:-4]
                    if not re.fullmatch(r"\d+\.wav", f.name) or gssi in offs:
                        continue
                    if want and gssi not in want:
                        continue
                    try:
                        offs[gssi] = (f.path, max(HDR, f.stat().st_size) if old else HDR)
                    except OSError:
                        pass
                old = False
            n += 1
            now = time.monotonic()
            parts = []
            for gssi, (path, off) in offs.items():
                try:
                    more = (os.path.getsize(path) - off) & ~1  # whole samples only
                    if more > 0:
                        with open(path, "rb") as f:
                            f.seek(off)
                            data = f.read(more)
                        data = data[:len(data) & ~1]
                        offs[gssi] = (path, off + len(data))
                        pend[gssi] = pend.get(gssi, b"") + data
                except OSError:
                    pass
                buf = pend.get(gssi)
                if not buf:
                    ready.pop(gssi, None)
                    continue
                if now < ready.setdefault(gssi, now + LIVE_LEAD):
                    continue
                parts.append(buf[:BLOCK * 2])
                pend[gssi] = buf[BLOCK * 2:]
            self.wfile.write(mix(parts) if parts else silence)
            self.wfile.flush()
            due += TICK
            time.sleep(max(0.0, due - time.monotonic()))

    def send_audio(self):
        """A WAV of one talkgroup, with Range so a browser can seek in it."""
        name = os.path.basename(self.path[len("/audio/") :].split("?")[0])
        if not re.fullmatch(r"\d+\.wav", name):
            return self.send_error(404)
        run = newest_run(self.recordings)
        path = os.path.join(run, "calls", name) if run else None
        if not path or not os.path.exists(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                if m.group(1):
                    start = min(int(m.group(1)), size - 1)
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
        length = max(0, end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


PAGE = r"""<!doctype html><meta charset="utf-8"><title>tetra-analyze</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='13' font-size='13'>&#128225;</text></svg>">
<style>
:root{--bg:#12151a;--fg:#e6e9ef;--dim:#8b93a3;--line:#242a34;--ok:#4ea564;
--warn:#d2a24c;--bad:#d2645c;--ctl:#5b9dd9;--car:#4ea564}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,
SFMono-Regular,Menlo,monospace;padding:16px;-webkit-font-smoothing:antialiased}
h1{font-size:15px;margin:0 0 2px;font-weight:600}
h2{font-size:13px;margin:22px 0 8px;color:var(--dim);font-weight:600;
text-transform:uppercase;letter-spacing:.08em}
.sub{color:var(--dim);margin-bottom:14px}
.strip{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:6px}
.cell{background:#181c23;border:1px solid var(--line);border-radius:8px;
padding:8px 12px;min-width:104px;position:relative}
.cell b{display:block;font-size:17px;font-weight:600;
font-variant-numeric:tabular-nums}
.cell span{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.06em}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.06em}
td.num,th.num{text-align:right;padding-right:16px}
.wrap{overflow-x:auto}
audio{height:28px;vertical-align:middle}
.empty{color:var(--dim);padding:8px 0}

/* The band. The hatched ground is what the dongle does not receive; the solid
   block is the captured span; the lighter block inside it is clear of the
   roll-off. */
.band{position:relative;height:136px;border:1px solid var(--line);
border-radius:8px;overflow:hidden;
background:repeating-linear-gradient(135deg,#141821,#141821 5px,#171c26 5px,#171c26 10px)}
.covered,.clear{position:absolute;top:0;bottom:30px}
.covered{background:#1b2230;box-shadow:inset 1px 0 0 #39465a,inset -1px 0 0 #39465a}
.clear{background:#232d3d}
.ctr{position:absolute;top:0;bottom:30px;border-left:1px dashed #55637a}
.zone{position:absolute;top:6px;font-size:10px;letter-spacing:.04em;color:#5d6a7d;
text-align:center;white-space:nowrap;overflow:hidden;pointer-events:none}
.hit{position:absolute;top:0;bottom:30px}
.mark{position:absolute;bottom:30px;width:2px;background:var(--car);border-radius:1px}
.mark.control{background:var(--ctl);width:3px}
.mark.miss{background:var(--bad)}
.lbl{position:absolute;font-size:10px;color:var(--dim);
transform:translateX(-50%);white-space:nowrap;font-variant-numeric:tabular-nums}
.tag{position:absolute;top:6px;font-size:10px;letter-spacing:.06em;
text-transform:uppercase;color:#6d7a8d;transform:translateX(-50%)}
.scale{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;
margin:6px 2px 0;font-variant-numeric:tabular-nums}
.scale b{color:var(--fg);font-weight:600}
.legend{color:var(--dim);font-size:11px;margin-bottom:6px}
/* One outline glyph on currentColor. It marks where an explanation is; the
   whole block stays the hover target, so the glyph never takes the mouse. */
.info{position:absolute;top:6px;right:7px;color:#414c5c;line-height:0;
pointer-events:none;transition:color 150ms cubic-bezier(.2,0,0,1)}
.cell:hover .info{color:var(--dim)}
/* On a heading the text and the glyph together are the target, padded out so
   it is comfortable to hit without reaching across the row. */
.ih{cursor:help;display:inline-flex;align-items:center;gap:6px;
padding:7px 6px;margin:-7px -6px;border-radius:6px;
transition:color 150ms cubic-bezier(.2,0,0,1)}
.ih:hover{color:var(--fg)}
.ih .info{position:static}
.ih:hover .info{color:var(--dim)}
.sw{display:inline-block;width:9px;height:9px;border-radius:2px;
margin:0 4px 0 12px;vertical-align:baseline;box-shadow:inset 0 0 0 1px #39465a}
.sw:first-child{margin-left:0}
.hatch{background:repeating-linear-gradient(135deg,#141821,#141821 2px,#1b212c 2px,#1b212c 4px);
border:1px solid #2a3240}

/* The disclosure. 40px tall so it is a comfortable target in a dense page. */
details{border:1px solid var(--line);border-radius:8px;background:#181c23}
summary{list-style:none;cursor:pointer;padding:0 12px;height:40px;
display:flex;align-items:center;gap:8px;color:var(--fg);border-radius:7px}
summary::-webkit-details-marker{display:none}
summary:hover{background:#1d222b}
summary .caret{color:var(--dim);transition:rotate 150ms cubic-bezier(.2,0,0,1)}
details[open] summary .caret{rotate:90deg}
summary .count{color:var(--dim);font-variant-numeric:tabular-nums}
.panel{padding:0 12px 10px;max-height:340px;overflow:auto}

/* The live bar stays at the top of the window, so the button and the name of
   the talkgroup that speaks are there at each scroll position. */
.pill{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim);
margin-left:8px;vertical-align:1px}
.pill.on{color:var(--ok);border-color:#2c5a3a}
.live{position:sticky;top:8px;z-index:5;display:flex;align-items:center;gap:14px;
flex-wrap:wrap;background:#181c23;border:1px solid var(--line);border-radius:10px;
padding:10px 12px;margin:0 0 12px;box-shadow:0 8px 18px -10px #000}
#livebtn{font:inherit;font-weight:600;color:var(--fg);background:#232d3d;
border:1px solid #39465a;border-radius:8px;height:40px;padding:0 16px;
display:inline-flex;align-items:center;gap:9px;cursor:pointer;white-space:nowrap;
transition:background 150ms cubic-bezier(.2,0,0,1),border-color 150ms cubic-bezier(.2,0,0,1),
scale 100ms cubic-bezier(.2,0,0,1)}
#livebtn:hover{background:#2a3649}
#livebtn:active{scale:.97}
#livebtn[aria-pressed=true]{background:#3a2020;border-color:#7a3a35}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim)}
#livebtn[aria-pressed=true] .dot{background:var(--bad);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
.meter{width:90px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.meter i{display:block;height:100%;width:0;background:var(--ok)}
.livetxt{min-width:0;flex:1 1 240px}
.livetxt div{overflow-wrap:anywhere}
.dim{color:var(--dim);font-size:12px}
.link{font:inherit;color:var(--ctl);background:none;border:0;padding:0;cursor:pointer;
text-decoration:underline}
/* The mark before a GSSI is green while the recorder adds speech to its WAV. */
.tk{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:8px;
background:#2a3240}
tr.talking .tk{background:var(--ok);animation:pulse 1.4s ease-in-out infinite}
tr.talking td{background:#16211b}
/* The label is the target of the checkbox, 28px, so it is easy to hit. */
.pick{display:inline-flex;width:28px;height:28px;align-items:center;
justify-content:center;cursor:pointer;margin:-4px 0}
.pick input{accent-color:var(--ctl);width:15px;height:15px;margin:0;cursor:pointer}
:focus-visible{outline:2px solid var(--ctl);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
<h1>tetra-analyze<span class="pill" id="pill" hidden></span></h1>
<div class="sub" id="sub">loading&hellip;</div>
<div class="live">
  <button id="livebtn" type="button" aria-pressed="false"><span class="dot"></span><span
    id="livelbl">Listen live</span></button>
  <div class="meter" aria-hidden="true"><i id="level"></i></div>
  <div class="livetxt">
    <div id="livewho" aria-live="polite">Play the speech when it comes in.</div>
    <div class="dim"><span id="livesel"></span>
      <button id="liveall" type="button" class="link" hidden>Listen to all</button></div>
  </div>
</div>
<div class="strip" id="strip"></div>

<h2 id="h-band">Band covered</h2>
<div class="legend">
<span class="sw hatch"></span>not received
<span class="sw" style="background:#1b2230"></span>captured span
<span class="sw" style="background:#232d3d"></span>clear of the roll-off
<span class="sw" style="background:var(--ctl)"></span>control carrier
<span class="sw" style="background:var(--car)"></span>traffic carrier
<span class="sw" style="background:var(--bad)"></span>granted, not recorded</div>
<div class="band" id="band"></div>
<div class="scale" id="scale"></div>

<h2 id="h-tg">Talkgroups</h2>
<div class="wrap"><table id="tg"></table></div><div class="empty" id="tg-empty"></div>

<h2 id="h-car">Carriers</h2>
<div class="wrap"><table id="car"></table></div><div class="empty" id="car-empty"></div>

<h2 id="h-miss">Calls not recorded</h2>
<details id="missbox">
  <summary><span class="caret">&#9654;</span><span>Calls not recorded</span>
    <span class="count" id="misscount"></span></summary>
  <div class="panel">
    <div class="wrap"><table id="miss"></table></div><div class="empty" id="miss-empty"></div>
  </div>
</details>
<script>
const INFO = '<svg class="info" viewBox="0 0 16 16" width="13" height="13" fill="none" '
  + 'stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
  + '<circle cx="8" cy="8" r="6.25"/><path d="M8 7.4v3.9" stroke-linecap="round"/>'
  + '<circle cx="8" cy="4.8" r=".55" fill="currentColor" stroke="none"/></svg>';
const head = (id, text, tip) => {
  const h = document.getElementById(id);
  if (h) h.innerHTML = '<span class="ih" title="'+esc(tip)+'">'+text+INFO+'</span>';
};
const mhz = h => (h/1e6).toFixed(4);
const dur = s => { s=Math.round(s); const h=(s/3600|0), m=(s/60|0)%60;
  return h? h+"h"+String(m).padStart(2,"0")+"m" : m? m+"m"+String(s%60).padStart(2,"0")+"s" : s+"s"; };
// The run date is in the header, so a time of day is enough in the tables.
const hhmm = s => s ? s.slice(11) : "—";
const esc = s => String(s==null?"":s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// Rows are keyed and updated in place. Rebuilding the table would take the
// <audio> elements out of the document, which stops whatever is playing.
function tbl(id, cols, rows, empty, keyOf){
  const el = document.getElementById(id), note = document.getElementById(id+"-empty");
  note.textContent = rows.length ? "" : empty;
  el.hidden = !rows.length; note.hidden = !!rows.length;
  if (!el.tHead) el.createTHead().innerHTML = "<tr>" +
    cols.map(c=>'<th class="'+(c.cls||"")+'">'+c.h+"</th>").join("") + "</tr>";
  const body = el.tBodies[0] || el.createTBody();
  const keep = new Set(rows.map(r=>String(keyOf(r))));
  for (const tr of [...body.children]) if (!keep.has(tr.dataset.k)) tr.remove();
  rows.forEach((r,i) => {
    const k = String(keyOf(r));
    let tr = body.querySelector('tr[data-k="'+k+'"]');
    if (!tr){
      tr = document.createElement("tr"); tr.dataset.k = k;
      for (const c of cols){
        const td = document.createElement("td");
        td.className = c.cls || ""; td.innerHTML = c.f(r); tr.appendChild(td);
      }
      body.insertBefore(tr, body.children[i] || null);
      return;
    }
    cols.forEach((c,j) => {
      if (c.once) return;             // holds an <audio>; leave it alone
      const v = c.f(r);
      if (tr.cells[j].innerHTML !== v) tr.cells[j].innerHTML = v;
    });
  });
}

// text needs about 0.62% of the width for each character at this size
const zone = (left, w, text) => w > text.length*0.62 + 1.5
  ? '<div class="zone" style="left:'+left+'%;width:'+w+'%">'+text+'</div>' : '';

function band(d){
  const el = document.getElementById("band"), sc = document.getElementById("scale");
  const s = d.span;
  if (!s || !s.center){
    el.innerHTML = '<div class="empty" style="padding:12px">no centre recorded in clock.log</div>';
    sc.textContent = ""; return;
  }
  const width = s.high - s.low, half = width/2;
  // A BADFREQ is a decode error, not a frequency, so it is not on the band at
  // all. One of them measured 1090150000 Hz, which would flatten the span to a
  // sliver if it were allowed to set the scale.
  const marks = d.marks.filter(m => m.role !== "BADFREQ");
  // Widen for anything granted outside the span, but never far enough that the
  // span itself stops being the subject of the picture.
  let need = half*1.12;
  for (const m of marks) need = Math.max(need, Math.abs(m.hz - s.center) + half*0.12);
  need = Math.min(need, half*3);
  const lo = s.center - need, hi = s.center + need;
  const x = h => Math.max(0, Math.min(100, ((h - lo)/(hi - lo))*100));
  const L = x(s.low), H = x(s.high), GL = x(s.good_low), GH = x(s.good_high);
  const tipSpan = "Captured span " + mhz(s.low) + " to " + mhz(s.high) +
    " MHz. The dongle receives all of this at once, and a carrier outside it "
    + "cannot be recorded at any setting.";
  const tipClear = "Clear of the roll-off: inside 80% of Nyquist, where a carrier "
    + "keeps its full sensitivity. " + mhz(s.good_low) + " to " + mhz(s.good_high) + " MHz.";
  const tipRoll = "Roll-off: still inside the span and still decoded, but the filter "
    + "of the dongle is falling away here, so a carrier loses SNR.";
  const tipOut = "Not received. The dongle is not listening to these frequencies, so a "
    + "grant naming one is reported but cannot be recorded.";
  el.innerHTML =
    '<div class="covered" style="left:'+L+'%;width:'+(H-L)+'%"></div>' +
    '<div class="clear" style="left:'+GL+'%;width:'+(GH-GL)+'%"></div>' +
    '<div class="ctr" style="left:'+x(s.center)+'%"></div>' +
    '<div class="tag" style="left:'+((L+H)/2)+'%">'+(width/1e6).toFixed(2)+' MHz span</div>' +
    // Naming each region in place, because a legend swatch does not say which
    // block of the picture it stands for.
    // A label is only drawn where it fits. A clipped word reads as a fault,
    // and the region still carries the whole sentence on hover.
    zone(0, L, "not received") + zone(H, 100-H, "not received") +
    zone(L, GL-L, "roll-off") + zone(GH, H-GH, "roll-off") +
    // Transparent strips carry the tooltips; the labels above ignore the mouse.
    '<div class="hit" style="left:0;width:'+L+'%" title="'+tipOut+'"></div>' +
    '<div class="hit" style="left:'+H+'%;width:'+(100-H)+'%" title="'+tipOut+'"></div>' +
    '<div class="hit" style="left:'+L+'%;width:'+(GL-L)+'%" title="'+tipRoll+'"></div>' +
    '<div class="hit" style="left:'+GH+'%;width:'+(H-GH)+'%" title="'+tipRoll+'"></div>' +
    '<div class="hit" style="left:'+GL+'%;width:'+(GH-GL)+'%" title="'+tipClear+'"></div>';
  el.title = tipSpan;
  // Two rows of labels, because carriers sit 25 kHz apart and their text does not.
  const lastAt = [-99,-99];
  for (const m of marks){
    const miss = !["carrier","control"].includes(m.role), px = x(m.hz);
    const bar = document.createElement("div");
    bar.className = "mark" + (m.role==="control"?" control":"") + (miss?" miss":"");
    bar.style.left = px+"%";
    // A control carrier decodes no voice frame of its own, so give it a height
    // that says "always on" instead of one that says "silent".
    bar.style.height = m.role==="control" ? "66px"
      : miss ? "36px" : (20 + Math.min(48, Math.log10(1+m.frames)*20))+"px";
    bar.title = m.hz + " Hz — " + (m.role==="control" ? "control carrier" :
      m.role==="carrier" ? m.frames+" frames" :
      m.role + (m.gssis.length? " — GSSI "+m.gssis.join(", ") : ""));
    el.appendChild(bar);
    const row = (px-lastAt[0] >= 5.5) ? 0 : (px-lastAt[1] >= 5.5) ? 1 : -1;
    if (row < 0) continue;            // no room; the tooltip still carries it
    lastAt[row] = px;
    const lb = document.createElement("div");
    lb.className = "lbl"; lb.style.left = px+"%"; lb.style.bottom = row? "3px":"15px";
    lb.textContent = mhz(m.hz);
    lb.style.color = miss ? "var(--bad)" : m.role==="control" ? "var(--ctl)" : "";
    el.appendChild(lb);
  }
  const out = marks.filter(m=>!m.in_span).length;
  sc.innerHTML = '<span>' + mhz(s.low) + ' MHz</span>' +
    '<span>centre <b>' + mhz(s.center) + ' MHz</b>' +
    (out? ' &nbsp; <span style="color:var(--bad)">' + out +
          ' granted outside the span</span>' : '') + '</span>' +
    '<span>' + mhz(s.high) + ' MHz</span>';
}

// ---- Live mode. The server sends 16-bit samples at 8 kHz with no end, and
// silence when nobody speaks. Each block plays immediately after the one before.
const $ = id => document.getElementById(id);
const KEY = "tetra-live-talkgroups";
// The talkgroups that live mode plays. Empty means all of them.
let sel = new Set();
try { sel = new Set(JSON.parse(localStorage.getItem(KEY) || "[]").map(String)); } catch(e){}
let D = null;                 // the last overview
let ctx = null, analyser = null, level = null;
let liveOn = false, conn = null, lost = false, playAt = 0, carry = null, again = 0;
// Sound that the page holds before it plays. It hides the jitter of the network.
const LEAD = 0.25;

function play(bytes){
  if (carry){                 // a block can end in the middle of a sample
    const joined = new Uint8Array(carry.length + bytes.length);
    joined.set(carry); joined.set(bytes, carry.length); bytes = joined;
  }
  const n = bytes.length >> 1;
  carry = bytes.length & 1 ? bytes.slice(n*2) : null;
  if (!n || !ctx) return;
  const view = new DataView(bytes.buffer, bytes.byteOffset, n*2);
  const buf = ctx.createBuffer(1, n, 8000), ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) ch[i] = view.getInt16(i*2, true) / 32768;
  const now = ctx.currentTime;
  if (playAt < now + 0.02) playAt = now + LEAD;   // the start, or the sound ran out
  if (playAt > now + 3) return;                   // the tab slept: drop the old sound
  const src = ctx.createBufferSource();
  src.buffer = buf; src.connect(analyser); src.start(playAt);
  playAt += n / 8000;
}

async function connect(){
  if (conn) conn.abort();
  const my = conn = new AbortController();
  carry = null; playAt = 0;
  try {
    const r = await fetch("live.pcm?tg=" + [...sel].join(","), {signal: my.signal, cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    lost = false; who();
    const rd = r.body.getReader();
    for (;;){
      const {done, value} = await rd.read();
      if (done) break;
      if (conn === my) play(value);
    }
  } catch(e){}
  if (!liveOn || conn !== my) return;             // stopped, or a newer stream took over
  lost = true; who();
  setTimeout(() => { if (liveOn && conn === my) connect(); }, 1500);
}

function meter(){
  if (!ctx){ $("level").style.width = "0"; return; }
  analyser.getByteTimeDomainData(level);
  let peak = 0;
  for (const v of level) peak = Math.max(peak, Math.abs(v - 128));
  $("level").style.width = Math.min(100, peak / 128 * 140) + "%";
  requestAnimationFrame(meter);
}

// The line next to the button: who speaks now, or why there is no sound.
function who(){
  const el = $("livewho");
  const act = !D || D.active;
  if (!liveOn){
    el.textContent = act ? "Play the speech when it comes in."
      : "No run is active. Live mode plays speech only while a run records.";
    return;
  }
  if (lost){ el.textContent = "The stream stopped. The page tries again."; return; }
  const now = (D ? D.talkgroups : []).filter(t => t.talking && (!sel.size || sel.has(t.gssi)));
  el.textContent = now.length
    ? "Now: " + now.map(t => t.gssi + (t.talker ? " (radio " + t.talker + ")" : "")).join(", ")
    : act ? "Live. Nobody speaks now." : "Live, but no run is active, so no speech comes in.";
}

function selection(changed){
  const list = [...sel].sort((a, b) => a - b);
  $("livesel").textContent = !list.length
      ? "Live plays all talkgroups. Tick talkgroups in the table to limit it."
    : list.length <= 4 ? "Live plays only " + list.join(", ") + "."
    : "Live plays only " + list.length + " selected talkgroups.";
  $("liveall").hidden = !list.length;
  if (!changed) return;
  try { localStorage.setItem(KEY, JSON.stringify(list)); } catch(e){}
  who();
  // One new stream after the last tick, not one for each tick.
  clearTimeout(again);
  if (liveOn) again = setTimeout(connect, 400);
}

$("livebtn").onclick = () => {
  liveOn = !liveOn; lost = false;
  $("livebtn").setAttribute("aria-pressed", liveOn);
  $("livelbl").textContent = liveOn ? "Stop live" : "Listen live";
  if (liveOn){
    // The browser permits sound only after a click, so the context starts here.
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = ctx.createAnalyser(); analyser.fftSize = 256;
    level = new Uint8Array(analyser.fftSize);
    analyser.connect(ctx.destination);
    connect(); meter();
  } else {
    if (conn) conn.abort();
    conn = null;
    // A closed context drops the sound that is in its queue.
    ctx.close(); ctx = null;
  }
  who();
};
$("liveall").onclick = () => {
  sel.clear();
  for (const box of document.querySelectorAll("#tg input[data-g]")) box.checked = false;
  selection(true);
};
$("tg").addEventListener("change", e => {
  const g = e.target.dataset.g;
  if (!g) return;
  if (e.target.checked) sel.add(g); else sel.delete(g);
  selection(true);
});
selection(false);

async function load(){
  let d; try { d = await (await fetch("api/overview")).json(); }
  catch(e){ document.getElementById("sub").textContent = "cannot reach the server"; return; }
  if (d.error){ document.getElementById("sub").textContent = d.error; return; }
  D = d;
  const pill = $("pill");
  pill.hidden = false; pill.textContent = d.active ? "recording" : "stopped";
  pill.className = "pill" + (d.active ? " on" : "");
  const h = d.health || {};
  document.getElementById("sub").textContent =
    "run " + d.run + "   started " + (d.start_utc||"?") + "   " +
    (d.iq_rate? (d.iq_rate/1e6).toFixed(1)+" MS/s":"") + "   updated " + d.generated;
  const drop = h.dropped|0;
  const peak = h.peak_queue|0;
  const cells = [
    ["recording for", dur(h.seconds||0), "",
     "How long this run has been going, counted from the samples the dongle "+
     "delivered, not from the wall clock."],
    // Dropped is a running total and means IQ was lost. Queue is the warning
    // that comes first: the peak is of the whole run, the other of this second.
    ["dropped", drop, drop? "bad":"ok",
     "Blocks of radio the dongle produced that this program could not take in "+
     "time. Every one is lost audio that no later fix recovers. It is a running "+
     "total for the run and it must stay at zero."],
    ["queue now/peak", (h.queue|0)+"/"+peak, peak>40? "bad" : peak? "warn":"ok",
     "Blocks waiting to be processed: this second, and the highest this run has "+
     "reached. The queue fills before anything is dropped, so a rising peak is "+
     "the warning that arrives first. It holds one second by default, 400 "+
     "blocks, which --queue-blocks sets."],
  ];
  if (h.temp_c != null)
    cells.push(["cpu temp", h.temp_c+"\u00b0C", h.temp_c>=80? "bad" : h.temp_c>=70? "warn":"ok",
     "A host that gets hot slows itself down, and a slowed host starts dropping. "+
     "A Raspberry Pi 5 begins to throttle near 80 \u00b0C."]);
  if (h.steps) cells.push(["clock steps", h.steps, "warn",
     "Times the wall clock jumped while recording, which is usually NTP "+
     "correcting it. The map from a position in a recording to UTC re-anchors "+
     "at each one, and clock.log marks that row STEP."]);
  cells.push(
    ["talkgroups", d.talkgroups.length, "",
     "Distinct talkgroups (GSSI) with recorded speech in this run. Each one has "+
     "its own WAV file."],
    ["carriers", d.carriers.length, "",
     "Carriers that have decoded at least one voice frame in this run."],
    ["learned", d.learned.length, "",
     "Carriers discovered: a control carrier granted a call on them, "+
     "and a free slot of the pool tuned to them and stayed."],
    ["not recorded", d.missed_total, d.missed_total? "warn":"ok",
     "Clear calls this run could not take, because no slot held that carrier, or "+
     "the carrier lies outside the span. This is a coverage problem, not a "+
     "performance one: see the list at the foot of the page for the reason of "+
     "each. Dropped is the number to watch for performance."]);
  document.getElementById("strip").innerHTML = cells.map(c =>
    '<div class="cell" title="'+esc(c[3])+'">'+INFO+'<span>'+c[0]+'</span><b class="'+c[2]+
    '">'+c[1]+"</b></div>").join("");

  band(d);
  head("h-band", "Band covered",
    "What the dongle receives, drawn across frequency. The lighter block inside it is clear of the "+
    "roll-off, meaning inside 80% of Nyquist, where a carrier keeps its full "+
    "sensitivity; between that and the edge a carrier still decodes but loses "+
    "SNR. The dashed line is the centre. Blue marks a control carrier, green a "+
    "traffic carrier with its height by frames decoded, red a frequency a grant "+
    "named that no slot recorded. Hover any region or mark for its own detail.");
  head("h-tg", "Talkgroups",
    "One row for each talkgroup (GSSI) with recorded speech, and its own WAV. "+
    "The mark before a GSSI is green while that talkgroup speaks. Tick the "+
    "box of a row to limit live mode to the rows with a tick. "+
    "Carriers lists every carrier it has been heard on, which is more than one "+
    "when the network moves a call. Radios lists the ISSIs that transmitted.");
  head("h-car", "Carriers",
    "Every frequency this run has seen, whether or not a slot could reach it. "+
    "Given means the command line named it; learned means a grant revealed it "+
    "and a free slot tuned to it. A row with in span \u201cno\u201d is a real "+
    "carrier this dongle cannot reach at the current centre.");
  head("h-miss", "Calls not recorded",
    "Clear calls that went out on air while no slot was on their carrier. This "+
    "is a coverage problem, not a performance one: nothing was dropped, the "+
    "program was simply not listening there. Each row says why.");

  tbl("tg", [
    {h:"live", once:true, f:r=>'<label class="pick" title="Play this talkgroup in live mode">'
      + '<input type="checkbox" data-g="'+esc(r.gssi)+'"'+(sel.has(r.gssi)?" checked":"")
      + ' aria-label="Live mode: talkgroup '+esc(r.gssi)+'"></label>'},
    {h:"GSSI", once:true, f:r=>'<span class="tk"></span>'+esc(r.gssi)},
    {h:"audio", once:true, f:r=>r.has_audio?
      '<audio controls preload="none" src="audio/'+esc(r.gssi)+'.wav"></audio>':"—"},
    {h:"speech", cls:"num", f:r=>dur(r.seconds)},
    {h:"frames", cls:"num", f:r=>r.frames},
    {h:"carriers", f:r=>r.carriers.map(c=>mhz(c.hz)).join(" ")},
    {h:"radios (ISSI)", f:r=>r.issis.map(i=>esc(i.issi)).join(" ")||"—"},
    {h:"first heard", f:r=>esc(hhmm(r.first_utc))},
    {h:"last heard", f:r=>esc(hhmm(r.last_utc))},
  ], d.talkgroups, "nothing recorded yet", r=>r.gssi);
  const talking = new Set(d.talkgroups.filter(t=>t.talking).map(t=>t.gssi));
  for (const tr of $("tg").tBodies[0].rows) tr.classList.toggle("talking", talking.has(tr.dataset.k));
  who();

  const learned = new Set(d.learned), pool = new Set(d.pool), ctl = new Set(d.controls);
  const heard = r => ["carrier","control"].includes(r.role);
  tbl("car", [
    {h:"frequency", f:r=>mhz(r.hz)+" MHz"},
    {h:"role", f:r=>ctl.has(r.hz)? '<span style="color:var(--ctl)">control</span>'
                  : heard(r)? "traffic" : "&mdash;"},
    {h:"source", f:r=>learned.has(r.hz)? "learned from a grant"
                    : (pool.has(r.hz)||heard(r))? "given"
                    : '<span class="bad">named by a grant, not followed</span>'},
    {h:"frames", cls:"num", f:r=>r.frames},
    {h:"in span", f:r=>r.in_span? '<span class="ok">yes</span>':'<span class="bad">no</span>'},
  ],
  // Everything the run has seen a frequency for, whether or not a slot could
  // reach it. BADFREQ is left out: a decode error is not a carrier.
  d.marks.filter(m=>m.role !== "BADFREQ"),
     "no carrier has decoded yet", r=>r.hz);

  document.getElementById("misscount").textContent =
    d.missed_total ? d.missed_total + (d.missed_total>d.missed.length? " (last "+d.missed.length+" shown)":"")
                   : "none";
  tbl("miss", [
    {h:"time", f:r=>esc(hhmm(r.utc))},
    {h:"GSSI", f:r=>esc(r.gssi)},
    {h:"radio", f:r=>esc(r.issi)},
    {h:"carrier", f:r=>mhz(r.hz)+" MHz"},
    {h:"granted by", f:r=>mhz(r.control_hz)+" MHz"},
    {h:"why", f:r=>({OUTSIDE:"outside the span — move --center or raise --rate",
                     BADFREQ:"the grant decoded wrong",
                     NOSLOT:"the carrier pool is full — raise --max-carriers",
                     UNTUNED:"no slot held it yet; one has taken it now"}[r.status]||r.status)},
  ], d.missed.slice().reverse(), "every granted call was recorded",
     r=>r.sample+"/"+r.hz+"/"+r.gssi);
}
load(); setInterval(load, 2000);
</script>
"""


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--in", dest="recordings", default="recordings")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args()
    Handler.recordings = a.recordings
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    everywhere = a.bind in ("", "0.0.0.0")
    print(
        "tetra-analyze ui: reading %s%s"
        % (a.recordings, ", reachable on every network" if everywhere else ""),
        flush=True,
    )
    for addr in (local_addresses() if everywhere else [a.bind]) or [a.bind or "0.0.0.0"]:
        print("  http://%s:%d" % (addr, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
