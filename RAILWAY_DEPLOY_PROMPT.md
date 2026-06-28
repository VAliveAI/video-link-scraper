# Deploy "Video Link Scraper" to Railway

I'm attaching a small Flask app that downloads YouTube, Pinterest, and
Instagram videos via `yt-dlp`. I want you to deploy it to **Railway** as a
public web service.

## What's in the project

- `app.py` — Flask backend with three routes: `GET /` (HTML), `POST /api/download`
  (start a job), `GET /api/status/<id>` (poll), `GET /api/file/<id>` (download).
- `templates/index.html` — single-page UI with a URL input, a Video/Audio mp3
  toggle, status indicator, and download link.
- `requirements.txt` — `flask`, `yt-dlp[default]`, `gunicorn`.
- `Dockerfile` — installs Python 3.12, Node.js 20, ffmpeg, and **bgutil-pot**
  (a Node helper that mints YouTube Proof-of-Origin tokens — without this,
  most YouTube videos return 403 / "format not available"). Then installs the
  bgutil-pot yt-dlp plugin and starts gunicorn.
- `railway.json` — tells Railway to use the Dockerfile and sets the start command.
- `.dockerignore` — excludes `.venv`, `__pycache__`, etc.

`app.py` already honors `$PORT` and binds `0.0.0.0` when `$PORT` is set, and it
looks for the bgutil-pot script at `$BGUTIL_POT_HOME/server/build/generate_once.js`
(the Dockerfile sets `BGUTIL_POT_HOME=/opt/bgutil-pot`).

## What to do

1. **Initialize a git repo** in the project root and make an initial commit.
2. **Create a new Railway project** using the Railway CLI (`railway init`).
   - Service name: `video-link-scraper`
   - Don't add a database — this app uses in-memory job state and the
     container's ephemeral filesystem.
3. **Deploy** with `railway up`. Railway will detect the Dockerfile and build.
4. **Expose the service publicly**: run `railway domain` to generate a
   `*.up.railway.app` URL.
5. **Verify the deployment**:
   - `curl -I https://<your-domain>/` should return `HTTP/2 200`.
   - Open the URL in a browser, paste a YouTube link like
     `https://www.youtube.com/watch?v=dQw4w9WgXcQ`, and confirm the download
     works in both Video and Audio modes.
6. **Report back** with: the public URL, the Railway project URL, and the
   results of the test downloads (file size + content-type from
   `curl -I` on the `/api/file/<id>` URL would be ideal).

## Things to know / watch for

- **First build will take 3–5 minutes** because it installs Node.js, clones
  bgutil-pot, runs `npm ci`, and compiles TypeScript. This is normal.
- **YouTube blocks aggressively.** If a specific video fails, try a different
  one before assuming the deploy is broken. The Rick Astley video above is a
  reliable test target.
- **Memory**: Set the Railway service to at least **512 MB RAM** (1 GB safer)
  — bgutil-pot's Node process plus ffmpeg can spike during merges.
- **Storage**: Downloads land in `/tmp/video_scraper_downloads/` inside the
  container and disappear on redeploy. That's fine for this use case (the
  user grabs the file immediately after it's ready). Don't add a volume.
- **Concurrency**: The Dockerfile runs gunicorn with `-w 1 --threads 8` on
  purpose — the in-memory `jobs` dict isn't shared across workers, so a
  single worker with threaded request handling keeps things consistent.
- **Instagram needs a logged-in cookies file.** Public IG reels/posts return
  empty media without a session. To enable Instagram support on Railway:
  1. In a desktop browser, log into Instagram.
  2. Export cookies to Netscape format using the "Get cookies.txt LOCALLY"
     extension (Chrome) or "cookies.txt" extension (Firefox) — visit
     instagram.com and click the extension to download `cookies.txt`.
  3. In the Railway dashboard, go to the service → Variables → New Variable →
     **File** type. Name it `ig_cookies.txt`, paste the file contents. Railway
     mounts it at `/etc/secrets/ig_cookies.txt` (or similar).
  4. Add a regular env var `IG_COOKIES_FILE` pointing to that path.

  Without this, YouTube and Pinterest will still work fine; only Instagram
  will error with "Instagram sent an empty media response".
- **Other env vars are optional.** Don't set any unless something fails and
  you need to debug.

## Done criteria

- Public Railway URL responds 200 on `/`.
- At least one YouTube video successfully downloads end-to-end via the UI.
- The audio-only toggle produces an `.mp3` file.
- You've shared the final URL with me.
