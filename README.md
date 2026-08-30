# XMEye/icsee/VMS cloud camera → local stream bridge

Logs into a Xiongmai/XMEye-family "General protocol" cloud camera (the same
CloudID + username + password you'd enter in VMS's Device Manager) directly
through `NetSdk.dll` — the same DLL VMS.exe itself uses — and re-publishes
the live H.264/H.265 stream locally as MPEG-TS over HTTP, so VLC (or
anything else) can open it.

## Setup

1. **Windows, 64-bit Python 3.8+.** `NetSdk.dll` is PE32+/x64; a 32-bit
   Python will fail to load it.
2. **Folder layout matters.** Keep `xmeye_cloud_to_rtsp.py`, `NetSdk.dll`,
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
   committed/shared anywhere):
   ```python
   CLOUD_ID     = "your-cloud-id"
   DEVICE_USER  = "admin"
   DEVICE_PASS  = "your-device-password"
   ```

## Usage

```
python xmeye_cloud_to_rtsp.py                    # main stream
python xmeye_cloud_to_rtsp.py --stream 1         # sub-stream
python xmeye_cloud_to_rtsp.py --cloud-id xxx --user xxx --password xxx
```

While the live command is running, once you see `Open this in VLC now:
http://127.0.0.1:8090/live.ts`, open that URL in VLC. Give it a few seconds
after that line — ffmpeg needs a real analysis window to lock onto stream
parameters before it starts serving.

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
