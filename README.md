# sdr-tetra-analyzer

- [Prerequisites](#prerequisites)
  - [SDR receiver](#sdr-receiver)
  - [Host](#host)
  - [Headroom](#headroom)
  - [Packages](#packages)
  - [Active TETRA network in range](#active-tetra-network-in-range)
- [Installation](#installation)
  - [Third party dependencies](#third-party-dependencies)
- [Usage](#usage)
  - [Finding carriers](#finding-carriers)
  - [The carrier pool](#the-carrier-pool)
  - [Core options](#core-options)
  - [Output](#output)
  - [UI](#ui)
  - [Run it as a service](#run-it-as-a-service)
- [Architecture](#architecture)
  - [Parent — the radio consumer](#parent--the-radio-consumer)
  - [Carrier child, one for each slot — demodulation](#carrier-child-one-for-each-slot--demodulation)
  - [The carrier pool — why a sweep only needs the control carriers](#the-carrier-pool--why-a-sweep-only-needs-the-control-carriers)
  - [The shared allocation table](#the-shared-allocation-table)
  - [Stitch — the demultiplexer](#stitch--the-demultiplexer)
  - [Talkgroup worker, one for each talkgroup — speech and files](#talkgroup-worker-one-for-each-talkgroup--speech-and-files)
  - [Why processes and not threads](#why-processes-and-not-threads)
  - [Layout](#layout)
  - [Tests](#tests)
- [To be optimized](#to-be-optimized)
- [Known issues](#known-issues)

## Prerequisites

### SDR receiver 
The analyzer was developed against an RTL-SDR Blog V4 with an R828D tuner.
It opens the receiver through SoapySDR. `sweep` and `run` call
`enumerate()`: a stick is visible only after that stick's Soapy plugin is on
disk. `./build.sh` detects the OS, lists the hardware modules the package
manager ships, and installs them. A factory the OS does not package is not
built here. When USB shows a known stick that Soapy does not list, the
program names the stick and says what to install or check.

These receivers were tested on the Raspberry Pi 5 in September 2026:

| Receiver | Result | Notes |
| --- | --- | --- |
| RTL-SDR Blog V4 | works, the reference | 3.2 MS/s, 17 spans for the whole band |
| LimeSDR-USB, USB 3 | works | 61.44 MS/s, one span for the whole band. See the LimeSDR notes below |
| USRP B210, USB 3 | works, but a wide span is not real time | Needs the UHD images and `--device-args num_recv_frames=1024`. At 61.44 MS/s with 46 slots, `run` kept up with 76% of the signal |
| ADALM-Pluto, USB 2 | works, with a known crash | The link carries about 7 MS/s, so the sweep takes 6 MS/s. See [Known issues](#known-issues) |
| bladeRF x115, USB 3 | does not stream | SoapyBladeRF 0.4.2 overflows. See the bladeRF notes below |

For a receiver with no module, the IQ feed can be piped in instead:

```
<your sdr tool writing IQ to stdout> \
  | ./tetra-analyze run --iq - --carriers ...
```

Give `--fmt`, `--rate` and `--center` to match what the tool produces.

### Host 
Any POSIX system with a C++17 compiler and a package manager `./build.sh`
can drive. Windows is not supported, because the program is a process tree
built on `fork`, pipes, POSIX file locks and shared memory.

On Debian that is `apt` (the script uses `sudo`). On macOS it is Homebrew
and the Xcode command line tools. Those two are the only packages you
install yourself.

| Platform | State |
| --- | --- |
| Debian 13 (trixie) aarch64, Raspberry Pi 5 | verified, the reference target |
| macOS arm64 (Darwin 25) | verified before FFTW and the filter bank came in; not tested since |
| Other Linux, x86-64 or ARM | expected to work, not tested |
| FreeBSD and the other BSDs | expected to work, not tested |
| Windows | not supported |

### Headroom 
A Raspberry Pi 5 runs 15 carriers at 3.2 MS/s in about one of
its four cores, at 92 MB for the whole tree. With a LimeSDR-USB at 61.44 MS/s
and 30 carriers, the tree took about 2.9 of the four cores, 2.2 of them in
the parent, and `clock.log` stayed clean.

### Packages

`./build.sh` installs the compiler tools, cmake, volk, SoapySDR, FFTW, and every
hardware Soapy module this OS lists (`soapysdr-module-*` on apt, `soapy*`
plus `limesuite` on Homebrew). It skips the kitchen-sink `-all` package and
the remote/audio/osmosdr wrappers, which conflict or are not a stick. On
Debian it installs the host packages without Recommends, because
`libsoapysdr0.8` recommends that `-all` package. It adds `ca-certificates`
for HTTPS and `soapysdr-tools` for `SoapySDRUtil`.

`SKIP_DEPS=1 ./build.sh` leaves the host packages as they are.
`SOAPY_SKIP_MODULES=1 ./build.sh` leaves the device plugins as they are.

What the two reference hosts typically ship:

| Receiver | Debian | Homebrew |
| --- | --- | --- |
| RTL-SDR | `soapysdr-module-rtlsdr` | `soapyrtlsdr` |
| HackRF | `soapysdr-module-hackrf` | `soapyhackrf` |
| Airspy | `soapysdr-module-airspy` | not in brew-core |
| bladeRF | `soapysdr-module-bladerf` | not in brew-core |
| LimeSDR | `soapysdr-module-lms7` | `limesuite` |
| USRP | `soapysdr-module-uhd`, and `uhd-host` for the images | `uhd`; SoapyUHD is not in brew-core |
| Pluto | not in Debian 13 (sid has `soapysdr-module-plutosdr`); build SoapyPlutoSDR | not in brew-core |
| SDRplay | not packaged: the SDRplay API, then SoapySDRPlay3 | not in brew-core |

A USRP also needs the firmware and FPGA images of UHD. On Debian 13 the
downloader puts them where libuhd does not look, so name the directory:

```
sudo uhd_images_downloader -t b2xx -i /usr/share/uhd/images
```

At a wide rate a USRP also needs a larger receive buffer. UHD's default
buffer overflows at 61.44 MS/s while the program works on a block. Give
`--device-args num_recv_frames=1024` to `sweep`, and the run lines it prints
carry it. Linux limits USB buffers to 16 MB (`usbfs_memory_mb`), so 1024
frames is the largest that opens.

A bladeRF needs three things on Debian 13. Its udev rules grant access only
to a local desktop session (`uaccess`), so SSH logins and services need a
rule of their own, for example `GROUP="plugdev"`. A bladeRF 1 needs its FPGA
image (`bladerf-fpga-hostedx40` or `bladerf-fpga-hostedx115`), and that image
needs firmware 2.4.0 or later (`bladerf-firmware-fx3`, then `bladeRF-cli -f`).
Firmware 2.x changes its USB ID from `1d50:6066` to `2cf0:5246`. With all of
that, a bladeRF x115 still overflowed on almost every buffer through
SoapyBladeRF 0.4.2, while `bladeRF-cli` streamed without a fault.

When USB shows a known receiver that Soapy does not list, the program says
whether its Soapy module is missing or loaded, and what to do next.

A LimeSDR has more than one RX input, and each has its own connector. The
program uses LNAW (RX1_W on a LimeSDR-USB) unless `--antenna` names another.
A LimeSDR-USB with its antenna on RX1_L needs `--antenna LNAL`. An input with
no antenna shows only the DC spike at the centre of the span. A LimeSDR has no
automatic gain, so "auto" keeps the gain of the driver, 32 dB.

### Active TETRA network in range
`sweep` runs a scan for active control carriers across the TETRA spectrum, and the traffic carriers are then learned from the grants that the
control carriers broadcast. See [Finding carriers](#finding-carriers) below.

## Installation

```
git clone --recurse-submodules git@github.com:martinezpl/sdr-tetra-analyzer.git
cd sdr-tetra-analyzer
./build.sh
./tetra-analyze help
```

`build.sh` installs the host packages and the Soapy device modules this OS
ships, checks out the submodules, fetches the speech codec, and builds
`./tetra-analyze` at the top of the repository.

### Third party dependencies

- **The ETSI ACELP speech codec.** ETSI source cannot be redistributed, 
  so `build.sh` downloads it from ETSI and applies the patches that make it build on a 64-bit host. 
  This needs a network connection one time.
- **The SDR++ core headers**, as the submodule `third_party/sdrpp`, pinned to
  one commit. Only the headers are used. Nothing of SDR++ is compiled, linked
  or installed. To build against a copy that you already have, pass
  `./build.sh -DSDRPP_CORE_ROOT=<sdrpp>/core`, and the submodule is then not
  fetched at all.

## Usage

```
./tetra-analyze help    # every option, with its default
./tetra-analyze sweep   # find the control channels and active carriers
./tetra-analyze run --carriers <control carriers>   # record
```

`run` exposes every setting as a flag. Ctrl-C, SIGTERM, or the end of the
input stops a run and closes the files cleanly. A receiver that gives no
samples for 5 s stops `sweep` and `run` with `the receiver stopped`, so an
unplugged stick ends the run and does not hang it. Logs go to stdout. A live
run prints `radio rtlsdr 0`, where `rtlsdr` is the driver name in Soapy's
device list (`lime`, `uhd`, `plutosdr`, ...).

```
nohup ./tetra-analyze run --carriers 419162500,419562500 \
  > tetra-analyze.log 2>&1 < /dev/null &
```

### Finding carriers

`sweep` measures every channel of the raster, then puts a demodulator on
each peak. It prints a ready `run` command line. The messages of the Soapy
driver are left out here:

```
$ ./tetra-analyze sweep
Found Rafael Micro R828D tuner
RTL-SDR Blog V4 Detected
tetra-analyze: the receiver takes 3.200 MS/s, so a span is 3.170 MHz
tetra-analyze: sweep 380.000-430.000 MHz, 12.5 kHz raster, 3.2 MS/s, 17 span(s), rx 0 RX
tetra-analyze: span 1 at 381.585 MHz, 254 channels
tetra-analyze: span 2 at 384.596 MHz, 241 channels
[...]
tetra-analyze: 106 peak(s) at or above 6.0 dB over the noise floor
tetra-analyze: 14 span(s) to decode
tetra-analyze: calibrate on 14 candidate(s) at 420.680 MHz for 3 s
tetra-analyze: the receiver is off by about +0.08 ppm, and each dwell corrects that
tetra-analyze: span 1 of 14, decode 4 candidate(s) at 382.230 MHz for 15 s
[...]
tetra-analyze: span 14 of 14, decode 7 candidate(s) at 429.040 MHz for 15 s

  channel       SNR dB  role                     cell main carrier  error Hz
  390012500        8.1  TETRA traffic carrier    391037500          -497
  390512500       23.9  TETRA control carrier    390512500          -292
  390862500       23.1  TETRA traffic carrier    390512500          -257
  [...]

24 TETRA carrier(s), of which 8 are a control carrier.

The receiver is off by about -0.75 ppm, from 13 carrier(s) above the noise that
held lock, spread -3.95 to +0.96 ppm. Each run below carries the
offset in Hz at its own centre, because the error scales with it.
That spread is wide for one oscillator. A carrier of its own may
sit off frequency, or a weak one may not have settled. A longer
--dwell tightens it.

Those carriers do not fit one span at 3.2 MS/s, so they need 3 runs,
one for each span. A receiver hears one span at a time, so running them
at once needs one receiver for each, named with --device.

  # span 1 of 3, 6 carrier(s), 3 control, 3 listed

  ./tetra-analyze run --center 390890000 \
      --rate 3200000 \
      --tune-offset -292 \
      --carriers 390512500,391037500,391562500

  # span 2 of 3, 9 carrier(s), 3 control, 3 listed
[...]

3 carrier(s) sit too far from any control carrier to share a span with
one, so nothing would grant them: 393237500 393287500 419550000
They are traffic carriers of a cell whose control carrier the sweep did
not find. Widen --band or raise --dwell to look for it.

Each run line lists the control carriers, and any carrier whose cell the
sweep could not read. "run" gives a free slot to each traffic carrier when
a control carrier first grants a call on it, and it keeps what it learns for
the next start. An idle traffic carrier sends nothing, so the sweep does not
count it. If "run" logs NOSLOT, raise --max-carriers.
```

With no `--rate`, the sweep asks for the widest rate that holds the whole
band (or a narrower `--band`) in one span, up to what the receiver takes.
Then it measures the samples that arrive for one second, and it steps down
to a rate that the link carries if samples are missing. A Pluto on USB 2.0
lists 10 MS/s but delivers about 7, so the sweep takes 6 MS/s. The rate that
the sweep uses goes into the run lines.

Before the dwells, a short calibration dwell of 3 s on the candidates of one
span measures the frequency error of the receiver. Every dwell then moves
its filters by that error. A receiver far off frequency (a Pluto was 10 ppm
off, which is 4 kHz at 420 MHz) would otherwise cut the edge of each carrier,
and its system information would fail. A span with more candidates than
`--max-carriers` gets more than one dwell, strongest first
(`part 2 of 3`), so no peak goes undecoded.

With `--carriers` and no `--center`, the receiver tunes to the midpoint of
your list.

- **Every control carrier is mandatory.** Every channel grant is broadcast on
  one, and a grant is how the program learns that a call is starting, on which
  carrier, for which talkgroup, and whether it is in the clear.
- **Traffic carriers are optional.** A free slot of the pool tunes to each one
  the moment a grant names it, so the list fills itself. Naming a traffic
  carrier you already know is a convenience: a slot is on it before its first
  call starts, so the head of that call is not lost to the time a retune takes.

The run lines that `sweep` prints list only the control carriers, and any
carrier whose cell it could not read. The traffic carriers that it found still
set the centre of each span, so their grants land inside it, and a span with
more carriers than the pool default (15) gets `--max-carriers` for all of
them. So only the first call on each traffic carrier loses its head, because
`run` keeps the carriers it learns in `DIR/carriers`.

`sweep` tells the two apart: a carrier whose system information names itself
as the main carrier of its cell is a control carrier.

### The carrier pool

`--carriers` seeds a pool of `--max-carriers` slots, 15 by default. Each
spare slot waits. When a control carrier grants a clear call on a carrier
that no slot follows, the parent gives a free slot that frequency and keeps
it there. A slot is never taken back: a carrier the network used once it will
use again, and a retune costs the head of a call.

So a sweep only has to find the control carriers. The rest fills itself:

```
tetra-analyze: slot 5 takes 420362500 Hz, granted to GSSI 1002 by 419562500 Hz
```

The learned carriers go to `DIR/carriers` beside the recordings, and the next
run reads them back, so a restart at the UTC day boundary does not learn them
all over again. `--no-learn` turns that file off.

### Core options

| Flag | Default | What it does |
| --- | --- | --- |
| `--carriers LIST` | **required** | Downlink carriers in Hz, separated by commas. Must hold every control carrier |
| `--center HZ` | midpoint of `--carriers` | Dongle centre frequency. Give one only to leave room on one side for a carrier not yet known |
| `--rate HZ` | 3200000 | Dongle sample rate, and so the width of the span. |
| `--gain DB` | auto | Tuner gain, or `auto` |
| `--tune-offset HZ` | 0 | Correction for the frequency error of your receiver. `sweep` measures it |
| `--out DIR` | `recordings` | Directory for recordings |
| `--max-carriers N` | 15 | Size of the carrier pool. Spare slots learn carriers from grants |
| `--per-carrier` | off | Also write one WAV and one log for each carrier. Costs much more CPU, and it fixes the carrier list |
| `--iq FILE` | — | Replay a capture instead of opening the dongle |
| `--rx CHANNEL` | 0 | RX channel of the receiver |
| `--antenna NAME` | driver default, LNAW for a Lime | RX input of the receiver (LNAL, LNAH, LNAW, ...) |
| `--device-args ARGS` | none | Soapy device arguments, `KEY=VALUE,...`, for example `num_recv_frames=1024` for a USRP |

### Output

Each run makes one directory, `recordings/<start_utc>/`:

| File | Content |
| --- | --- |
| `calls/<GSSI>.wav` | The speech of one talkgroup. 8 kHz mono s16, silence removed |
| `calls.log` | One line for each decoded voice frame, and one for each call that could not be recorded |
| `timemap.log` | Anchors that tie a position in a WAV to a capture sample |
| `clock.log` | UTC against the sample counter, one line each second |

`clock.log` is the health record of a run. Its `dropped` column counts the
overflows that the driver of the receiver reported. It must stay at zero;
anything else means the host cannot keep up with the receiver. The `queue`
column is always 0: it belonged to a reader thread that the program no
longer has, and `--queue-blocks` is still accepted but does nothing.

Because silence is removed, a position in a WAV is not a wall-clock time on
its own. `src/timemap.py` converts between the two with the anchors of
that run:

```
src/timemap.py RUNDIR wav 1001 1048576      # byte offset -> UTC
src/timemap.py RUNDIR utc 1001 2026-09-12T15:20:00Z   # UTC -> byte offset
```

### UI

`src/ui.py` serves one page that shows what a run is doing: health, the
band it covers, every talkgroup it has recorded with its audio, the carriers
it follows, and every call it could not take.

```
$ python3 src/ui.py --in recordings --port 8080
tetra-analyze ui: reading recordings, reachable on every network
  http://10.42.0.1:8080
  http://192.168.1.201:8080
  http://127.0.0.1:8080
```

It prints every address it answers on. `--bind` narrows that to one, and
there is no authentication, so that choice is the only access control.

It reads only the files that a run writes, so it can be started, stopped or edited while a capture is going on,
and it never touches the recorder. Python 3 alone, no dependencies.


### Run it as a service

The program sends `READY=1` and `WATCHDOG=1` to `$NOTIFY_SOCKET`, so
`Type=notify` and `WatchdogSec` work in a systemd unit with no wrapper.

Give `TasksMax` room for the whole tree: `--max-gssi` talkgroup writers, 256
by default, plus one process for each carrier, plus the parent and the stitch
process. A `fork()` above `TasksMax` throws in the stitch process, which ends the run and logs
`tetra-analyze: stitch exited 134`.

## Architecture

```
dongle ────── IQ──▶ PARENT
                       │  N pipes
                       ▼
                  CARRIER CHILD ×N  ◀──▶ shared allocation table
                       │  one voice pipe, coded bits
                       ▼
                     STITCH
                       │  one pipe for each talkgroup
                       ▼
              TALKGROUP WORKER ×N ──▶ calls/<GSSI>.wav
```

### Parent — the radio consumer
It owns the receiver, and it reads it on the thread that opened it, because
SoapyRTLSDR crashed when another thread read it. An overflow that the driver
reports counts in the `dropped` column of `clock.log`. The main loop takes
each block as complex float (a raw `--iq` input is converted first) and runs
one channelizer for each carrier. A channelizer
shifts its carrier down to baseband and lowers the sample rate, so a child
works on one narrow stream instead of the whole span. A span wider than
4 MS/s, whose rate divides into whole sub-band rates, first goes through a
polyphase filter bank (`src/pfb.cpp`). The bank splits the span into
sub-bands at most 0.5 MHz apart, on up to four threads, and each
channelizer then works on the sub-band of its carrier, on the same threads.
Without the bank, each channelizer filters the full rate, and a full
61.44 MS/s span with 15 carriers would need about 4.6 cores. The result goes down
that carrier's pipe. The parent also writes `clock.log`
and answers the systemd watchdog. It is the only process that touches the
full-rate stream, so it costs far more than any other.

### Carrier child, one for each slot — demodulation
It reads its stream and runs π/4-DQPSK demodulation, symbol extraction, bit unpacking and
the TETRA burst decoder. It sends raw coded voice frames to the stitch
process, and it publishes and reads channel grants in the shared table. By
default it never decodes speech; `--per-carrier` is what turns that on.

A child reads its own slot in the shared table once for each block. When the
frequency there changes, it throws the whole decoder away and builds a new
one. That is deliberate: the decoder keeps state in the display state, the
crypto state and the fragment slots, and one stale field is enough to
suppress playback for the rest of the run, so a rebuild is the only reset
that cannot miss one.

The parent feeds every slot, free or not, so all children count the same
samples. Grants are stamped with that count, so a slot filled an hour into a
run still reads them on the same timebase.

### The carrier pool — why a sweep only needs the control carriers
A free slot holds no frequency. When a child on a control carrier decodes a grant
for a clear call on a carrier that no slot follows, it writes the frequency
into a small queue in the shared table. Once for each input block the parent
empties that queue, gives each frequency a free slot, and moves that slot's
channelizer onto it. A slot
is never taken back, so each carrier costs one lock time ever — about 0.2 to
1.7 s, which is the head of that first call.

The span is the hard limit. A grant for a carrier the receiver cannot reach
is recorded in `calls.log` as `OUTSIDE` and never enters the pool. A grant
naming a frequency 10 MHz or more from the carrier that sent it is a decode
error rather than a carrier, because a network keeps its carriers within a
few MHz of each other. It is recorded as `BADFREQ` and kept out too. The
test uses the sending carrier and not the centre of the span, because the
centre of a wide span can sit between two networks.

### The shared allocation table
A control carrier announces that a talkgroup has been granted a traffic
carrier, a slot and a clear channel. The child on that traffic carrier never
hears the announcement, and its own encryption field is ambiguous. So the
child on the control carrier writes the grant into shared memory, and the
child on the traffic carrier reads it back. That is how a clear call gets a
real talkgroup and subscriber identity instead of an anonymous one. The table
is anonymous shared memory, mapped before any fork, with POSIX file locks
around each access.

### Stitch — the demultiplexer
Every carrier writes voice frames into its one pipe. 
It writes the headers of `calls.log` and `timemap.log`, then keys
on the talkgroup: the first frame of a new talkgroup forks a worker and keeps
a pipe to it, and every later frame goes there. `--max-gssi` caps the number
of workers, because a corrupt talkgroup field on the air would otherwise fork
without limit.

### Talkgroup worker, one for each talkgroup — speech and files
It owns one WAV file and appends to the shared logs. It holds each frame about a second,
so late duplicates can arrive: the same talkgroup is often heard on several
carriers at once, and the worker keeps the carrier that gives the most frames
and drops the rest. Then it runs the ETSI speech decoder, which gives 480
samples for each frame. A gap longer than a second gets a separator and a
codec reset; a short gap gets concealment frames. Each of those moves the WAV
by something other than 480 samples, so each one writes an anchor to
`timemap.log`. Those anchors are what make the map from a WAV position to UTC
exact rather than an estimate.

### Why processes and not threads

The ETSI speech decoder holds its state in globals, so two calls cannot share
one copy of it. The decoder must be isolated per call to record multiple simultaneously.

Any child that dies raises `SIGCHLD`, the
parent stops, and the whole tree ends so a supervisor can start a fresh one.
A tree that stays half alive looks healthy while it records nothing, which is
the worst outcome for something left running unattended.

### Layout

- `src/` — the program: the parent, the filter bank, the carrier children,
  the sweep, the stitch process, the wall-clock map and the tests.
- `sdrpp-tetra-demodulator/` — a submodule that gives the TETRA channel
  decoder and the DSP blocks. It's my fork of
  [cropinghigh/sdrpp-tetra-demodulator](https://github.com/cropinghigh/sdrpp-tetra-demodulator).
- `third_party/sdrpp/` — a submodule that gives the
  [SDR++](https://github.com/AlexandreRouma/SDRPlusPlus) core headers.

### Tests

```
src/test/run.sh                       # synthetic IQ, plus the unit tests
src/test/run.sh BASEBAND.wav HZ1 HZ2  # checks against a real capture
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to ensure no regressions.

## To be optimized

**A narrow span still runs on one thread.** At 4 MS/s or less the parent
has no filter bank, and its channelizer loop runs on one thread. Each
carrier costs about 1.6% of one core there, against about 1.5% in its own
child, which the other cores absorb. At 13 carriers the parent takes about
41% of a core. A wide span already uses the filter bank and its threads:
on a Pi 5, 24 carriers at 61.44 MS/s take 2.9 s for 3.9 s of signal. The
channelizers of a narrow span could use the same threads.
`volk-config-info --machine` gives `neonv8_orc` on a Pi 5, so the NEON
kernels already carry the present load.

**The parent reads the radio between blocks, on one thread.** It reads a
block, then runs the bank and the channelizers, then reads the next one. A
driver with a large buffer of its own (LimeSuite) does not notice. UHD has
a small buffer and needs a reader that never stops: a USRP B210 at 61.44
MS/s with 46 slots kept up with only 76% of the signal, even with
`num_recv_frames=1024`. A reader thread would overlap the two. The program
had one and dropped it because SoapyRTLSDR crashed when a thread other than
the opener read it, so a new one must open, read and close on its own
thread.

**FFTW plans the filter bank for about 3.7 s at the start.** `FFTW_MEASURE`
tries many ways to do the FFT and keeps the fastest, which is 37% faster
than the plan it would guess. A run plans before it opens the radio, so no
sample is lost. Saving the FFTW wisdom to a file would make the next start
instant.

**A free pool slot costs as much as a busy one.** The parent runs the
channelizer for every slot, assigned or not, so that all children count the
same samples and a slot filled late still reads the grants on the same
timebase. A spare slot therefore burns about 1.6% of a core to produce
nothing. Putting the sample counter in the shared table instead would let the
parent skip a free slot completely.

**A retune loses the head of the first call.** A demodulator needs 0.2 to
1.7 s to lock, and a weak carrier has been seen to need 4 s and even 115 s.
The grant arrives with the voice, so those seconds come out of the call. A
ring of a few seconds of IQ for each slot, replayed into the child after the
retune, would recover most of it.

**calls.log line order is not deterministic.** One process for each talkgroup
appends to it, so two lines written at the same moment can land in either
order. Running the *unchanged* binary twice over the same capture reproduces
the same transposition, so this is inherent and not a regression. Every line
carries its own `capture_sample`, so sort before comparing. Letting the
stitch process write that file, instead of each worker, would fix it.

**Automatic gain makes SNR incomparable across spans.** The sweep takes the
noise floor of each span alone to work around it. A fixed `--gain` gives
numbers that can be compared directly, and the report does not yet say so.

**Signal handlers carry no `SA_RESTART`, on purpose.** They have to break the
blocking read so a dead child ends the run at once. The cost is that a signal
landing inside a line-buffered flush can cut that line, but only when stdout
is a socket, which means under systemd. A regular file never blocks, so a
redirect to a file is safe.

**Raw replay assumes a little-endian host.** The WAV writer and the WAV
header parser are explicit about byte order, but `--fmt cs16` and `--fmt
cf32` read raw input in host order. `cu8` and `cs8` are byte-wise and safe.

**The ETSI codec fetch script is fragile.** It has no `set -e`, it uses the
GNU-only `md5sum` and `stat -c%s`, and on a host with no `md5sum` its
integrity test silently reports success and skips the retry over HTTP. It
belongs to the submodule, not here, so `build.sh` checks for a patched file
afterwards instead of trusting the exit status.

## Known issues

**An ADALM-Pluto sweep crashed twice with heap corruption.** Two of about
nine sweeps with a Pluto ended with `corrupted size vs. prev_size` when the
sweep freed its buffers. AddressSanitizer and gdb did not find the cause, and
the other receivers never showed it.

**A bladeRF x115 does not stream through SoapyBladeRF 0.4.2.** See the
bladeRF notes under [Packages](#packages).
