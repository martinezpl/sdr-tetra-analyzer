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
  - [Terms](#terms)
  - [Parent — the radio consumer](#parent--the-radio-consumer)
  - [Carrier child, one for each lane — demodulation](#carrier-child-one-for-each-lane--demodulation)
  - [The carrier pool — why a sweep only needs the control carriers](#the-carrier-pool--why-a-sweep-only-needs-the-control-carriers)
  - [The shared table](#the-shared-table)
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
`enumerate()`: a receiver is visible only after its Soapy plugin is on
disk. `./build.sh` detects the OS, lists the hardware modules the package
manager ships, and installs them. A factory the OS does not package is not
built here. When USB shows a known stick that Soapy does not list, the
program names the stick and says what to install or check.

These receivers were tested on the Raspberry Pi 5 in September 2026:

| Receiver | Result | Notes |
| --- | --- | --- |
| RTL-SDR Blog V4 | works, the reference | 3.2 MS/s, 17 spans for the whole band |
| LimeSDR-USB, USB 3 | works | 61.44 MS/s, one span for the whole band. See the LimeSDR notes below |
| USRP B210, USB 3 | works, but a wide span is not real time | Needs the UHD images and `--device-args num_recv_frames=1024`. At 61.44 MS/s with 46 lanes, `run` kept up with 76% of the signal |
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
unplugged receiver ends the run and does not hang it. Logs go to stdout. A live
run prints `radio rtlsdr 0`, where `rtlsdr` is the driver name in Soapy's
device list (`lime`, `uhd`, `plutosdr`, ...).

```
nohup ./tetra-analyze run --carriers 419162500,419562500 \
  > tetra-analyze.log 2>&1 < /dev/null &
```

### Finding carriers

`sweep` measures every channel of the raster, then puts a demodulator on
each peak. It prints a ready `run` command line.

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
- **Traffic carriers are optional.** A free lane of the pool tunes to each one
  the moment a grant names it, so the list fills itself. Naming a traffic
  carrier you already know is a convenience: a lane is on it before its first
  call starts, so the head of that call is not lost to the time a retune takes.

The run lines that `sweep` prints list only the control carriers, and any
carrier whose cell it could not read. The traffic carriers that it found still
set the centre of each span, so their grants land inside it, and a span with
more carriers than the pool default (15) gets `--max-carriers` for all of
them. So only the first call on each traffic carrier in a run loses its head.

`sweep` tells the two apart: a carrier whose system information names itself
as the main carrier of its cell is a control carrier.

### The carrier pool

`--carriers` seeds a pool of `--max-carriers` lanes, 15 by default. Each
spare lane waits. When a control carrier grants a clear call on a carrier
that no lane follows, the parent gives a free lane that frequency and keeps
it there. A lane is never taken back: a carrier the network used once it will
use again, and a retune costs the head of a call.

So a sweep only has to find the control carriers. The rest fills itself:

```
tetra-analyze: lane 5 takes 420362500 Hz, granted to GSSI 1002 by 419562500 Hz
```

Each run starts with only the `--carriers` list in the pool, so it finds the
traffic carriers again. A carrier that the network no longer uses does not
keep a lane. The carriers that the lanes follow go to `carriers` in the run
directory, for the web overview. No run reads that file back.

### Core options

| Flag | Default | What it does |
| --- | --- | --- |
| `--carriers LIST` | **required** | Downlink carriers in Hz, separated by commas. Must hold every control carrier |
| `--center HZ` | midpoint of `--carriers` | Receiver centre frequency. Give one only to leave room on one side for a carrier not yet known |
| `--rate HZ` | 3200000 | Receiver sample rate, and so the width of the span. |
| `--gain DB` | auto | Tuner gain, or `auto` |
| `--tune-offset HZ` | 0 | Correction for the frequency error of your receiver. `sweep` measures it |
| `--out DIR` | `recordings` | Directory for recordings |
| `--max-carriers N` | 15 | Size of the carrier pool. Spare lanes learn carriers from grants |
| `--per-carrier` | off | Also write one WAV and one log for each carrier. Costs much more CPU, and it fixes the carrier list |
| `--iq FILE` | — | Replay a capture instead of opening the receiver |
| `--rx CHANNEL` | 0 | RX channel of the receiver |
| `--antenna NAME` | driver default, LNAW for a Lime | RX input of the receiver (LNAL, LNAH, LNAW, ...) |
| `--device-args ARGS` | none | Soapy device arguments, `KEY=VALUE,...`, for example `num_recv_frames=1024` for a USRP |

### Output

Each run makes one directory, `recordings/<start_utc>/`:

| File | Content |
| --- | --- |
| `calls/<GSSI>.wav` | The speech of one talkgroup. 8 kHz mono s16, silence removed |
| `calls.log` | One line for each decoded voice frame, and one for each call that could not be recorded |
| `timemap.log` | Anchors that tie a position in a WAV to a VFO sample |
| `clock.log` | UTC against the VFO sample counter, one line each second |
| `carriers` | The carriers that the lanes follow. `learned` marks one that a grant found |

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

`src/ui.py` serves one page that shows what the current/latest run is doing: health, the
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
receiver ──── IQ──▶ PARENT
                       │  N pipes
                       ▼
                  CARRIER CHILD ×N  ◀──▶ shared table
                       │  one voice pipe, coded bits
                       ▼
                     STITCH
                       │  one pipe for each talkgroup
                       ▼
              TALKGROUP WORKER ×N ──▶ calls/<GSSI>.wav
```

### Terms

One word for each thing. The last column gives the TETRA term where one
exists, so that a reader of the standard can map the two.

| Term | Meaning here | In TETRA |
| --- | --- | --- |
| carrier | One downlink RF carrier, named by its frequency in Hz | The same. A carrier carries four timeslots |
| control carrier | A carrier whose system information names itself as the main carrier of its cell. Every grant of that cell arrives on it | The main carrier. It carries the MCCH on timeslot 1 |
| traffic carrier | A carrier of a cell that is not its main carrier. It carries speech only while a call runs on it | Not a standard term |
| timeslot | One of the four slots of a TDMA frame on one carrier. `tn` in code and `TN` in the logs, 1 to 4 | The same |
| grant | A channel allocation for a clear call, which a control carrier broadcasts: a carrier, a timeslot and a talkgroup | The channel allocation element of a MAC-RESOURCE PDU. Not a TX grant, which lets one terminal speak |
| usage marker | The number that the network gives a call on a timeslot, so that the traffic can be tied to its grant | The same, 4 to 63 |
| talkgroup | The group that a call addresses, named by its GSSI. Each one gets one WAV file | A group short subscriber identity |
| terminal | A subscriber radio, named by its ISSI | A mobile station |
| voice frame | The 432 coded speech bits of one timeslot in one TDMA frame: 60 ms of speech, 480 WAV samples | The TCH/S payload of one timeslot |
| call | A run of voice frames of one talkgroup with no gap of 1 s or more. The unit of `calls.log` | A group call, from set-up to release |
| talkspurt | What one carrier sees of a call on one timeslot, from the first voice frame to a gap of 1 s or a new talker. The unit of the per-carrier logs | One transmission inside a call |
| span | The bandwidth that the receiver captures at once, which `--rate` sets | None. An SDR term |
| sub-band | One output of the polyphase filter bank, at most 0.5 MHz wide | None |
| VFO | The block that shifts one carrier to baseband, filters it and resamples it to 36 kS/s. The name comes from SDR++ | None. In DSP terms, a digital down converter |
| VFO sample | The sample counter that stamps every grant, voice frame and anchor. It counts the output of one VFO at 36 kS/s, and every lane counts the same | None |
| lane | One entry of the pool: one VFO in the parent, one pipe and one carrier child. A lane follows one carrier, or it is free | None |
| pool | The `--max-carriers` lanes of a run. `--carriers` seeds it, and grants fill it | None |
| shared table | The shared memory that holds the pool and the grants, with a file lock around each access | None |
| carrier child | The process that demodulates and decodes one lane | None |
| stitch | The process that sorts voice frames by talkgroup and forks the talkgroup workers | None |
| talkgroup worker | The process that decodes the speech of one talkgroup and writes its WAV file | None |

### Parent — the radio consumer
It owns the receiver, and it reads it on the thread that opened it, because
SoapyRTLSDR crashed when another thread read it. An overflow that the driver
reports counts in the `dropped` column of `clock.log`. The main loop takes
each block as complex float (a raw `--iq` input is converted first) and runs
one VFO for each lane. A VFO shifts its carrier down to baseband and lowers
the sample rate to 36 kS/s, so a child
works on one narrow stream instead of the whole span. A span wider than
4 MS/s, whose rate divides into whole sub-band rates, first goes through a
polyphase filter bank (`src/pfb.cpp`). The bank splits the span into
sub-bands at most 0.5 MHz apart, on up to four threads, and each
VFO then works on the sub-band of its carrier, on the same threads.
Without the bank, each VFO filters the full rate, and a full
61.44 MS/s span with 15 carriers would need about 4.6 cores. The result goes down
that carrier's pipe. The parent also writes `clock.log`
and answers the systemd watchdog. It is the only process that touches the
full-rate stream, so it costs far more than any other.

### Carrier child, one for each lane — demodulation
It reads its stream and runs π/4-DQPSK demodulation, symbol extraction, bit unpacking and
the TETRA burst decoder. It sends raw coded voice frames to the stitch
process, and it publishes and reads channel grants in the shared table. By
default it never decodes speech; `--per-carrier` is what turns that on.

A child reads its own lane in the shared table once for each block. When the
frequency there changes, it throws the whole decoder away and builds a new
one. That is deliberate: the decoder keeps state in the display state, the
crypto state and the fragment buffers of the decoder, and one stale field
is enough to suppress playback for the rest of the run, so a rebuild is the
only reset that cannot miss one.

The parent feeds every lane, free or not, so all children count the same
samples. Grants are stamped with that count, so a lane filled an hour into a
run still reads them on the same timebase.

### The carrier pool — why a sweep only needs the control carriers
A free lane holds no frequency. When a child on a control carrier decodes a grant
for a clear call on a carrier that no lane follows, it writes the frequency
into a small queue in the shared table. Once for each input block the parent
empties that queue, gives each frequency a free lane, and moves the VFO of
that lane onto it. A lane
is never taken back, so each carrier costs one lock time ever — about 0.2 to
1.7 s, which is the head of that first call.

The span is the hard limit. A grant for a carrier the receiver cannot reach
is recorded in `calls.log` as `OUTSIDE` and never enters the pool. A grant
naming a frequency 10 MHz or more from the carrier that sent it is a decode
error rather than a carrier, because a network keeps its carriers within a
few MHz of each other. It is recorded as `BADFREQ` and kept out too. The
test uses the sending carrier and not the centre of the span, because the
centre of a wide span can sit between two networks.

### The shared table
A control carrier announces that a talkgroup has been granted a traffic
carrier, a timeslot and a clear channel. The child on that traffic carrier never
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
has no filter bank, and its VFO loop runs on one thread. Each
carrier costs about 1.6% of one core there, against about 1.5% in its own
child, which the other cores absorb. At 13 carriers the parent takes about
41% of a core. A wide span already uses the filter bank and its threads:
on a Pi 5, 24 carriers at 61.44 MS/s take 2.9 s for 3.9 s of signal. The
VFOs of a narrow span could use the same threads.
`volk-config-info --machine` gives `neonv8_orc` on a Pi 5, so the NEON
kernels already carry the present load.

**The parent reads the radio between blocks, on one thread.** It reads a
block, then runs the bank and the VFOs, then reads the next one. A
driver with a large buffer of its own (LimeSuite) does not notice. UHD has
a small buffer and needs a reader that never stops: a USRP B210 at 61.44
MS/s with 46 lanes kept up with only 76% of the signal, even with
`num_recv_frames=1024`. A reader thread would overlap the two. The program
had one and dropped it because SoapyRTLSDR crashed when a thread other than
the opener read it, so a new one must open, read and close on its own
thread.

**FFTW plans the filter bank for about 3.7 s at the start.** `FFTW_MEASURE`
tries many ways to do the FFT and keeps the fastest, which is 37% faster
than the plan it would guess. A run plans before it opens the radio, so no
sample is lost. Saving the FFTW wisdom to a file would make the next start
instant.

**A free pool lane costs as much as a busy one.** The parent runs the VFO
for every lane, assigned or not, so that all children count the same
samples and a lane filled late still reads the grants on the same timebase.
A spare lane therefore burns about 1.6% of a core to produce nothing.
Putting the VFO sample counter in the shared table instead would let the
parent skip a free lane completely.

**A retune loses the head of the first call.** A demodulator needs 0.2 to
1.7 s to lock, and a weak carrier has been seen to need 4 s and even 115 s.
The grant arrives with the voice, so those seconds come out of the call. A
ring of a few seconds of IQ for each lane, replayed into the child after the
retune, would recover most of it.

**calls.log line order is not deterministic.** One process for each talkgroup
appends to it, so two lines written at the same moment can land in either
order. Running the *unchanged* binary twice over the same capture reproduces
the same transposition, so this is inherent and not a regression. Every line
carries its own `vfo_sample`, so sort before comparing. Letting the
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
