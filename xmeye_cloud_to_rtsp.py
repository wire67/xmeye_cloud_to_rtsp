"""
xmeye_cloud_to_rtsp.py
-----------------------
Logs into a cloud camera (CloudID + user + password, same as VMS's Device
Manager) via NetSdk.dll, and re-publishes its H264/H265 stream locally over
HTTP/MPEG-TS for VLC. See README.md for setup, background, and troubleshooting.
"""

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
from ctypes import (
    Structure, POINTER, WINFUNCTYPE, c_int, c_uint, c_long, c_ulong,
    c_ushort, c_char, c_char_p, c_void_p, c_bool, byref, string_at,
)
import _credentials as credentials

# --------------------------------------------------------------------------
VMS_DIR      = os.path.dirname(os.path.abspath(__file__))
CHANNEL      = 0
SUB_STREAM   = 0
HTTP_PORT    = 8090
HTTP_PATH    = "live.ts"
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
        return buf  # nothing looks like Annex-B at all; leave untouched
    for idx, (pos, sclen) in enumerate(starts):
        nal_start = pos + sclen
        nal_end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        if nal_end <= nal_start:
            continue
        forbidden_bit = (buf[nal_start] >> 7) & 1
        if forbidden_bit:
            continue  # drop this segment (start code + payload) entirely
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
                print(f"[+] Logged in. Device serial: {info.sSerialNumber.decode(errors='ignore')}, "
                      f"channels: {info.byChanNum}")
                return info
            last_err = err.value
            cloud_err = info.sCloudErrCode.decode(errors="ignore").strip("\x00")
            print(f"[!] Attempt {attempt}/{retries} failed: error={err.value} "
                  f"cloudErrCode='{cloud_err}' "
                  f"(H264_DVR_GetLastError={self.dll.H264_DVR_GetLastError()})")
            if skip_p2p and hasattr(self.dll, "H264_DVR_SkipP2P") and attempt == 1:
                try:
                    print("[i] Trying experimental H264_DVR_SkipP2P(0, True)...")
                    self.dll.H264_DVR_SkipP2P(0, True)
                except Exception as e:
                    print(f"[!] SkipP2P call itself raised: {e}")
            time.sleep(retry_delay)
        raise RuntimeError(f"Cloud login failed after {retries} attempts, last error code {last_err}.")

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
                print(f"[!] callback error: {e}", file=sys.stderr)
            return 0

        self._cb_ref = REALDATA_CB(_trampoline)
        ok = self.dll.H264_DVR_SetRealDataCallBack_V2(handle, self._cb_ref, 0)
        if not ok:
            raise RuntimeError("H264_DVR_SetRealDataCallBack_V2 failed")
        print(f"[+] Real-time stream started (handle={handle})")

    def stop(self):
        if self.real_handle:
            self.dll.H264_DVR_StopRealPlay(self.real_handle, None)
            self.real_handle = None
        if self.login_id:
            self.dll.H264_DVR_Logout(self.login_id)
            self.login_id = None
        self.dll.H264_DVR_Cleanup()


class FfmpegHttpBridge:
    """Spawns ffmpeg.exe reading raw H264/H265 from stdin and re-serving it as
    MPEG-TS over HTTP (ffmpeg acting as its own tiny HTTP server via
    -listen 1). This is a much older/more universally-supported muxer
    combination than RTSP-listen-mode-with-HEVC, which repeatedly failed to
    ever open its listening socket despite input parsing succeeding fine.

    Writes go through a bounded queue drained by a dedicated thread, NOT
    directly from the caller's thread (the caller is the SDK's own internal
    callback thread -- a blocking write() here would freeze frame delivery
    from the camera entirely if ffmpeg's pipe ever backs up)."""

    def __init__(self, ffmpeg_path, port=HTTP_PORT, path=HTTP_PATH, max_queue=500):
        self.ffmpeg_path = ffmpeg_path
        self.port = port
        self.path = path
        self.proc = None
        self._codec = None
        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=max_queue)
        self._writer_thread = None
        self._stop = threading.Event()
        self._dropped = 0

    def _spawn(self, codec):
        import subprocess
        input_fmt = "h264" if codec == ENCODE_H264 else "hevc"
        url = f"http://127.0.0.1:{self.port}/{self.path}"
        cmd = [
            self.ffmpeg_path, "-loglevel", "verbose",
            "-fflags", "+discardcorrupt+genpts",
            "-analyzeduration", "10000000", "-probesize", "5000000",
            "-f", input_fmt, "-i", "pipe:0",
            "-c", "copy", "-f", "mpegts", "-listen", "1", url,
        ]
        print("[+] Launching:", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self._codec = codec
        print(f"[+] Open this in VLC now:  {url}")

        def _drain():
            while not self._stop.is_set():
                try:
                    data = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    break
                try:
                    if self.proc.stdin:
                        self.proc.stdin.write(data)
                except (BrokenPipeError, OSError) as e:
                    print(f"[!] ffmpeg pipe write failed, stream likely dead: {e}")
                    break

        self._writer_thread = threading.Thread(target=_drain, daemon=True)
        self._writer_thread.start()

    def write(self, codec, data):
        with self._lock:
            if self.proc is None:
                self._spawn(codec)
        try:
            self._q.put_nowait(data)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 50 == 1:
                print(f"[!] ffmpeg falling behind, dropped {self._dropped} frames so far")

    def close(self):
        self._stop.set()
        if self._writer_thread:
            self._q.put(None)
            self._writer_thread.join(timeout=2)
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()


def main():
    import subprocess
    try:
        v = subprocess.run([os.path.join(VMS_DIR, "ffmpeg.exe"), "-version"],
                            capture_output=True, text=True, timeout=5)
        print("[i]", v.stdout.splitlines()[0] if v.stdout else "(no version output)")
    except Exception as e:
        print(f"[!] Could not check ffmpeg -version: {e}")

    ap = argparse.ArgumentParser()
    ap.add_argument("--vms-dir", default=VMS_DIR)
    ap.add_argument("--cloud-id", default=credentials.CLOUD_ID)
    ap.add_argument("--user", default=credentials.DEVICE_USER)
    ap.add_argument("--password", default=credentials.DEVICE_PASS)
    ap.add_argument("--channel", type=int, default=CHANNEL)
    ap.add_argument("--stream", type=int, default=SUB_STREAM, help="0=main, 1=sub")
    ap.add_argument("--probe", metavar="FILE")
    ap.add_argument("--probe-seconds", type=float, default=5.0)
    ap.add_argument("--skip-p2p", action="store_true")
    ap.add_argument("--convert", nargs=2, metavar=("IN", "OUT"))
    args = ap.parse_args()

    if args.convert:
        src, dst = args.convert
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(strip_private_markers(to_annexb(data)))
        print(f"[+] Converted {src} -> {dst} ({len(data)} -> {os.path.getsize(dst)} bytes)")
        return

    cam = CloudCamera(args.vms_dir)
    cam.init()
    cam.login(args.cloud_id, args.user, args.password, skip_p2p=args.skip_p2p)

    stats = {"frames": 0, "bytes": 0, "start": time.time()}

    if args.probe:
        f = open(args.probe, "wb")
        deadline = time.time() + args.probe_seconds

        def on_frame(frame):
            if frame.nPacketType in VIDEO_PACKET_TYPES and frame.dwPacketSize:
                if stats["frames"] == 0:
                    codec_name = {ENCODE_H264: "H264", ENCODE_H265: "H265"}.get(
                        frame.nEncodeType, f"unknown({frame.nEncodeType})")
                    print(f"[i] First frame: nEncodeType={frame.nEncodeType} ({codec_name}), "
                          f"{frame.uWidth}x{frame.uHeight}")
                buf = strip_private_markers(to_annexb(string_at(frame.pPacketBuffer, frame.dwPacketSize)))
                f.write(buf)
                stats["frames"] += 1
                stats["bytes"] += len(buf)

        cam.start_real_play(args.channel, args.stream, on_frame)
        while time.time() < deadline:
            time.sleep(0.2)
        f.close()
        print(f"[+] Wrote {stats['bytes']} bytes / {stats['frames']} frames to {args.probe}")
        cam.stop()
        return

    bridge = FfmpegHttpBridge(os.path.join(args.vms_dir, "ffmpeg.exe"))

    def on_frame(frame):
        if frame.nPacketType in VIDEO_PACKET_TYPES and frame.dwPacketSize:
            buf = strip_private_markers(to_annexb(string_at(frame.pPacketBuffer, frame.dwPacketSize)))
            bridge.write(frame.nEncodeType, buf)
            stats["frames"] += 1
            stats["bytes"] += len(buf)

    cam.start_real_play(args.channel, args.stream, on_frame)

    print("[i] Streaming... Ctrl+C to stop.")
    try:
        while True:
            time.sleep(5)
            elapsed = time.time() - stats["start"]
            print(f"[i] {stats['frames']} frames, {stats['bytes']/1024:.0f} KB, "
                  f"{stats['frames']/max(elapsed,1):.1f} fps avg")
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        cam.stop()
        print("[+] Stopped cleanly.")


if __name__ == "__main__":
    main()