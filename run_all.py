"""
run_all.py
-----------
Launches one xmeye_cloud_to_rtsp.py subprocess per (camera, stream)
combination defined in _credentials.py's CAMERAS dict, each on its own
auto-assigned port, and prints a summary of every VLC URL once they're all
starting up.

Usage:
    python run_all.py                       # every camera, both main+sub
    python run_all.py --streams 0            # every camera, main only
    python run_all.py --cameras front back   # only these cameras
    python run_all.py --protocol rtsp

_credentials.py must define CAMERAS, e.g.:
    CAMERAS = {
        "front": {"cloud_id": "...", "user": "admin", "password": "..."},
        "back":  {"cloud_id": "...", "user": "admin", "password": "..."},
    }

Ctrl+C stops every child process cleanly.
"""

import argparse
import os
import subprocess
import sys
import time

import _credentials as credentials

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xmeye_cloud_to_rtsp.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", nargs="*", default=None,
                     help="Subset of CAMERAS names to launch. Default: all of them.")
    ap.add_argument("--streams", nargs="*", type=int, default=[1],
                     help="Which stream indices to launch per camera (0=main, 1=sub). Default: 1.")
    ap.add_argument("--protocol", choices=["http", "rtsp"], default="http")
    ap.add_argument("--base-port", type=int, default=8090,
                     help="First port used; each subsequent (camera, stream) combo gets base_port+N.")
    args = ap.parse_args()

    cams = getattr(credentials, "CAMERAS", None)
    if not cams:
        sys.exit("_credentials.py has no CAMERAS dict defined -- see this script's docstring "
                  "for the expected shape.")

    names = args.cameras or list(cams.keys())
    for n in names:
        if n not in cams:
            sys.exit(f"Unknown camera '{n}'. Available: {', '.join(cams.keys())}")

    procs = []
    summary = []
    port = args.base_port
    for name in names:
        for stream in args.streams:
            url_scheme = "rtsp" if args.protocol == "rtsp" else "http"
            path = "live.ts" if args.protocol == "http" else "live"
            cmd = [
                sys.executable, SCRIPT,
                "--camera", name,
                "--stream", str(stream),
                "--protocol", args.protocol,
                "--port", str(port),
                "--path", path,
            ]
            print(f"[launcher] starting {name} stream={stream} on port {port}: {' '.join(cmd)}")
            p = subprocess.Popen(cmd)
            procs.append(p)
            summary.append((name, stream, f"{url_scheme}://127.0.0.1:{port}/{path}"))
            port += 1
            time.sleep(1)  # stagger startups a bit rather than hammering everything at once

    print("\n[launcher] All started. VLC URLs (may take ~15-30s each to come up):")
    for name, stream, url in summary:
        label = "main" if stream == 0 else f"stream{stream}"
        print(f"  {name:12s} {label:8s} {url}")
    print("\n[launcher] Ctrl+C to stop everything.\n")

    try:
        while True:
            time.sleep(2)
            for p in procs:
                if p.poll() is not None:
                    print(f"[launcher] warning: a child process exited (code {p.returncode})")
    except KeyboardInterrupt:
        print("\n[launcher] Stopping all child processes...")
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[launcher] Done.")


if __name__ == "__main__":
    main()
