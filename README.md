# PostersPlus

A self-hosted poster generation service that composites extensive metadata onto TMDB posters - ratings, award sashes, quality badges, and title logos - served as ready-to-use JPEGs for AIOMetadata.

Non self hosters can [visit the public instance.](https://postersplus.elfhosted.com)

---

## Showcase

<!-- Add poster screenshots below. Recommended: 3-4 per row, use GitHub-hosted images -->
<!-- Example row format:
<p align="center">
  <img src="url-to-image-1" width="23%"/>
  <img src="url-to-image-2" width="23%"/>
  <img src="url-to-image-3" width="23%"/>
  <img src="url-to-image-4" width="23%"/>
</p>
-->

---

## Features

- **Ratings overlay** - weighted composite score from Letterboxd, Trakt, Rotten Tomatoes, IMDb, Metacritic, TMDb, MyAnimeList, and more. Three display modes: accent bar, numeric, and minimalist.
- **Award sashes** - Oscar Best Picture, Golden Globe (film and TV, five major categories), Emmy Outstanding Series (drama, comedy, limited), festival winners, custom cast, trending, newly streaming and more. Priority order is fully configurable.
- **Quality badges** - 4K, 1080p, Remux, Web, DV, HDR10+, HDR10 sourced from an AIOStreams integration.
- **Title logos** - TMDB logos composited over the poster with configurable size and position. Language-aware with English fallback.
- **Web configurator** - easy to use browser-based UI to tune every parameter and generate a ready-to-paste URL template.
- **Composite poster cache** - fully rendered posters are cached by config hash and served directly on repeat requests.

---

## Requirements

- Docker
- A free [TMDB API key](https://www.themoviedb.org/settings/api) for posters, logos and metadata
- A free [MDBList API key](https://mdblist.com/) for ratings and keywords.
- An [AIOMetadata](https://github.com/cedya77/aiometadata) config. Self hosted or public instance are both fine.
- An [AIOStreams](https://github.com/Viren070/AIOStreams) self hosted instance *(optional, for quality badges)*

---

## Quick Start

> **HTTPS is required for production use.**
> Stremio addons like AIOMetadata are served over HTTPS. Browsers enforce mixed content blocking, meaning any poster URLs referenced by an HTTPS addon must also be HTTPS - HTTP image URLs will be silently blocked, including in Stremio's web client. You will need a reverse proxy with a valid SSL certificate in front of PostersPlus before using it with Stremio.
>
> Common options are [Traefik](https://traefik.io/) (widely used in the self-hosting community, particularly alongside AIOStreams and Authelia), [Caddy](https://caddyserver.com/) (handles Let's Encrypt automatically with minimal config), and Nginx with Certbot. Cloudflare proxying works too and pairs naturally with the `CDN_CACHE_TTL` option.
>
> `http://localhost:8000` is only suitable for accessing the configurator locally during setup.

**1. Clone the repository**
```bash
git clone https://github.com/UmbraProjects/PostersPlus.git
cd PostersPlus
```

**2. Configure your environment**
```bash
cp .env.example .env
```
Edit `.env` and add at minimum your `TMDB_API_KEY` and `MDBLIST_API_KEY`. See the comments in `.env.example` for all available options.

**3. Start the service**
```bash
docker compose up -d
```

The service will be available at `http://localhost:8000`. Set up your reverse proxy to expose it over HTTPS before generating your poster URL.

**4. Open the configurator**

Navigate to `http://localhost:8000/configurator` to tune your settings and generate a URL template.

---

## Configuration

All configuration is done via environment variables. Copy `.env.example` to `.env` and fill in your values. Every variable is optional - API keys can be omitted from the server and passed per-request as URL parameters instead.

| Variable | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | - | TMDB API key for poster/metadata fetching |
| `MDBLIST_API_KEY` | - | MDBList API key for ratings and award data |
| `ACCESS_KEY` | - | Shared secret for request authentication. Leave blank to allow open access |
| `WORKERS` | `2` | Number of Uvicorn worker processes |
| `AIOSTREAMS_URL` | - | Base URL of your AIOStreams instance |
| `AIOSTREAMS_AUTH` | - | AIOStreams credentials as Base64 `user:password` |
| `QUALITY_OLD_CACHE_DURATION` | `90` | Days to cache quality data for titles older than 2 weeks |
| `QUALITY_BG_CONCURRENCY` | `5` | Max concurrent background quality fetches |
| `CDN_CACHE_TTL` | `0` | Adds `Cache-Control: public, max-age=N` to poster responses. Set to `0` to disable |
| `COMPOSITE_CACHE_TTL` | `604800` | Seconds to keep a rendered poster before re-rendering (default 7 days) |
| `COMPOSITE_MAX_ENTRIES` | `0` | Cap on composite cache entries. `0` = no cap |
| `DEFAULT_LOGO_LANGUAGE` | `en` | ISO 639-1 language code for title logos |

---

## URL Structure

Posters are served at `/poster` with parameters controlling every aspect of rendering:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&imdb_id={imdb_id}&type={type}
```

The web configurator at `/configurator` generates the full URL for you. For use with AIOMetadata, click the **Copy URL button** which uses `{tmdb_id}`, `{imdb_id}`, and `{type}` placeholders, don't use the direct image link.

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

Scores from multiple providers are normalised to a 0–100 scale and combined using configurable weights. Default weights favour Letterboxd (80%) and Rotten Tomatoes (20%) for movies, and Trakt (80%) and Rotten Tomatoes (20%) for TV. Weights are fully adjustable in the web configurator.

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

## License

[GNU Affero General Public License v3.0](LICENSE)
