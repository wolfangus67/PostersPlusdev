# PostersPlus

A self-hosted poster generation service that composites extensive metadata onto TMDB posters - ratings, award sashes, quality badges, and title logos - served as ready-to-use JPEGs for AIOMetadata.

Non self hosters can [visit the public instance.](https://postersplus.elfhosted.com)

---

## Showcase
<p align="center">
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase-mode1.png?raw=true" width="23%"/>
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase-mode2.png?raw=true" width="23%"/>
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase-mode3.png?raw=true" width="23%"/>
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase%204.png" width="23%"/>
</p>
<p align="center">
  Client featured is a slightly modified Stremio Kai by allecsc
</p>
<p align="center">
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase-kai.png?raw=true" width="92%"/>
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase-kai2.png" width="92%"/>
  <img src="https://github.com/UmbraProjects/PostersPlus/blob/main/Showcase/showcase-kai3.png?raw=true" width="92%"/>
</p>
</p>

---

## Features

- **Ratings overlay** - weighted composite score from Letterboxd, Trakt, Rotten Tomatoes, IMDb, Metacritic, TMDb, MyAnimeList, and more. Three display modes (Score Bar, Clean, Minimalist), three colour palettes (Prestige, Dark/light, Light), and optional glow on high scores.

- **Award sashes** - Oscar Best Picture, Golden Globe (film and TV, five major categories), Emmy Outstanding Series (Drama, Comedy, Limited), festival winners, notable studios/directors/cast, trending titles, newly streaming (release-date recency plus r/movieleaks tracking), cult classics, true stories, and Metacritic Must-See. Priority order is fully configurable and any sash can be disabled. Optional Badge Style render with adjustable X/Y position.

- **Quality badges** - five display modes: Quality Notch (vertical tier-coloured accent pill), Quality + Age Rating (age numeral tinted by 4K/Remux/HDR tier), Badge Row (PNG icons for 4K, 1080p, Remux, Web, DV, HDR10+, HDR10), Age Rating Only, or hidden. Sourced from either an AIOStreams integration or any Stremio stream addon (Torrentio, Comet, etc.) and fetched in the background on first request.

- **Title logos** - TMDB logos composited over the poster with configurable size and position. Language preference falls back through requested → content's original language → language-neutral → English → Metahub CDN. When no logo exists in the preferred language you can choose between showing the native-language logo or rendering the translated title as text. Optional Textless toggle skips the logo entirely for clients that render the title separately.

- **Art fallback chain** - when a title has no textless poster on TMDB the landscape backdrop is centre-cropped to portrait; when no poster art exists at all, an atmospheric genre background (a starfield for Sci-Fi, blood drips for Horror, a dusty sunset for Western, …) is used with the title text and a genre mascot. Backgrounds live in `static/genre_bg/` — regenerate them with `python genre_backgrounds.py`, or drop in your own 500×750 PNG per genre to override. Preview the full set in the configurator (Logo section → **Preview fallback art**) or at `/debug/fallback-gallery`. 

- **Web configurator** - browser-based UI to tune every parameter and generate a ready-to-paste URL template. Per-section info modals, URL import (paste any /poster URL to hydrate every control), persistent settings via localStorage, and a mobile-optimised expanded preview.

- **Composite poster cache** - fully rendered posters are cached by config hash and served directly on repeat requests, with configurable TTL and max-entry cap.

- **Operator overrides** - drop a discovery_overrides.json into the cache volume to replace or merge the built-in notable-studio / director / cast lists without editing source.

---

## Self Hosted Requirements

- Docker
- A free [TMDB API key](https://www.themoviedb.org/settings/api) for posters, logos and metadata.
- A free [MDBList API key](https://mdblist.com/) for ratings and keywords.
- An [AIOMetadata](https://github.com/cedya77/aiometadata) config. Self hosted or public instance are both fine.
- A quality source for quality badges — choose **one** of:
  - An [AIOStreams](https://github.com/Viren070/AIOStreams) self hosted instance (set `AIOSTREAMS_URL` + `AIOSTREAMS_AUTH`), **or**
  - Any standalone Stremio stream addon such as [Torrentio](https://torrentio.strem.fun) or [Comet](https://comet.elfhosted.com) (set `QUALITY_SOURCE=scraper` + `SCRAPER_URL` to the addon's base URL, e.g. `https://torrentio.strem.fun/`). Note: Stremthru Torz requires authentication and won't work standalone — use it via AIOStreams instead. Quality badges are optional — both sources can be left unconfigured.

---

## Quick Start

> **HTTPS or AIOMetadata's proxy opton is required for production use.**
> If going HTTPS route ensure the access_key env is set to protect your instance
> Good reverse proxy choices are [Traefik](https://traefik.io/) which has great support from Viren's templates or [Caddy](https://caddyserver.com/) which is very simple. 
> If going for AIOMetadata's proxy you don't expose PostersPlus to the internet. Use http://postersplus:8000 in the URL instead of a domain to have them communicate via Docker's internal network. The proxy route is slightly slower but maximizes security.

### Using the pre-built image (recommended)

Pre-built images for `amd64` and `arm64` are published to the GitHub Container Registry on every release.

Create a `compose.yaml` with the following content, substituting your own values:

```yaml
services:
  postersplus:
    image: ghcr.io/umbraprojects/postersplus:latest
    ports:
      - "8000:8000"    # change the left side if port 8000 is already in use
    restart: unless-stopped
    volumes:
      - ./postersplus-cache:/app/cache
    environment:
      - TMDB_API_KEY=your_tmdb_key
      - MDBLIST_API_KEY=your_mdblist_key
      - WORKERS=2
      - ACCESS_KEY=youraccesskey # Highly suggested if exposing to the internet.*
      # See .env.example for all available options
```

Then start it:

```bash
docker compose up -d
```

Once your reverse proxy is set up, open the configurator at your public HTTPS domain to tune your settings and generate a URL template for AIOMetadata. The URL it generates is based on the domain you access it from.

### Building from source

```bash
git clone https://github.com/UmbraProjects/PostersPlus.git
cd PostersPlus
cp .env.example .env   # fill in your keys
docker compose up -d --build
```

---

## Configuration

All configuration is done via environment variables. Copy `.env.example` to `.env` and fill in your values. Every variable is optional - API keys can be omitted from the server and passed per-request as URL parameters instead.

| Variable | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | - | TMDB API key for poster/metadata fetching |
| `MDBLIST_API_KEY` | - | MDBList API key for ratings and award data |
| `MDBLIST_API_KEY_2` | - | Optional second MDBList key. Automatically rotated to when the primary key is rate-limited |
| `ACCESS_KEY` | - | Shared secret for request authentication. Leave blank to allow open access |
| `WORKERS` | `2` | Number of Uvicorn worker processes |
| `AIOSTREAMS_URL` | - | Base URL of your AIOStreams instance (used when `QUALITY_SOURCE=aiostreams`) |
| `AIOSTREAMS_AUTH` | - | AIOStreams credentials as Base64 `user:password` |
| `QUALITY_SOURCE` | `aiostreams` | Quality data source: `aiostreams` or `scraper`. Set to `scraper` to use any Stremio stream addon instead of AIOStreams |
| `SCRAPER_URL` | - | Base URL of a Stremio stream addon (e.g. `https://torrentio.strem.fun/`). Only used when `QUALITY_SOURCE=scraper`. Standalone addons like Torrentio and Comet work best; Stremthru Torz requires auth and should be used via AIOStreams instead |
| `QUALITY_OLD_CACHE_DURATION` | `90` | Days to cache quality data for titles older than 2 weeks |
| `QUALITY_BG_CONCURRENCY` | `5` | Max concurrent background quality fetches |
| `CDN_CACHE_TTL` | `0` | Adds `Cache-Control: public, max-age=N` to poster responses. Set to `0` to disable |
| `JPEG_QUALITY` | `85` | JPEG output quality for composited posters (70–95). Raise to `92` for higher fidelity; lower to reduce file size |
| `COMPOSITE_CACHE_TTL` | `604800` | Seconds to keep a rendered poster before re-rendering (default 7 days) |
| `COMPOSITE_MAX_ENTRIES` | `0` | Cap on composite cache entries. `0` = no cap |
| `DISABLE_COMPOSITE_CACHE` | - | Set to `true` to skip composite cache reads and writes entirely. Every request re-renders from scratch. For development only |
| `LOGO_CONTRAST_RESCUE` | `false` | Recolour a flat logo (white/black/accent) when it blends into the poster background. Multi-colour/outline logos are never touched. Experimental — off by default while tested; set `true` to enable |
| `LOGO_STRETCH_DISABLED` | `true` | Fill-stretch is off by default — every logo is kept at its true clamped size. Set `false` to enable the stretch below |
| `LOGO_STRETCH_FACTOR` | `1.2` | When stretching is enabled, a slim logo is enlarged toward its size cap by up to this factor (one axis only). `1.0` = no enlargement |
| `DEBUG_LOGO_SIZING` | `false` | Log per-logo sizing telemetry at INFO level. For tuning only |
| `TEXTLESS_TEXT_DETECTION` | `true` | Detect burned-in title text on posters TMDB mislabelled as "textless" and skip our own logo so the title isn't doubled. Set `false` to opt out |
| `TEXTLESS_DETECTION_MAX_VOTES` | `300` | Only run text detection on titles with at most this many TMDB votes (mislabels concentrate in the long tail; popular titles are trusted) |
| `TEXTLESS_MIN_BOXES` | `128` | Min EAST text activations (at the 320×640 reference, auto-scaled to the active resolution) before a poster is treated as having burned-in text. Higher = stricter. Changing it auto-invalidates cached detection results + composites |
| `TEXTLESS_DETECTION_CONCURRENCY` | `1` | Max text scans allowed into the worker pool at once. Keeps the pool free for compositing during a burst |
| `EAST_INPUT_WIDTH` / `EAST_INPUT_HEIGHT` | `192` / `384` | EAST scan input resolution (each a multiple of 32). Smaller = faster; `min_boxes` auto-scales so its meaning is preserved |
| `TEXTLESS_SCAN_TOP` | `0.08` | Fraction of poster height skipped from the top before counting text (covers top/middle/bottom titles; ignores top-edge logos) |
| `BAKE_EAST_MODEL` | `true` | Build-time only. Bake the ~96MB EAST model into the image. Set `false` for a leaner image that downloads it once at runtime |

> The ~96 MB EAST model is baked into the image by default. Set `BAKE_EAST_MODEL=false` (build arg / `.env`) for a leaner image that downloads it once into the cache volume on first use instead.
| `DEFAULT_LOGO_LANGUAGE` | `en` | ISO 639-1 language code for title logos |

---

## URL Structure

Posters are served at `/poster` with parameters controlling every aspect of rendering:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&imdb_id={imdb_id}&type={type}
```

Append `&debug=1` to any poster URL to receive a JSON response with all computed metadata — score, genre, sash label, quality tokens, award data, matched cast/directors — instead of rendering the image. Useful for diagnosing unexpected sashes or missing ratings.

Append `&nocache=1` (requires `ACCESS_KEY` to be set and valid) to force a fresh render of a single title, bypassing the composite cache read and re-caching the result. Lets you refresh one poster without flushing the whole cache.

### Operator endpoints

These are gated behind `access_key` when one is configured:

- `GET /stats` — cache row counts / sizes plus live runtime state (in-flight renders, background fetches, MDBList key cooldowns). Handy for spotting issues before they surface.
- `GET /debug/fallback-gallery` — a gallery of every genre's no-art fallback card (mascot + genre font), also reachable via the **Preview fallback art** button in the configurator's Logo section.

---

## Award Sashes

Sashes display contextual metadata about a title - awards, festival recognition, notable cast or crew, and more. The first matching sash in the priority list is shown.

| Sash | Triggers on |
|---|---|
| Best Picture / Emmy Win | Oscar Best Picture winner, Emmy Outstanding Drama/Comedy/Limited winner |
| Golden Globe Win | Golden Globe winner (film drama/comedy, TV drama/comedy/limited) |
| Festival Winner | Cannes, Venice, Sundance, TIFF, and other major festivals |
| Best Picture / Emmy Nom | Oscar Best Picture nominee, Emmy Outstanding nominee |
| Golden Globe Nom | Golden Globe nominee (same categories as above) |
| Notable Studio | A24, Neon, Pixar, and other curated studios |
| Notable Director | Curated list of notable directors |
| Notable Cast | Curated list of notable cast members |
| Trending | Currently in TMDB's trending top 40 |
| Cult Classic | Curated list of cult classics |
| Foreign Language | Non-English language title |
| Newly Streaming | Recently added to streaming |
| Metacritic Must-See | High Metacritic score |
| True Story | Based on a true story |
| Short / Mini / Binge | Short film, miniseries, or bingeable series |

Sash priority order is configurable in the web configurator via drag-and-drop. Individual sashes can be disabled entirely with the ✕ button - disabled sashes are serialised as `-slot_name` in the URL (e.g. `&sash_priority=wins,cast,-trending`).

### Customising Directors, Studios, and Cast

**Source editors** can modify the lists directly in `discovery.py`.

**Docker operators** can override them without editing source by placing a JSON file at `/app/cache/discovery_overrides.json` inside the cache volume. See `discovery_overrides.example.json` for the format.

---

## Ratings

Scores from multiple providers are normalised to a 0–100 scale and combined using configurable weights. Default weights use Letterboxd with Trakt fallback for movies, and Trakt (80%) and Rotten Tomatoes (20%) for TV. Weights are fully adjustable in the web configurator.

---

## Caching

PostersPlus uses a SQLite database (WAL mode) for all caching. The cache volume is mounted at `/app/cache` and persists across container restarts.

| Cache | Default TTL |
|---|---|
| TMDB posters | 60 days |
| TMDB logos | 60 days |
| TMDB metadata | 7 days |
| Ratings (new titles) | 1 day |
| Ratings (older titles) | 14 days |
| Quality badges (new) | 1 day |
| Quality badges (older) | 90 days |
| Composite posters | 7 days |

---

## Donate & Discord

If you'd like to support development, I'd appreciate it: https://ko-fi.com/umbraprojects
Join the discord here to request features or report bugs: https://discord.com/invite/wEgTPNXUMU

---

## License

This project and any associated forks should remain open source under the [GNU Affero General Public License v3.0](LICENSE)
