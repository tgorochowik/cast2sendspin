<p align="center">
  <img src="src/web/icon.svg" width="96" height="96" alt="">
</p>

# Cast2Sendspin

**YouTube Music → Music Assistant → your speakers.**

Cast from the official YouTube Music app using the casting person's account,
including Premium-quality audio. No shared Google login or browser cookies.

- Expose individual Music Assistant speakers as named receivers.
- Or use one receiver and choose the player or group in Music Assistant.
- Control playback, seeking, and volume from YouTube Music.
- Keep YouTube's queue and autoplay, without replacing your MA queue.
- Manage receivers in a small local web UI.

## Quickstart

Use a Linux Docker host on the same LAN as your phone, with Music Assistant
and its Sendspin Source provider available.
The **[`compose.yaml`](compose.yaml)** in this repository pulls your GHCR image.
Set its image address in `.env`, using **[`.env.example`](.env.example)** as a template.

1. Clone this repository, then prepare your configuration:

   ```sh
   cp config.example.json config.json
   cp .env.example .env
   mkdir -p .state
   chmod 700 .state
   touch .ma_token
   chmod 600 .ma_token
   ```

2. Edit these files:
   - **`config.json`**: set your MA URL, Sendspin URL, and host LAN interface
     (`ip -brief address` lists interfaces).
   - **`.ma_token`**: paste an MA access token with player-control and
     provider/player-configuration permissions.
   - **`.env`**: set `IMAGE=ghcr.io/YOUR_GITHUB_USER/cast2sendspin:latest`
     using your lowercase GitHub name. Match `PUID`/`PGID` to the files' owner.

3. Start:

   ```sh
   docker compose up -d
   ```

4. Open **http://localhost:8790**, name a receiver, and enable it.

5. Open YouTube Music, tap **Cast**, select your receiver, and play.

For manual routing, select **Single source** in the UI, then play its input from
**Browse → Sendspin Source** on your chosen MA player or group.

## Updates

```sh
docker compose pull
docker compose up -d
```

Keep `.state/`, `config.json`, and `.ma_token` when upgrading.
The management UI stays loopback-only; keep receiver ports on your trusted LAN.

## Build & publish

Push this repository to GitHub. The included Actions workflow tests the app and
publishes **amd64 + arm64** images to `ghcr.io/<owner>/<repo>` on `main` and
`v*` tags. Set the GHCR package visibility to **Public** to pull without login.
Use that image path in `.env` if you choose a different repository name.

To build locally:

```sh
docker build -t cast2sendspin:local .
IMAGE=cast2sendspin:local docker compose up -d
```

## Development

Node.js 22+, Python 3.14+, and FFmpeg:

```sh
npm ci --ignore-scripts
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm test
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
npm start
```

### Repository layout

```text
src/            Backend code (Node.js and Python)
src/web/        Management UI and icon
tests/          Node.js and Python tests
.github/        CI and multi-architecture GHCR publishing
compose.yaml    Homelab deployment using a GHCR image
Dockerfile      Local and CI container builds
```

Anonymous yt-dlp fallback is off by default. Enable it explicitly with
`"allow_anonymous_fallback": true` in `config.json`.

[Apache 2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

Not affiliated with Google, YouTube, or Music Assistant.
