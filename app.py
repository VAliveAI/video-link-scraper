from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

APP_VERSION = "2026.08.28-jsruntime"

# yt-dlp now needs a JavaScript runtime to solve YouTube's nsig/player
# challenges. Its built-in default is Deno, which we don't ship; Node is in the
# image (and via nvm locally), so resolve it once at import and hand yt-dlp an
# explicit path. Without a runtime, formats silently degrade and YouTube's
# bot-check trips far more often. Resolved lazily-ish here so it's shared.
NODE_PATH = shutil.which("node")

app = Flask(__name__)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "video_scraper_downloads"
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # pragma: no cover - startup must never die on this
    pass

# Cap simultaneous downloads. Each job can spawn ffmpeg (merge + possible
# transcode), so unbounded parallelism from a big batch paste would thrash a
# small container. Requests beyond the cap queue up as "queued" jobs.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
_download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

# Jobs older than this are swept (files deleted, entry dropped).
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(6 * 60 * 60)))

MAX_BATCH_URLS = 25

# job_id -> dict (see _new_job)
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# URL matching
# ---------------------------------------------------------------------------

SITE_PATTERNS = {
    "youtube": r"(youtube\.com|youtu\.be|youtube-nocookie\.com)",
    "pinterest": r"(pinterest\.[a-z.]+|pin\.it)",
    "instagram": r"(instagram\.com|instagr\.am|ddinstagram\.com)",
    "twitter": r"(twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|t\.co)",
}

SITE_RES = {
    name: re.compile(rf"^(https?://)?([\w-]+\.)*{pattern}(/|$)", re.IGNORECASE)
    for name, pattern in SITE_PATTERNS.items()
}


def site_for_url(url: str) -> Optional[str]:
    url = url.strip()
    for name, rx in SITE_RES.items():
        if rx.match(url):
            return name
    return None


def is_allowed_url(url: str) -> bool:
    return site_for_url(url) is not None


def extract_urls(blob: str) -> list[str]:
    """Pull every http(s) URL out of a pasted blob.

    Handles newline-separated lists, space-separated, and links embedded in
    share text like "Check this out https://x.com/... via @someone".
    """
    found = re.findall(r"https?://[^\s<>\"']+", blob or "")
    cleaned = []
    seen = set()
    for u in found:
        u = u.rstrip(".,);]!")
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    return cleaned


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

# Sites that generally require a logged-in session. Each maps to the env var
# prefix used to supply cookies in a deployed environment.
COOKIE_SITES = {"instagram": "IG", "twitter": "X", "youtube": "YT"}

# Cookie name that proves an actual logged-in session for each site.
SESSION_COOKIE = {"instagram": "sessionid", "twitter": "auth_token", "youtube": "SID"}

CHROMIUM_ROOTS = {
    "chrome": "~/Library/Application Support/Google/Chrome",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
    "edge": "~/Library/Application Support/Microsoft Edge",
    "chromium": "~/Library/Application Support/Chromium",
}


def _cookies_from_env(site: str) -> Optional[str]:
    """Materialize cookies supplied via env var into a file yt-dlp can read.

    Base64 is preferred because Netscape cookie files are tab-separated and
    multi-line, which most env-var UIs mangle.
    """
    prefix = COOKIE_SITES.get(site)
    if not prefix:
        return None

    for var, is_b64 in ((f"{prefix}_COOKIES_B64", True), (f"{prefix}_COOKIES_TXT", False)):
        raw = os.environ.get(var)
        if not raw:
            continue
        try:
            text = base64.b64decode(raw).decode("utf-8") if is_b64 else raw
        except Exception:
            continue
        if SESSION_COOKIE.get(site, "") not in text:
            # Present but not actually a logged-in export — keep looking.
            continue
        path = DOWNLOAD_DIR / f"{site}_cookies.txt"
        path.write_text(text)
        return str(path)

    file_var = os.environ.get(f"{prefix}_COOKIES_FILE")
    if file_var and Path(file_var).exists():
        return file_var
    return None


def _browser_with_session(site: str) -> Optional[tuple]:
    """Find a local browser profile holding a real logged-in session for `site`.

    Only useful in local dev — a deployed container has no browsers. Returns a
    tuple shaped for yt-dlp's `cookiesfrombrowser` option, or None.
    """
    import sqlite3  # local-dev only; kept lazy so slim images still boot

    cookie_name = SESSION_COOKIE.get(site)
    host_like = {"instagram": "%instagram%", "twitter": "%twitter%"}.get(site)
    if not cookie_name or not host_like:
        return None

    for browser, root in CHROMIUM_ROOTS.items():
        root_path = Path(os.path.expanduser(root))
        if not root_path.exists():
            continue
        for profile_dir in sorted(root_path.iterdir()):
            if not profile_dir.is_dir():
                continue
            if profile_dir.name != "Default" and not profile_dir.name.startswith("Profile "):
                continue
            cookies_db = profile_dir / "Cookies"
            if not cookies_db.exists():
                continue
            tmp_db = Path(tempfile.gettempdir()) / f"cookie_probe_{os.getpid()}_{threading.get_ident()}.db"
            try:
                shutil.copy(cookies_db, tmp_db)
                with sqlite3.connect(tmp_db) as conn:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ? AND name = ?",
                        (host_like, cookie_name),
                    )
                    if cur.fetchone()[0] > 0:
                        return (browser, profile_dir.name)
            except Exception:
                continue
            finally:
                tmp_db.unlink(missing_ok=True)

    # Safari stores cookies in a binary format we can't probe with sqlite, so
    # ask yt-dlp to export them and look for the session cookie in the result.
    safari_db = Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    if safari_db.exists():
        probe = Path(tempfile.gettempdir()) / f"safari_probe_{os.getpid()}_{threading.get_ident()}.txt"
        try:
            subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--cookies-from-browser", "safari",
                 "--cookies", str(probe), "--skip-download", "https://example.com/"],
                capture_output=True, timeout=20,
            )
            if probe.exists() and cookie_name in probe.read_text(errors="ignore"):
                return ("safari",)
        except Exception:
            pass
        finally:
            probe.unlink(missing_ok=True)
    return None


def apply_cookies(ydl_opts: dict, site: str) -> None:
    """Attach cookies to yt-dlp options if this site needs (and has) them."""
    if site not in COOKIE_SITES:
        return

    from_env = _cookies_from_env(site)
    if from_env:
        ydl_opts["cookiefile"] = from_env
        return

    if site == "youtube":
        # Strictly opt-in via YT_COOKIES_*. Never auto-detect from a browser:
        # unsolicited cookies push YouTube down a more restrictive path that
        # yields "format not available" even when it would otherwise work.
        return

    from_browser = _browser_with_session(site)
    if from_browser:
        ydl_opts["cookiesfrombrowser"] = from_browser


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------

def _probe_codec(path: Path, stream: str) -> str:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip().lower().splitlines()[0] if out.stdout.strip() else ""
    except Exception:
        return ""


def ensure_quicktime_compatible(path: Path) -> Path:
    """Transcode to H.264 + AAC when needed so the file plays in QuickTime/Photos.

    Instagram and YouTube often serve VP9/AV1/Opus, which play fine in
    Chrome/Safari/VLC but not in QuickTime or the iOS Photos app.
    """
    video_codec = _probe_codec(path, "v:0")
    audio_codec = _probe_codec(path, "a:0")

    if video_codec in ("h264", "avc1") and audio_codec in ("aac", "mp4a", ""):
        return path

    out_path = path.with_name(path.stem + "__qt.mp4")
    audio_flag = ["-c:a", "copy"] if audio_codec in ("aac", "mp4a") else ["-c:a", "aac", "-b:a", "192k"]
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", *audio_flag, "-movflags", "+faststart",
             str(out_path)],
            check=True, capture_output=True, timeout=900,
        )
    except Exception:
        out_path.unlink(missing_ok=True)
        return path

    if out_path.exists() and out_path.stat().st_size > 0:
        final = path.with_suffix(".mp4")
        path.unlink(missing_ok=True)
        out_path.replace(final)
        return final
    return path


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _new_job(url: str, audio_only: bool) -> dict:
    return {
        "status": "queued",       # queued | downloading | processing | done | error
        "progress": 0,
        "file": None,
        "title": "",
        "error": None,
        "url": url,
        "audio_only": audio_only,
        "site": site_for_url(url),
        "created": time.time(),
    }


def _update(job_id: str, **fields) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def _public_job(job_id: str, job: dict) -> dict:
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "title": job["title"],
        "error": job["error"],
        "url": job["url"],
        "site": job["site"],
        "audio_only": job["audio_only"],
        "filename": job["file"].name if job.get("file") else None,
        "download_url": f"/api/file/{job_id}" if job["status"] == "done" else None,
    }


def sweep_old_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if j["created"] < cutoff]
        for jid in stale:
            jobs.pop(jid, None)
    for jid in stale:
        shutil.rmtree(DOWNLOAD_DIR / jid, ignore_errors=True)


def download_video(job_id: str, url: str, audio_only: bool = False) -> None:
    site = site_for_url(url)
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    with _download_slots:
        _update(job_id, status="downloading")

        def progress_hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total:
                    _update(job_id, progress=min(99, int(done * 100 / total)))
            elif d.get("status") == "finished":
                _update(job_id, progress=99, status="processing")

        # bgutil-pot mints the Proof-of-Origin tokens YouTube now requires.
        pot_script = next(
            (p for p in (
                Path(os.environ.get("BGUTIL_POT_HOME", "/opt/bgutil-pot")) / "server/build/generate_once.js",
                Path.home() / "bgutil-pot/server/build/generate_once.js",
            ) if p.exists()),
            None,
        )

        ydl_opts = {
            "outtmpl": str(job_dir / "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "retries": 20,
            "fragment_retries": 20,
            "extractor_retries": 3,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [progress_hook],
            "extractor_args": {},
        }

        # Give yt-dlp a real JS runtime for nsig/player-challenge solving.
        if NODE_PATH:
            ydl_opts["js_runtimes"] = {"node": {"path": NODE_PATH}}

        if pot_script:
            ydl_opts["extractor_args"]["youtubepot-bgutilscript"] = {"script_path": [str(pot_script)]}

        if audio_only:
            ydl_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            # Prefer H.264+AAC so no transcode is needed; fall back to best.
            ydl_opts.update({
                "format": (
                    "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
                    "bv*[vcodec^=avc1]+ba/b[vcodec^=avc1]/bv*+ba/b/best"
                ),
                "merge_output_format": "mp4",
            })

        # Cookies are not universally good. Instagram/X need them; YouTube is
        # often *broken* by them (a session captured mid-rotation comes back as
        # "The page needs to be reloaded"), yet needs them when the server's IP
        # is bot-blocked. So try the order most likely to work, then fall back.
        if site == "youtube":
            attempts = [False, True]      # clean first, cookies only if blocked
        elif site in COOKIE_SITES:
            attempts = [True, False]      # auth first, clean as a long shot
        else:
            attempts = [False]

        last_error = None

        for use_cookies in attempts:
            opts = dict(ydl_opts)
            opts.pop("cookiefile", None)
            opts.pop("cookiesfrombrowser", None)
            if use_cookies:
                apply_cookies(opts, site)
                if "cookiefile" not in opts and "cookiesfrombrowser" not in opts:
                    continue  # nothing to add; retrying would be identical

            # Clear partials so a failed attempt can't be mistaken for output.
            for leftover in job_dir.glob("*"):
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)

            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if "entries" in info:
                        info = info["entries"][0]

                    filename = Path(ydl.prepare_filename(info))
                    # Post-processing rewrites extensions, so resolve what's on disk.
                    preferred_ext = ".mp3" if audio_only else ".mp4"
                    if not filename.exists() or filename.suffix != preferred_ext:
                        vid_id = info.get("id", "")
                        matches = [p for p in job_dir.glob(f"*{vid_id}*") if p.is_file()]
                        preferred = [p for p in matches if p.suffix == preferred_ext]
                        if preferred:
                            filename = preferred[0]
                        elif matches:
                            filename = max(matches, key=lambda p: p.stat().st_size)

                    if not audio_only and filename.exists():
                        _update(job_id, status="processing", progress=99)
                        filename = ensure_quicktime_compatible(filename)

                    _update(
                        job_id,
                        status="done",
                        progress=100,
                        file=filename,
                        title=info.get("title") or filename.stem,
                    )
                    return
            except DownloadError as e:
                last_error = str(e)
                if not _is_auth_error(last_error):
                    break
                _update(job_id, progress=0)
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                break

        _update(job_id, status="error", error=_friendly_error(last_error or "Download failed", site))


def _is_auth_error(msg: str) -> bool:
    """True when a failure might be fixed by changing the cookie strategy."""
    needles = (
        "not a bot", "page needs to be reloaded", "login required", "Sign in",
        "empty media response", "Requested format is not available",
        "cookies", "private", "age",
    )
    low = msg.lower()
    return any(n.lower() in low for n in needles)


def _friendly_error(msg: str, site: Optional[str]) -> str:
    if "Instagram sent an empty media response" in msg or "login required" in msg.lower():
        if site == "instagram":
            return ("Instagram needs a logged-in session. The saved cookies may have "
                    "expired — refresh them and try again.")
        if site == "twitter":
            return ("X/Twitter needs a logged-in session for this post. Add X cookies "
                    "and try again.")
    if "confirm you" in msg and "not a bot" in msg:
        return ("YouTube is blocking this server's IP. Add YouTube cookies "
                "(YT_COOKIES_B64) to get past the bot check.")
    if "page needs to be reloaded" in msg.lower():
        return ("YouTube rejected the saved cookies (the browser session rotated "
                "since they were exported). Re-export them from a private window "
                "and update YT_COOKIES_B64.")
    if "No video could be found" in msg:
        return "No video found at that link — it may be an image-only post."
    if "Private" in msg or "not available" in msg:
        return "That post is private or unavailable."
    # Strip yt-dlp's noisy suffixes for anything else.
    return msg.split("; please report")[0].split(". Check if")[0].strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


def _start_job(url: str, audio_only: bool) -> str:
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = _new_job(url, audio_only)
    threading.Thread(target=download_video, args=(job_id, url, audio_only), daemon=True).start()
    return job_id


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    audio_only = bool(data.get("audio_only"))

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not is_allowed_url(url):
        return jsonify({"error": "Only YouTube, Pinterest, Instagram, and X links are supported"}), 400

    sweep_old_jobs()
    return jsonify({"job_id": _start_job(url, audio_only)})


@app.route("/api/batch", methods=["POST"])
def start_batch():
    """Accept a pasted blob or a list of URLs and start one job per link."""
    data = request.get_json(silent=True) or {}
    audio_only = bool(data.get("audio_only"))

    raw = data.get("urls")
    if isinstance(raw, list):
        candidates = [str(u).strip() for u in raw if str(u).strip()]
    else:
        candidates = extract_urls(data.get("text") or data.get("url") or "")

    if not candidates:
        return jsonify({"error": "No links found in what you pasted"}), 400
    if len(candidates) > MAX_BATCH_URLS:
        return jsonify({"error": f"Too many links — max {MAX_BATCH_URLS} at once"}), 400

    sweep_old_jobs()

    started, rejected = [], []
    for url in candidates:
        if is_allowed_url(url):
            started.append({"url": url, "job_id": _start_job(url, audio_only)})
        else:
            rejected.append({"url": url, "error": "Unsupported site"})

    if not started:
        return jsonify({"error": "None of those links are from a supported site", "rejected": rejected}), 400
    return jsonify({"jobs": started, "rejected": rejected})


@app.route("/api/status")
def status_many():
    """Poll several jobs at once — one request per tick instead of N."""
    ids = [i for i in (request.args.get("ids") or "").split(",") if i]
    if not ids:
        return jsonify({"error": "No ids provided"}), 400
    out = {}
    with jobs_lock:
        for jid in ids:
            job = jobs.get(jid)
            if job:
                out[jid] = _public_job(jid, job)
    return jsonify({"jobs": out})


@app.route("/api/status/<job_id>")
def status_one(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(_public_job(job_id, job))


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


@app.route("/api/diag")
def diag():
    """Temporary diagnostics: what the container sees for YouTube extraction.

    Reports the JS runtime / PO-token wiring and, if ?url= is given, runs an
    info-only extraction with cookies off then on, returning the raw yt-dlp
    error for each so we can see the true failure behind the friendly message.
    """
    if os.environ.get("DIAG_TOKEN") and request.args.get("token") != os.environ["DIAG_TOKEN"]:
        abort(404)

    pot_script = next(
        (str(p) for p in (
            Path(os.environ.get("BGUTIL_POT_HOME", "/opt/bgutil-pot")) / "server/build/generate_once.js",
            Path.home() / "bgutil-pot/server/build/generate_once.js",
        ) if p.exists()),
        None,
    )
    info = {
        "version": APP_VERSION,
        "node_path": NODE_PATH,
        "pot_script": pot_script,
        "yt_cookies_b64_present": bool(os.environ.get("YT_COOKIES_B64")),
        "yt_cookies_txt_present": bool(os.environ.get("YT_COOKIES_TXT")),
        "yt_cookie_has_SID": None,
    }
    cookiefile = _cookies_from_env("youtube")
    info["yt_cookiefile_resolved"] = bool(cookiefile)
    raw = os.environ.get("YT_COOKIES_B64")
    if raw:
        try:
            info["yt_cookie_has_SID"] = "SID" in base64.b64decode(raw).decode("utf-8", "ignore")
        except Exception:
            info["yt_cookie_has_SID"] = "decode-failed"

    url = request.args.get("url")
    if url and site_for_url(url) == "youtube":
        base = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "extractor_args": {},
        }
        if NODE_PATH:
            base["js_runtimes"] = {"node": {"path": NODE_PATH}}
        if pot_script:
            base["extractor_args"]["youtubepot-bgutilscript"] = {"script_path": [pot_script]}

        # Try a range of player clients (clean, no cookies) to see which, if
        # any, gets past the datacenter bot gate. The winner gets baked in.
        clients_param = request.args.get("clients")
        client_sets = (
            [c.strip() for c in clients_param.split(",")] if clients_param
            else ["default", "tv", "tv_embedded", "mweb", "web_safari", "ios", "android_vr"]
        )
        results = {}
        for client in client_sets:
            opts = dict(base)
            opts["extractor_args"] = dict(base["extractor_args"])
            opts["extractor_args"]["youtube"] = {"player_client": [client]}
            try:
                with YoutubeDL(opts) as ydl:
                    i = ydl.extract_info(url, download=False)
                results[f"clean:{client}"] = f"OK: {i.get('title')} ({len(i.get('formats') or [])} formats)"
            except Exception as e:
                results[f"clean:{client}"] = f"{type(e).__name__}: {str(e)[:220]}"
        # And the stored cookies, default client.
        opts = dict(base)
        apply_cookies(opts, "youtube")
        if "cookiefile" in opts:
            try:
                with YoutubeDL(opts) as ydl:
                    i = ydl.extract_info(url, download=False)
                results["cookies:default"] = f"OK: {i.get('title')} ({len(i.get('formats') or [])} formats)"
            except Exception as e:
                results["cookies:default"] = f"{type(e).__name__}: {str(e)[:220]}"
        info["extraction"] = results

    return jsonify(info)


@app.route("/api/health")
def health():
    with jobs_lock:
        active = sum(1 for j in jobs.values() if j["status"] in ("queued", "downloading", "processing"))
        total = len(jobs)
    return jsonify({"ok": True, "version": APP_VERSION, "jobs": total, "active": active})


if __name__ == "__main__":
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    port = int(os.environ.get("PORT", "8000"))
    app.run(host=host, port=port, debug=False, threaded=True)
