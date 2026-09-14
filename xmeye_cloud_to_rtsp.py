"""
xmeye_cloud_to_rtsp.py
-----------------------
Logs into a cloud camera (CloudID + user + password, same as VMS's Device
Manager) via NetSdk.dll, and re-publishes its H264/H265 stream locally over
HTTP/MPEG-TS or RTSP for VLC. See README.md for setup, background, and
troubleshooting.

Supports multiple cameras and multiple streams (main/sub) of the same
camera running concurrently -- as separate OS processes, one per
camera+stream, each on its own port. Use --camera to select from CAMERAS in
_credentials.py, and --port/--path to avoid collisions when running several
at once (or use run_all.py to launch everything at once automatically).
"""

import argparse
import ctypes
import os
import queue
import signal
import sys
import threading
import time
from ctypes import (
    Structure, POINTER, WINFUNCTYPE, c_int, c_uint, c_long, c_ulong,
    c_ushort, c_char, c_char_p, c_void_p, c_bool, byref, string_at,
)
from datetime import datetime
import _credentials as credentials

# --------------------------------------------------------------------------
VMS_DIR      = os.path.dirname(os.path.abspath(__file__))
CHANNEL      = 0
DEFAULT_STREAM = 0          # 0 = main, 1 = sub
DEFAULT_HTTP_PORT = 8090
DEFAULT_RTSP_PORT = 8554
DEFAULT_PATH = "live"
LOGIN_PORT   = 34567

# --------------------------------------------------------------------------
# Struct / enum definitions transcribed from netsdk.h
# --------------------------------------------------------------------------

class SDK_SYSTEM_TIME(Structure):
    _fields_ = [
        ("year", c_int), ("month", c_int), ("day", c_int), ("wday", c_int),
        ("hour", c_int), ("minute", c_int), ("second", c_int), ("isdst", c_int),
    ]


class H264_DVR_DEVICEINFO(Structure):
    _fields_ = [
        ("sSoftWareVersion", c_char * 64),
        ("sHardWareVersion", c_char * 64),
        ("sEncryptVersion", c_char * 64),
        ("tmBuildTime", SDK_SYSTEM_TIME),
        ("sSerialNumber", c_char * 64),
        ("byChanNum", c_int),
        ("iVideoOutChannel", c_int),
        ("byAlarmInPortNum", c_int),
        ("byAlarmOutPortNum", c_int),
        ("iTalkInChannel", c_int),
        ("iTalkOutChannel", c_int),
        ("iExtraChannel", c_int),
        ("iAudioInChannel", c_int),
        ("iCombineSwitch", c_int),
        ("iDigChannel", c_int),
        ("uiDeviceRunTime", c_uint),
        ("deviceTye", c_int),
        ("sHardWare", c_char * 64),
        ("uUpdataTime", c_char * 20),
        ("uUpdataType", c_uint),
        ("sDeviceModel", c_char * 16),
        ("nLanguage", c_int),
        ("sCloudErrCode", c_char * 260),
        ("status", c_int * 32),
    ]


class H264_DVR_CLIENTINFO(Structure):
    _fields_ = [
        ("nChannel", c_int),
        ("nStream", c_int),
        ("nMode", c_int),
        ("nComType", c_int),
        ("hWnd", c_void_p),
    ]


class PACKET_INFO_EX(Structure):
    _fields_ = [
        ("nPacketType", c_int),
        ("pPacketBuffer", c_void_p),
        ("dwPacketSize", c_uint),
        ("nEncodeType", c_uint),
        ("nYear", c_int), ("nMonth", c_int), ("nDay", c_int),
        ("nHour", c_int), ("nMinute", c_int), ("nSecond", c_int),
        ("dwTimeStamp", c_uint), ("dwTimeStampHigh", c_uint),
        ("dwFrameNum", c_uint), ("dwFrameRate", c_uint),
        ("uWidth", c_ushort), ("uHeight", c_ushort),
        ("Reserved", c_uint * 6),
    ]


FILE_HEAD, VIDEO_I_FRAME, VIDEO_B_FRAME, VIDEO_P_FRAME = 0, 1, 2, 3
VIDEO_BP_FRAME, VIDEO_BBP_FRAME, VIDEO_J_FRAME, AUDIO_PACKET = 4, 5, 6, 10
VIDEO_PACKET_TYPES = {VIDEO_I_FRAME, VIDEO_B_FRAME, VIDEO_P_FRAME, VIDEO_BP_FRAME, VIDEO_BBP_FRAME}

ENCODE_H264, ENCODE_H265 = 2, 5

REALDATA_CB = WINFUNCTYPE(c_int, c_long, POINTER(PACKET_INFO_EX), c_long)

# Set once argparse has run, used to prefix every log line so concurrent
# instances' output stays distinguishable if merged into one place.
TAG = ""


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {TAG}{msg}", flush=True)


def to_annexb(buf: bytes) -> bytes:
    """Defensive: convert AVCC length-prefixed frames to Annex-B if we ever
    see that shape. In practice (confirmed via hex dump) this device's
    frames are already Annex-B, so this is normally a no-op passthrough."""
    if buf[:4] == b"\x00\x00\x00\x01" or buf[:3] == b"\x00\x00\x01":
        return buf
    out = bytearray()
    i, n = 0, len(buf)
    while i + 4 <= n:
        length = int.from_bytes(buf[i:i + 4], "big")
        i += 4
        if length <= 0 or i + length > n:
            if out:
                return bytes(out)
            return bytes(buf)
        out += b"\x00\x00\x00\x01"
        out += buf[i:i + length]
        i += length
    return bytes(out) if out else bytes(buf)


def strip_private_markers(buf: bytes) -> bytes:
    """Remove SDK-internal packet-length pseudo-NALs that leak into the
    SetRealDataCallBack_V2 buffer ahead of real frame data. Confirmed via
    hex dump: these always have forbidden_zero_bit=1 (byte0's top bit),
    which is never valid in a real HEVC/H264 NAL header, and their trailing
    4 bytes are a little-endian length of the NAL that immediately follows
    (length_field == next_real_NAL_length + 4). ffmpeg correctly rejects
    them as invalid, which was breaking decoder sync entirely (blank
    video). Drop any such segment; keep everything else untouched."""
    out = bytearray()
    n = len(buf)
    starts = []
    j = 0
    while j < n - 2:
        if buf[j:j + 3] == b"\x00\x00\x01":
            sc_start = j - 1 if (j > 0 and buf[j - 1] == 0) else j
            sc_len = 4 if sc_start == j - 1 else 3
            starts.append((sc_start, sc_len))
            j += 3
        else:
            j += 1
    if not starts:
        return buf
    for idx, (pos, sclen) in enumerate(starts):
        nal_start = pos + sclen
        nal_end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        if nal_end <= nal_start:
            continue
        forbidden_bit = (buf[nal_start] >> 7) & 1
        if forbidden_bit:
            continue
        out += buf[pos:nal_end]
    return bytes(out)


class CloudCamera:
    def __init__(self, dll_dir):
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)
        self.dll = ctypes.WinDLL(os.path.join(dll_dir, "NetSdk.dll"))
        self._bind()
        self.login_id = None
        self.real_handle = None
        self._cb_ref = None

    def _bind(self):
        d = self.dll
        d.H264_DVR_Init.argtypes = [c_void_p, c_ulong]
        d.H264_DVR_Init.restype = c_long

        d.H264_DVR_SetConnectTime.argtypes = [c_long, c_long]
        d.H264_DVR_SetConnectTime.restype = c_bool

        try:
            d.H264_DVR_SkipP2P.argtypes = [c_long, c_bool]
            d.H264_DVR_SkipP2P.restype = c_bool
        except AttributeError:
            pass

        d.H264_DVR_Login_Cloud.argtypes = [
            c_char_p, c_ushort, c_char_p, c_char_p,
            POINTER(H264_DVR_DEVICEINFO), POINTER(c_int), c_char_p,
        ]
        d.H264_DVR_Login_Cloud.restype = c_long

        d.H264_DVR_RealPlay.argtypes = [c_long, POINTER(H264_DVR_CLIENTINFO)]
        d.H264_DVR_RealPlay.restype = c_long

        d.H264_DVR_SetRealDataCallBack_V2.argtypes = [c_long, REALDATA_CB, c_long]
        d.H264_DVR_SetRealDataCallBack_V2.restype = c_bool

        d.H264_DVR_StopRealPlay.argtypes = [c_long, c_void_p]
        d.H264_DVR_StopRealPlay.restype = c_bool

        d.H264_DVR_Logout.argtypes = [c_long]
        d.H264_DVR_Logout.restype = c_long

        d.H264_DVR_Cleanup.restype = c_bool
        d.H264_DVR_GetLastError.restype = c_long

    def init(self):
        ok = self.dll.H264_DVR_Init(None, 0)
        if ok <= 0:
            raise RuntimeError(f"H264_DVR_Init failed ({ok})")
        self.dll.H264_DVR_SetConnectTime(20000, 3)
        time.sleep(4)

    def login(self, cloud_id, username, password, port=LOGIN_PORT, retries=20, retry_delay=6.0, skip_p2p=False):
        last_err = None
        for attempt in range(1, retries + 1):
            info = H264_DVR_DEVICEINFO()
            err = c_int(0)
            login_id = self.dll.H264_DVR_Login_Cloud(
                cloud_id.encode(), port, username.encode(), password.encode(),
                byref(info), byref(err), b"",
            )
            if login_id:
                self.login_id = login_id
                log(f"[+] Logged in. Device serial: {info.sSerialNumber.decode(errors='ignore')}, "
                    f"channels: {info.byChanNum}")
                return info
            last_err = err.value
            cloud_err = info.sCloudErrCode.decode(errors="ignore").strip("\x00")
            log(f"[!] Attempt {attempt}/{retries} failed: error={err.value} "
                f"cloudErrCode='{cloud_err}' "
                f"(H264_DVR_GetLastError={self.dll.H264_DVR_GetLastError()})")
            if skip_p2p and hasattr(self.dll, "H264_DVR_SkipP2P") and attempt == 1:
                try:
                    log("[i] Trying experimental H264_DVR_SkipP2P(0, True)...")
                    self.dll.H264_DVR_SkipP2P(0, True)
                except Exception as e:
                    log(f"[!] SkipP2P call itself raised: {e}")
            time.sleep(retry_delay)
        raise RuntimeError(
            f"Cloud login failed after {retries} attempts, last error code {last_err}. "
            "If another process/window is already connected to this same CloudID "
            "(VMS.exe, or another instance of this script on a different stream), "
            "that can cause a transient device-registration lookup failure -- the "
            "retries above are meant to ride that out, but if it never resolves, "
            "try running just one session against this CloudID at a time."
        )

    def start_real_play(self, channel, stream, on_frame):
        client_info = H264_DVR_CLIENTINFO(nChannel=channel, nStream=stream, nMode=0, nComType=0, hWnd=None)
        handle = self.dll.H264_DVR_RealPlay(self.login_id, byref(client_info))
        if not handle:
            raise RuntimeError(f"H264_DVR_RealPlay failed (error={self.dll.H264_DVR_GetLastError()})")
        self.real_handle = handle

        def _trampoline(lRealHandle, pFrame, dwUser):
            try:
                on_frame(pFrame.contents)
            except Exception as e:
                log(f"[!] callback error: {e}")
            return 0

        self._cb_ref = REALDATA_CB(_trampoline)
        ok = self.dll.H264_DVR_SetRealDataCallBack_V2(handle, self._cb_ref, 0)
        if not ok:
            raise RuntimeError("H264_DVR_SetRealDataCallBack_V2 failed")
        log(f"[+] Real-time stream started (handle={handle})")

    def stop(self):
        if self.real_handle:
            self.dll.H264_DVR_StopRealPlay(self.real_handle, None)
            self.real_handle = None
        if self.login_id:
            self.dll.H264_DVR_Logout(self.login_id)
            self.login_id = None
        self.dll.H264_DVR_Cleanup()


class FfmpegBridge:
    def __init__(self, ffmpeg_path, protocol="http", port=DEFAULT_HTTP_PORT, path=DEFAULT_PATH,
                 push_url=None, max_queue=500):
        self.ffmpeg_path = ffmpeg_path
        self.protocol = protocol
        self.port = port
        self.path = path
        self.push_url = push_url
        self.proc = None
        self._codec = None
        self._fps = None
        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=max_queue)
        self._writer_thread = None
        self._monitor_thread = None
        self._stop = threading.Event()
        self._dropped = 0
        self._last_input_at = 0
        self._last_write_at = time.monotonic()

    def _spawn(self, codec, fps):
        import subprocess
        input_fmt = "h264" if codec == ENCODE_H264 else "hevc"
        # Raw elementary stream input carries no timing info at all; genpts
        # alone isn't enough for the mpegts/rtsp muxer to assign valid
        # per-frame timestamps under -c copy (that flag only backfills PTS
        # from DTS, and there's no DTS here either). Telling the demuxer the
        # actual frame rate up front (from the SDK's own dwFrameRate) gives
        # it a real basis to generate correct, monotonic timestamps from.
        common = [
            self.ffmpeg_path, "-loglevel", "warning",
            "-fflags", "+discardcorrupt+genpts",
            "-analyzeduration", "10000000", "-probesize", "5000000",
            "-r", str(fps or 15),
            "-f", input_fmt, "-i", "pipe:0", "-c", "copy",
        ]

        if self.protocol == "http":
            url = f"http://127.0.0.1:{self.port}/{self.path}"
            cmd = common + ["-f", "mpegts", "-listen", "1", url]
        elif self.protocol == "rtsp":
            url = f"rtsp://127.0.0.1:{self.port}/{self.path}"
            cmd = common + ["-f", "rtsp", "-rtsp_flags", "listen", url]
        else:  # push -- connect out to an already-running RTSP server (e.g. MediaMTX)
            url = self.push_url
            cmd = common + ["-f", "rtsp", "-rtsp_transport", "tcp", url]

        log(f"[+] Launching: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, bufsize=0,
        )
        self._last_write_at = time.monotonic()
        self._codec = codec
        self._fps = fps
        if self.protocol == "push":
            log(f"[+] Pushing to: {url}  (open that same path on MediaMTX's own port in VLC)")
        else:
            log(f"[+] Open this in VLC now: {url}")

        def _read_stderr(proc):
            for raw_line in proc.stderr:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    log(f"[ffmpeg] {line}")

        threading.Thread(target=_read_stderr, args=(self.proc,), daemon=True).start()

        def _drain():
            while not self._stop.is_set():
                try:
                    data = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    break
                with self._lock:
                    proc = self.proc
                    if proc is None or proc.poll() is not None:
                        if proc is not None:
                            log(f"[!] ffmpeg exited (code {proc.returncode}); restarting")
                            self.proc = None
                        continue
                try:
                    if proc.stdin:
                        proc.stdin.write(data)
                        proc.stdin.flush()
                        self._last_write_at = time.monotonic()
                except (BrokenPipeError, OSError) as e:
                    log(f"[i] ffmpeg pipe closed ({e}); will restart listener for the next viewer")
                    with self._lock:
                        if self.proc is proc:
                            self.proc = None

        if self._writer_thread is None or not self._writer_thread.is_alive():
            self._writer_thread = threading.Thread(target=_drain, daemon=True)
            self._writer_thread.start()

        def _monitor():
            while not self._stop.wait(5):
                with self._lock:
                    proc = self.proc
                    last_input_at = self._last_input_at
                    last_write_at = self._last_write_at
                if proc is None:
                    continue
                if proc.poll() is not None:
                    log(f"[!] ffmpeg exited (code {proc.returncode}); restarting on next frame")
                    with self._lock:
                        if self.proc is proc:
                            self.proc = None
                    continue
                if last_input_at > last_write_at and time.monotonic() - last_write_at >= 15:
                    log("[!] ffmpeg server stopped accepting stream data; restarting it")
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        log("[!] ffmpeg did not terminate; killing it")
                        proc.kill()
                        proc.wait()
                    except OSError:
                        pass
                    with self._lock:
                        if self.proc is proc:
                            self.proc = None

        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(target=_monitor, daemon=True)
            self._monitor_thread.start()

    def write(self, codec, data, fps=None):
        self._last_input_at = time.monotonic()
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    log(f"[!] ffmpeg exited (code {self.proc.returncode}); restarting")
                    self.proc = None
                try:
                    self._spawn(codec, fps)
                except OSError as e:
                    log(f"[!] Could not start ffmpeg: {e}")
                    return
        try:
            self._q.put_nowait(data)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 50 == 1:
                log(f"[!] ffmpeg falling behind, dropped {self._dropped} frames so far")

    def close(self):
        self._stop.set()
        if self._writer_thread:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass
            self._writer_thread.join(timeout=2)
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


def resolve_credentials(camera_name):
    """Supports two _credentials.py shapes:
      - single-camera (backwards compatible): CLOUD_ID / DEVICE_USER / DEVICE_PASS
      - multi-camera: CAMERAS = {"name": {"cloud_id":..,"user":..,"password":..}, ...}
    --camera selects from CAMERAS; omit it to use the flat single-camera vars."""
    if camera_name:
        cams = getattr(credentials, "CAMERAS", None)
        if not cams or camera_name not in cams:
            available = ", ".join(cams.keys()) if cams else "(none defined)"
            sys.exit(f"--camera '{camera_name}' not found in _credentials.py CAMERAS. "
                      f"Available: {available}")
        c = cams[camera_name]
        return c["cloud_id"], c["user"], c["password"]
    return credentials.CLOUD_ID, credentials.DEVICE_USER, credentials.DEVICE_PASS


def main():
    global TAG
    import subprocess

    ap = argparse.ArgumentParser()
    ap.add_argument("--vms-dir", default=VMS_DIR)
    ap.add_argument("--camera", default=None,
                     help="Name of an entry in _credentials.py's CAMERAS dict. "
                          "Omit to use the flat CLOUD_ID/DEVICE_USER/DEVICE_PASS vars.")
    ap.add_argument("--cloud-id", default=None, help="Overrides whatever --camera/defaults resolve to")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--channel", type=int, default=CHANNEL)
    ap.add_argument("--stream", type=int, default=DEFAULT_STREAM, help="0=main, 1=sub")
    ap.add_argument("--protocol", choices=["http", "rtsp", "push"], default="http",
                     help="http/rtsp: this script listens itself (single stream, own port). "
                          "push: connect out to an already-running RTSP server (e.g. MediaMTX) "
                          "instead -- needed to put multiple streams on one shared port, or for "
                          "on-demand start/stop. See README.")
    ap.add_argument("--push-url", default=None,
                     help="Target URL for --protocol push. Defaults to "
                          "rtsp://127.0.0.1:8554/<camera>_<stream> if omitted.")
    ap.add_argument("--port", type=int, default=None,
                     help="Local port to serve on. Required to differ across concurrent "
                          "instances. Defaults to 8090 (http) / 8554 (rtsp) if omitted -- "
                          "fine for a single instance, but set explicitly when running "
                          "more than one at once.")
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--probe", metavar="FILE")
    ap.add_argument("--probe-seconds", type=float, default=5.0)
    ap.add_argument("--skip-p2p", action="store_true")
    ap.add_argument("--convert", nargs=2, metavar=("IN", "OUT"))
    args = ap.parse_args()

    stream_label = "main" if args.stream == 0 else f"stream{args.stream}"
    TAG = f"[{args.camera or 'default'}/{stream_label}] "

    if args.convert:
        src, dst = args.convert
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(strip_private_markers(to_annexb(data)))
        log(f"[+] Converted {src} -> {dst} ({len(data)} -> {os.path.getsize(dst)} bytes)")
        return

    cloud_id, user, password = resolve_credentials(args.camera)
    cloud_id = args.cloud_id or cloud_id
    user = args.user or user
    password = args.password or password

    try:
        v = subprocess.run([os.path.join(args.vms_dir, "ffmpeg.exe"), "-version"],
                            capture_output=True, text=True, timeout=5)
        log("[i] " + (v.stdout.splitlines()[0] if v.stdout else "(no version output)"))
    except Exception as e:
        log(f"[!] Could not check ffmpeg -version: {e}")

    cam = CloudCamera(args.vms_dir)
    cam.init()
    cam.login(cloud_id, user, password, skip_p2p=args.skip_p2p)

    stats = {"frames": 0, "bytes": 0, "start": time.time()}

    if args.probe:
        f = open(args.probe, "wb")
        deadline = time.time() + args.probe_seconds

        def on_frame(frame):
            if frame.nPacketType in VIDEO_PACKET_TYPES and frame.dwPacketSize:
                if stats["frames"] == 0:
                    codec_name = {ENCODE_H264: "H264", ENCODE_H265: "H265"}.get(
                        frame.nEncodeType, f"unknown({frame.nEncodeType})")
                    log(f"[i] First frame: nEncodeType={frame.nEncodeType} ({codec_name}), "
                        f"{frame.uWidth}x{frame.uHeight}")
                buf = strip_private_markers(to_annexb(string_at(frame.pPacketBuffer, frame.dwPacketSize)))
                f.write(buf)
                stats["frames"] += 1
                stats["bytes"] += len(buf)

        cam.start_real_play(args.channel, args.stream, on_frame)
        while time.time() < deadline:
            time.sleep(0.2)
        f.close()
        log(f"[+] Wrote {stats['bytes']} bytes / {stats['frames']} frames to {args.probe}")
        cam.stop()
        return

    port = args.port or (DEFAULT_RTSP_PORT if args.protocol == "rtsp" else DEFAULT_HTTP_PORT)
    push_url = args.push_url or f"rtsp://127.0.0.1:{DEFAULT_RTSP_PORT}/{args.camera or 'cam'}_{stream_label}"
    bridge = FfmpegBridge(os.path.join(args.vms_dir, "ffmpeg.exe"),
                          protocol=args.protocol, port=port, path=args.path, push_url=push_url)
    last_frame_at = time.monotonic()

    def on_frame(frame):
        nonlocal last_frame_at
        if frame.nPacketType in VIDEO_PACKET_TYPES and frame.dwPacketSize:
            buf = strip_private_markers(to_annexb(string_at(frame.pPacketBuffer, frame.dwPacketSize)))
            bridge.write(frame.nEncodeType, buf, fps=frame.dwFrameRate)
            stats["frames"] += 1
            stats["bytes"] += len(buf)
            last_frame_at = time.monotonic()

    def start_camera_stream():
        cam.start_real_play(args.channel, args.stream, on_frame)

    start_camera_stream()

    # Graceful shutdown: if this process is running under MediaMTX's
    # runOnDemand (protocol=push), MediaMTX stops it with a normal process
    # termination once the last viewer disconnects, not Ctrl+C -- catch
    # that too so the camera gets logged out cleanly rather than left in a
    # half-open session. (Best-effort on Windows: a forceful kill can still
    # bypass this, same as it would for any process.)
    stop_requested = threading.Event()

    def _handle_term(signum, frame):
        stop_requested.set()

    try:
        signal.signal(signal.SIGTERM, _handle_term)
    except (ValueError, AttributeError):
        pass  # not available in this environment; Ctrl+C still works below

    log("[i] Streaming... Ctrl+C to stop.")
    try:
        while not stop_requested.is_set():
            time.sleep(20)
            silence_for = time.monotonic() - last_frame_at
            if silence_for >= 60:
                log(f"[!] No video frames received for {silence_for:.0f}s; reconnecting camera session")
                last_frame_at = time.monotonic()
                try:
                    bridge.close()
                    bridge = FfmpegBridge(os.path.join(args.vms_dir, "ffmpeg.exe"),
                                          protocol=args.protocol, port=port,
                                          path=args.path, push_url=push_url)
                    cam.stop()
                    cam.init()
                    cam.login(cloud_id, user, password, skip_p2p=args.skip_p2p)
                    start_camera_stream()
                    log("[+] Camera session reconnected; waiting for video frames")
                except Exception as e:
                    log(f"[!] Camera session reconnect failed: {e}; retrying in 20s")
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        cam.stop()
        log("[+] Stopped cleanly.")


if __name__ == "__main__":
    main()