from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_file, abort
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# Browsers to try for cookie extraction, in order. YouTube increasingly requires
# authenticated requests (PO tokens / SABR). Pulling cookies from a logged-in
# browser is the most reliable workaround.
COOKIE_BROWSERS = ("chrome", "brave", "edge", "firefox", "chromium", "arc")


def _pick_cookie_browser() -> Optional[str]:
    candidates = {
        "chrome": "~/Library/Application Support/Google/Chrome",
        "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "edge": "~/Library/Application Support/Microsoft Edge",
        "firefox": "~/Library/Application Support/Firefox",
        "chromium": "~/Library/Application Support/Chromium",
        "arc": "~/Library/Application Support/Arc",
    }
    for name in COOKIE_BROWSERS:
        if Path(os.path.expanduser(candidates[name])).exists():
            return name
    return None


def _find_browser_with_instagram_session() -> Optional[tuple]:
    """Find a browser (Chromium-family or Safari) that's logged into Instagram.

    Returns a tuple usable as yt-dlp's `cookiesfrombrowser` — either
    (browser, profile_dir) for Chromium browsers or (browser,) for Safari —
    or None if no logged-in session is found.
    """
    import sqlite3
    import shutil
    import subprocess

    chromium_roots = {
        "chrome": "~/Library/Application Support/Google/Chrome",
        "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "edge": "~/Library/Application Support/Microsoft Edge",
        "chromium": "~/Library/Application Support/Chromium",
    }
    for browser, root in chromium_roots.items():
        root_path = Path(os.path.expanduser(root))
        if not root_path.exists():
            continue
        for profile_dir in sorted(root_path.iterdir()):
            if not profile_dir.is_dir():
                continue
            if profile_dir.name not in ("Default",) and not profile_dir.name.startswith("Profile "):
                continue
            cookies_db = profile_dir / "Cookies"
            if not cookies_db.exists():
                continue
            try:
                tmp_db = Path(tempfile.gettempdir()) / f"yt_cookie_probe_{os.getpid()}.db"
                shutil.copy(cookies_db, tmp_db)
                with sqlite3.connect(tmp_db) as conn:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%instagram%' AND name='sessionid'"
                    )
                    if cur.fetchone()[0] > 0:
                        return (browser, profile_dir.name)
            except Exception:
                continue
            finally:
                try:
                    tmp_db.unlink()
                except Exception:
                    pass

    # Safari uses a proprietary binary cookie format, so we can't sqlite-probe.
    # Probe by asking yt-dlp to export Safari cookies to a temp Netscape file
    # and grep for sessionid. Safari's cookie file is also TCC-protected;
    # this returns nothing gracefully if Python lacks Full Disk Access.
    safari_cookies = Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    if safari_cookies.exists():
        probe_file = Path(tempfile.gettempdir()) / f"safari_probe_{os.getpid()}.txt"
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    "--cookies-from-browser", "safari",
                    "--cookies", str(probe_file),
                    "--skip-download",
                    "https://example.com/",
                ],
                capture_output=True, timeout=15,
            )
            if probe_file.exists():
                content = probe_file.read_text(errors="ignore")
                if "instagram" in content.lower() and "sessionid" in content.lower():
                    return ("safari",)
        except Exception:
            pass
        finally:
            try:
                probe_file.unlink()
            except Exception:
                pass

    return None

app = Flask(__name__)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "video_scraper_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# job_id -> {"status": "pending|done|error", "file": Path|None, "title": str, "error": str|None}
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

ALLOWED_HOSTS = re.compile(
    r"^(https?://)?([\w-]+\.)*("
    r"youtube\.com|youtu\.be|"
    r"pinterest\.com|pin\.it|pinterest\.[a-z.]+|"
    r"instagram\.com|instagr\.am"
    r")(/|$)",
    re.IGNORECASE,
)

INSTAGRAM_RE = re.compile(r"^(https?://)?([\w-]+\.)*(instagram\.com|instagr\.am)/", re.IGNORECASE)

YOUTUBE_RE = re.compile(r"^(https?://)?([\w-]+\.)*(youtube\.com|youtu\.be)(/|$)", re.IGNORECASE)


def is_allowed_url(url: str) -> bool:
    return bool(ALLOWED_HOSTS.match(url.strip()))


def is_instagram_url(url: str) -> bool:
    return bool(INSTAGRAM_RE.match(url.strip()))


def is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_RE.match(url.strip()))


def download_video(job_id: str, url: str, audio_only: bool = False) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Path to the bgutil-pot script (generates Proof-of-Origin tokens that
    # YouTube now requires for most videos). Look in the Docker location
    # first, then fall back to the local-dev install in ~/.
    pot_script_candidates = [
        Path(os.environ.get("BGUTIL_POT_HOME", "/opt/bgutil-pot")) / "server/build/generate_once.js",
        Path.home() / "bgutil-pot/server/build/generate_once.js",
    ]
    pot_script = next((p for p in pot_script_candidates if p.exists()), pot_script_candidates[-1])

    ydl_opts = {
        "outtmpl": str(job_dir / "%(title).200B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 30,
        "fragment_retries": 30,
        "extractor_retries": 5,
        "concurrent_fragment_downloads": 4,
        "extractor_args": {},
    }

    if audio_only:
        # Pull best audio stream and re-encode to mp3 via ffmpeg.
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        })
    else:
        # Fallback chain: best video+audio merge, then best single stream, then anything.
        ydl_opts.update({
            "format": "bv*+ba/b/best",
            "merge_output_format": "mp4",
        })

    # Wire up bgutil-pot so YouTube actually serves us video data. With this
    # the default player_client selection works on its own — adding cookies
    # or custom user-agents tends to push YouTube down a more restrictive
    # path that yields "format not available" errors.
    if pot_script.exists():
        ydl_opts["extractor_args"]["youtubepot-bgutilscript"] = {
            "script_path": [str(pot_script)],
        }

    # Instagram blocks most reels/posts unless the request carries a logged-in
    # session. Pull cookies from a browser profile that's actually logged in
    # (we scan profiles for a real `sessionid` cookie rather than guessing).
    # (We skip cookies for YouTube because they make YouTube *more*
    # restrictive — see comment above.)
    if is_instagram_url(url):
        cookie_file = os.environ.get("IG_COOKIES_FILE")
        if cookie_file and Path(cookie_file).exists():
            ydl_opts["cookiefile"] = cookie_file
        else:
            match = _find_browser_with_instagram_session()
            if match:
                ydl_opts["cookiesfrombrowser"] = match
    # YouTube gates some videos (e.g. licensed "- Topic" tracks) behind a
    # "sign in to confirm you're not a bot" check that the PO token alone can't
    # satisfy from a datacenter IP. This is opt-in: only when YT_COOKIES_FILE is
    # set do we attach a logged-in cookies file for YouTube. Left unset, YouTube
    # behaves as before (PO token only), avoiding the over-restriction noted above.
    elif is_youtube_url(url):
        yt_cookie_file = os.environ.get("YT_COOKIES_FILE")
        if yt_cookie_file and Path(yt_cookie_file).exists():
            ydl_opts["cookiefile"] = yt_cookie_file

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]
            filename = Path(ydl.prepare_filename(info))
            # Post-processing (audio extraction, mp4 merge) rewrites the
            # extension, so the prepared filename may no longer exist.
            # Resolve by finding the actual file on disk, preferring the
            # expected extension for the chosen mode.
            preferred_ext = ".mp3" if audio_only else ".mp4"
            if not filename.exists() or filename.suffix != preferred_ext:
                vid_id = info.get("id", "")
                # First try exact match on preferred extension.
                preferred = [p for p in job_dir.glob(f"*{vid_id}*") if p.suffix == preferred_ext]
                if preferred:
                    filename = preferred[0]
                else:
                    candidates = [p for p in job_dir.glob(f"*{vid_id}*") if p.is_file()]
                    if candidates:
                        # Pick the largest — that's the final merged/encoded output.
                        filename = max(candidates, key=lambda p: p.stat().st_size)

            with jobs_lock:
                jobs[job_id] = {
                    "status": "done",
                    "file": filename,
                    "title": info.get("title", filename.name),
                    "error": None,
                }
    except DownloadError as e:
        msg = str(e)
        if "Instagram sent an empty media response" in msg:
            msg = (
                "Instagram requires a logged-in session and none was found. "
                "Log into instagram.com in Chrome (any profile), then retry. "
                "If you're already logged in, refresh the page once and try again."
            )
        with jobs_lock:
            jobs[job_id] = {"status": "error", "file": None, "title": "", "error": msg}
    except Exception as e:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "file": None, "title": "", "error": f"Unexpected error: {e}"}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    audio_only = bool(data.get("audio_only"))

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_allowed_url(url):
        return jsonify({"error": "Only YouTube and Pinterest links are supported"}), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "pending", "file": None, "title": "", "error": None}

    threading.Thread(
        target=download_video, args=(job_id, url, audio_only), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(
        {
            "status": job["status"],
            "title": job["title"],
            "error": job["error"],
            "download_url": f"/api/file/{job_id}" if job["status"] == "done" else None,
        }
    )


@app.route("/api/file/<job_id>")
def get_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        abort(404)
    path: Path = job["file"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    # In production (Railway/Docker), bind 0.0.0.0 and honor $PORT.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    port = int(os.environ.get("PORT", "8000"))
    app.run(host=host, port=port, debug=False)
