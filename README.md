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

Run locally to get started or try it out—**Docker is optional**. Once dependencies
and configuration are set up, **`npm start` is all you need to launch the app**.

You'll need Node.js 22+, Python 3.14+, and FFmpeg, on the same LAN as your phone,
with Music Assistant and its Sendspin Source provider available.

1. Clone this repository, then prepare your configuration:

   ```sh
   cp config.example.json config.json
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

3. Install dependencies once, then start:

   ```sh
    npm ci --ignore-scripts
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    npm start
   ```

4. Open **http://localhost:8790**, name a receiver, and enable it.

5. Open YouTube Music, tap **Cast**, select your receiver, and play.

For manual routing, select **Single source** in the UI, then play its input from
**Browse → Sendspin Source** on your chosen MA player or group.

In per-speaker mode, pause stops MA's output while keeping the YouTube session;
MA may show the speaker as idle. Resume restarts the input at the remembered
position. The seeking preference below determines whether seeking interrupts the
output or keeps it running. Restarting can still take time to buffer.
Single-source/manual mode leaves MA's output controls alone.

Each enabled receiver keeps a YouTube resolver warm to reduce new-track startup
time. Initialization is reused, but sender credentials are cleared after each
request; every track still uses the current sender's authorization.

The selected track is fetched ahead automatically into a session-local
encoded-audio memory cache, without re-encoding or additional quality loss.
Playback starts after one small chunk, not the full download; seeks prioritize
missing ranges. The limit is 32 MiB per receiver. Unknown-size, oversized, or
unsupported range responses fall back to direct streaming during initialization.
Nothing is saved to disk, shared between sessions, or fetched for the next track.

### Seeking preference

The web UI's **Stop Sendspin output while seeking** switch chooses the tradeoff:

- **Off:** deliver new-position audio much faster without stopping MA output,
  but seeking can feel more sluggish because old audio remains queued in MA or
  speaker buffers.
- **On:** stop the output before seeking, interrupting old audio, then restart
  it at the new position.

Pause and Stop keep their existing behavior. The switch applies to per-speaker
routing only and takes effect on the **next seek without restarting casting**.
UI choices persist in `.state/receivers.json`, including with Docker's read-only
`config.json` mount. Set `"stop_receiver_on_seek": false` (or `true`) in
`config.json` to seed the initial preference; an existing saved UI choice takes
precedence. Older configurations retain their original stop-on-seek behavior
unless a preference was set.

See the [seek evaluation](docs/seek-buffer-evaluation.md) for the measured
first-PCM timings and listening observations, and the
[latency notes](docs/playback-latency-notes.md) for the removed next-track lookahead
experiment. Faster PCM delivery is not a promise of an instant audible seek.

## Docker (optional)

For a Linux homelab server, **[`compose.yaml`](compose.yaml)** pulls a prebuilt
GHCR image instead. Use the same `config.json` and `.ma_token` from above; no
local Node.js, Python, or FFmpeg installation is needed.

Copy **[`.env.example`](.env.example)** to `.env`, set
`IMAGE=ghcr.io/YOUR_GITHUB_USER/cast2sendspin:latest` using your lowercase GitHub
name, and match `PUID`/`PGID` to the files' owner. Stop any locally running
instance before starting Docker.

```sh
cp .env.example .env
# Edit .env, then:
docker compose up -d
```

### Updates

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

After the local setup above, run the tests with:

```sh
npm test
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
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
