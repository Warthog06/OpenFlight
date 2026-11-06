# OpenFlight

A free, self-hosted flight radar in the style of Flightradar24. It uses OpenStreetMap, needs no API keys or accounts, and costs nothing to run.

- **Live map, updated every second.** Between updates, aircraft are moved smoothly using their speed, track and climb rate.
- **3D view with altitudes.** Tilt the map or press **3D**. Each aircraft is drawn at its real altitude, with a line down to the ground and its flight level shown. The altitude scale can be exaggerated up to ×12 so climbs and descents are easy to see.
- **Pick any date and time.** Choose a moment, press **Go**, and play it back at 1× to 60×, with pause and ±1/±5 minute steps. Replay links can be shared, because the time is stored in the URL.
- **Colour by altitude** (same scale as the legend), trails, callsign labels, worldwide search by callsign, registration or ICAO hex, a details panel with the route when known, and a "follow" mode.
- **Works on phones.**

The only dependency is Python 3.8+ (standard library). MapLibre GL and deck.gl ship in `public/vendor/`, so there is no `npm install` and no build step.

## Run it

```bash
python3 server.py
```

Then open http://localhost:8080.

To try it with simulated traffic (no internet needed, and every date works):

```bash
python3 server.py --demo
```

## Deploy to Vercel (free)

Import the repo into Vercel with the default settings. The website comes from `public/`, and each file in `api/` becomes a small serverless function that runs the same code as `server.py`. No build step is needed.

Optional environment variables (Project → Settings → Environment Variables):

| Variable | Effect |
|---|---|
| `OPENFLIGHT_DEMO=1` | simulated traffic instead of real flights |
| `OPENFLIGHT_PROVIDERS=adsb.fi,airplanes.live` | choose live sources and their fallback order |
| `OPENFLIGHT_TILES=https://…/{z}/{x}/{y}.png` | a different map tile server |
| `OPENFLIGHT_NO_SAMPLES=1` | turn off 1st-of-month history |

**What's different on Vercel:** serverless functions can't run in the background or keep files, so the hosted site **does not record**. Live view and 3D work the same. Replay only works for the 1st day of each month (ADS-B Exchange samples), or for any time in demo mode. For full recording and replay, run `python3 server.py` on a machine that stays on, such as a home PC, a Raspberry Pi or a free VM.

## Where the data comes from

| What | Source | Cost / key |
|---|---|---|
| Map | [OpenStreetMap](https://www.openstreetmap.org) raster tiles | free, no key |
| Live aircraft | [adsb.lol](https://adsb.lol), then [airplanes.live](https://airplanes.live), then [adsb.fi](https://adsb.fi) as fallbacks. [OpenSky](https://opensky-network.org) is optional. | free, no key |
| Routes (origin → destination) | adsb.lol route lookup | free, no key |
| History | your own local recording (see below) plus [ADS-B Exchange sample data](https://www.adsbexchange.com/products/historical-data/) for the 1st of each month | free |

All requests go through the small Python server. This keeps API calls to a minimum: one upstream request per second, shared by every open browser tab that is looking at roughly the same area.

### How history works (and its limits)

No free service offers worldwide flight history for any date, so OpenFlight builds its own:

1. **Everything you watch live is recorded** into `openflight.db` (SQLite), with a position for each aircraft every 5 seconds by default. Any time and place you've viewed can be replayed later.
2. **To record an area even when no browser is open**, run:
   ```bash
   python3 server.py --record 51.47,-0.45,150
   ```
   This records 150 nm around London Heathrow around the clock. `--record` can be given more than once.
3. **On the 1st day of every month**, the whole world is available from ADS-B Exchange's free sample files. This is used automatically when nothing was recorded locally. It works best at slower playback speeds, since each 5-second step is a separate download.

Recordings older than 7 days are deleted automatically (`--retention-days`). As a rough guide, a busy 150 nm area at the default 5-second interval uses a few hundred MB per day. Use `--record-interval 10` or higher to cut that down. Replay interpolates between stored points, so playback stays smooth.

If you pick a time with no data, the app tells you.

## Options

```text
python3 server.py --help

  --port 8080                  web port
  --host 127.0.0.1             use 0.0.0.0 to open it to your network
  --demo                       simulated traffic
  --provider NAME              adsb.lol | airplanes.live | adsb.fi | opensky | demo (repeat for fallbacks)
  --record LAT,LON,NM          keep recording an area in the background (repeatable)
  --record-interval 5          seconds between stored positions per aircraft
  --retention-days 7           how long to keep recordings
  --db openflight.db           where recordings live
  --no-samples                 don't use ADS-B Exchange 1st-of-month samples
  --tiles URL                  another raster tile server, e.g. a self-hosted one
```

## Using the app

| | |
|---|---|
| Click an aircraft | details, route, full trail |
| **3D** button, `3` key, or right-drag / Ctrl-drag | tilt into 3D |
| Layers button | trails, labels, aircraft on the ground, altitude exaggeration |
| Date box + **Go** | jump to a date and time (your local time) |
| ❚❚ / ▶ or Space | pause and resume. Pausing while live freezes the current picture. |
| −5m −1m +1m +5m | step through time |
| **● Live** | return to now |
| Search box | filters aircraft on screen. Press Enter to search worldwide (live mode). |

Each query covers up to 250 nm from the map centre, which is the limit of the free APIs. If you zoom out further, a dashed circle shows the area being covered.

## Fair use

These services are free because volunteers run them. Please be kind to them:

- **OpenStreetMap tiles**: fine for personal use. For heavier use, point `--tiles` at another provider or your own tile server ([tile usage policy](https://operations.osmfoundation.org/policies/tiles/)).
- **adsb.lol / airplanes.live / adsb.fi**: free for non-commercial use with rate limits of about one request per second, which OpenFlight respects. Check each site's terms before using it commercially.
- **ADS-B Exchange samples**: check their terms before any commercial use.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests cover parsing, interpolation, recording and replay. A fake upstream server stands in for the live APIs.

```
server.py            HTTP server, data providers, recorder, replay
api/*.py             Vercel serverless entry points (thin wrappers around server.py)
vercel.json          Vercel settings
public/index.html    page layout
public/app.js        map, 3D rendering, time controls
public/style.css     styles
public/vendor/       MapLibre GL 4.7.1 (BSD-3) and deck.gl 9.1.15 (MIT), unmodified
```
