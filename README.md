# XMEye/icsee/VMS cloud camera → local stream bridge

Logs into a Xiongmai/XMEye-family "General protocol" cloud camera (the same
CloudID + username + password you'd enter in VMS's Device Manager) directly
through `NetSdk.dll` — the same DLL VMS.exe itself uses — and re-publishes
the live H.264/H.265 stream locally as MPEG-TS over HTTP, so VLC (or
anything else) can open it.

## Setup

1. **Windows, 64-bit Python 3.8+.** `NetSdk.dll` is PE32+/x64; a 32-bit
   Python will fail to load it.
2. **Folder layout matters.** Keep `xmeye_cloud_to_rtsp.py`, `run_all.py`, `NetSdk.dll`,
   `StreamReader.dll`, `H264Play.dll`, and `ffmpeg.exe` all in the same
   folder — `NetSdk.dll` needs its sibling DLLs alongside it to load, and
   the script looks for `ffmpeg.exe` next to itself.
3. **Use the older DLL pair, not the one VMS shipped with.** See
   "Findings" below for why — the bundled 2024-era `NetSdk.dll` has a
   broken cloud-login handshake. Use the 2018 build instead (from the
   open-source `hifided/IPCAS` project) — that's the one that actually
   works.
4. **Use the ffmpeg.exe that actually ships in the VMS folder.** Don't
   substitute one from elsewhere (e.g. a video editor like CapCut) — those
   often bundle a minimal ffmpeg build stripped of network-protocol
   support, which will parse the camera's stream fine but can never open a
   listening socket for VLC to connect to. See Findings below.
5. **Credentials go in `_credentials.py`** next to the script (not
   committed/shared anywhere). Two supported shapes:

   Single camera (unchanged from before):
   ```python
   CLOUD_ID     = "your-cloud-id"
   DEVICE_USER  = "admin"
   DEVICE_PASS  = "your-device-password"
   ```

   Multiple cameras (needed for `--camera` / `run_all.py`):
   ```python
   CAMERAS = {
       "front": {"cloud_id": "...", "user": "admin", "password": "..."},
       "back":  {"cloud_id": "...", "user": "admin", "password": "..."},
   }
   ```
   Both can be present at once — the flat vars are just the fallback when
   `--camera` isn't passed.
6. VMS.exe does **not** need to be closed — it can run concurrently with
   this script. (Earlier testing turned up a login failure when both were
   running, but that turned out not to require closing VMS.exe as a fix —
   see Findings.)

## Usage

```
python xmeye_cloud_to_rtsp.py                                 # single-camera setup, main video
python xmeye_cloud_to_rtsp.py --stream 1                      # sub-stream (smaller/lighter)
python xmeye_cloud_to_rtsp.py --cloud-id xxx --user xxx --password xxx
```

While the live command is running, once you see `Open this in VLC now:
http://127.0.0.1:8090/live.ts`, open that URL in VLC. Give it a few seconds
after that line — ffmpeg needs a real analysis window to lock onto stream
parameters before it starts serving.

## Multiple cameras / main+sub at once

Each camera+stream combination runs as its own OS process on its own port —
that's deliberate (see Findings: this SDK build's thread-safety for
concurrent sessions is unverified, and separate processes sidestep that
entirely).

**One at a time**, with an explicit port so instances don't collide:
```
python xmeye_cloud_to_rtsp.py --camera front --stream 0 --port 8090
python xmeye_cloud_to_rtsp.py --camera front --stream 1 --port 8091
python xmeye_cloud_to_rtsp.py --camera back  --stream 0 --port 8092
```

**All at once**, using every camera in `CAMERAS`:
```
python run_all.py                        # every camera, main + sub, ports from 8090 up
python run_all.py --streams 0            # every camera, main only
python run_all.py --cameras front back   # just these
```
It prints the VLC URL for every stream it starts, and Ctrl+C stops all of
them together.

**Two sessions to the *same* CloudID at once (main+sub of one camera) is
the one combination worth watching closely.** We already saw this exact
P2P/cloud setup get confused by two simultaneous sessions to the same
device (that's why VMS.exe had to be closed during earlier testing — see
Findings). Running this script's own main+sub instances against the same
camera concurrently might hit the same thing. It's plausibly fine — VMS.exe
itself supports viewing main+sub together — but if one of the two instances
sits retrying its login for a while before succeeding, that's the same
transient P2P registration hiccup, not a new bug; the 20-retry/6s-spacing
patience already in `login()` is there specifically to ride that out.

## One shared port + on-demand start/stop (MediaMTX)

The `run_all.py` setup above gives every stream its own port, and every
stream runs continuously whether anyone's watching or not. Two things
that setup *can't* do on its own, because `ffmpeg -listen` only ever
serves one stream on one port:

- **All streams reachable through a single port**, distinguished by path
  instead (`rtsp://host:8554/front_main`, `.../front_sub`,
  `.../back_main`, ...)
- **Only pull from the camera while someone's actually watching** — no
  point holding a P2P session and burning bandwidth to an empty room.

Both need a real RTSP server in front of this script, not just ffmpeg.
[MediaMTX](https://github.com/bluenviron/mediamtx) (small single-binary,
free, Windows/Linux/macOS) does this well and has on-demand support built
in — `runOnDemand` starts a command the moment a viewer connects to a
path, and `runOnDemandCloseAfter` stops it once the last viewer leaves.

Setup:
1. Download `mediamtx.exe` (and its default `mediamtx.yml`, which we'll
   overwrite) into this same script folder from the
   [MediaMTX releases page](https://github.com/bluenviron/mediamtx/releases).
2. Generate the config from your `_credentials.py` cameras:
   ```
   python gen_mediamtx_config.py
   ```
   This writes `mediamtx.yml` with one on-demand path per camera+stream —
   re-run it any time you edit `CAMERAS`, rather than hand-editing the
   generated file.
3. Run MediaMTX itself:
   ```
   mediamtx.exe mediamtx.yml
   ```
   It listens on port 8554 but doesn't start any camera sessions yet —
   nothing is connected, so `xmeye_cloud_to_rtsp.py` hasn't even launched.
4. Open a path in VLC, e.g. `rtsp://127.0.0.1:8554/front_main`. **That
   connection attempt is what triggers MediaMTX to launch this script**
   (with `--protocol push`, connecting *out* to MediaMTX instead of
   listening itself) — expect the same ~15-30s login/analysis delay as
   any other first connection. ~15 seconds after you disconnect (per
   `runOnDemandCloseAfter` in the generated config), MediaMTX stops that
   camera session automatically.

Under the hood, `--protocol push` is what makes this work: instead of this
script's own ffmpeg listening for a client, it connects out to whatever
`--push-url` says (MediaMTX substitutes its own port and the requested
path via `$RTSP_PORT`/`$MTX_PATH` environment variables, which the
generated config already wires up). You can also run `--protocol push`
manually with any RTSP server, MediaMTX or otherwise, by pointing
`--push-url` at it directly.

Graceful shutdown when MediaMTX stops a session: the script also handles
`SIGTERM` (not just Ctrl+C) so the camera gets logged out cleanly rather
than left in a half-open state. This is best-effort on Windows, same as
any process — a forceful kill can still bypass it, same as it would for
any process, but the SDK/relay session will simply time out on its own if
that happens.

## Findings (why the script looks the way it does)

- **Bundled `NetSdk.dll` (2024 build) can't complete cloud login.**
  `H264_DVR_Login_Cloud` reliably fails with error `-12003`. Packet capture
  showed this is *not* a real connectivity problem — real ~1420-byte data
  packets flow over the P2P/relay tunnel within 0.6s of the login attempt
  and keep flowing for the whole session — the DLL's own success bookkeeping
  just never recognizes it. Swapping in an older (2018) `NetSdk.dll` +
  `StreamReader.dll` pair sidesteps the bug entirely; login and `RealPlay`
  both succeed with that pair.
- **The stream is H.265/HEVC, not H.264.** Detected dynamically per-session
  from `frame.nEncodeType` — main stream is 3696×1080, sub-stream is
  1280×360, both in the ~12–25fps range depending on how you measure it.
  Don't hardcode H.264 assumptions anywhere.
- **The real bug behind "connects fine, but video is blank":** the SDK's
  `SetRealDataCallBack_V2` buffer includes small SDK-internal
  packet-length marker segments prefixed before real NAL data. These are
  identifiable because their `forbidden_zero_bit` (top bit of the NAL
  header byte) is `1`, which is never valid in real HEVC/H.264 — confirmed
  by hex dump that the marker's trailing 4 bytes, read little-endian, equal
  the very next real NAL's length + 4. `strip_private_markers()` drops any
  NAL segment with an invalid `forbidden_zero_bit` before forwarding to
  ffmpeg. Without this, ffmpeg logs constant "Invalid NAL unit" warnings
  and the decoder never gets clean access to real slice data.
- **A wrong `ffmpeg.exe` copy broke network serving entirely.** At one
  point the `ffmpeg.exe` in the script folder had been copied from CapCut
  (a video editor) rather than from the VMS folder — CapCut bundles a
  minimal ffmpeg build for its own internal transcoding, stripped of
  network-protocol support. That's why `ffmpeg -version` produced no
  output at all and it could never open an RTSP or HTTP listening socket
  (input parsing via `pipe:0` worked fine; only server/listen
  functionality was affected). **The `ffmpeg.exe` that actually ships with
  VMS is correct and works fine** — just make sure that's the one sitting
  next to this script, not a copy from somewhere else.
- **Output is MPEG-TS over HTTP, not RTSP.** RTSP listen-mode
  (`-rtsp_flags listen`) never opened its socket even with the replacement
  ffmpeg and otherwise-correct stream parsing — MPEG-TS/HTTP is a much
  older, more universally supported combination and just works.
- **Camera delivery vs. ffmpeg's consumption rate are decoupled** via a
  bounded queue and dedicated writer thread. The SDK calls back into Python
  from its own internal thread; if ffmpeg's stdin pipe ever backs up
  (e.g. mid-analysis), a direct blocking write would freeze frame delivery
  from the camera entirely with no visible error. The queue drops frames
  (with a warning) instead of blocking.

## Known limitation

**No audio.** `AUDIO_PACKET` frames are currently filtered out entirely —
even if they weren't, mixing raw audio bytes into the same `-f hevc pipe:0`
video-only input would break parsing the same way the marker bug did.
Adding audio needs: confirming the device has a mic enabled (check
`H264_DVR_DEVICEINFO.iAudioInChannel` at login), figuring out the actual
audio codec the SDK delivers (likely G.711 or ADPCM), and giving ffmpeg two
separate inputs (video pipe + audio pipe) to mux together, rather than
sharing the one raw pipe.
