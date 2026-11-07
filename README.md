# OpenFlight

A free, self-hosted flight radar in the style of Flightradar24. It uses OpenStreetMap, needs no API keys or accounts, and costs nothing to run.

- **Live map, updated every second.** Between updates, aircraft are moved smoothly using their speed, track and climb rate.
- **3D view with altitudes.** Tilt the map or press **3D**. Each aircraft is drawn as a small 3D airliner at its real altitude, with a line down to the ground and its flight level shown. The model points along the aircraft's track, tilts its nose with the climb or descent, and banks into turns. The altitude scale can be exaggerated up to ×12 so climbs and descents are easy to see; the nose tilt follows the same exaggeration.
- **Pick any date and time.** Choose a moment, press **Go**, and play it back at 1× to 60×, with pause and ±1/±5 minute steps. Replay links can be shared, because the time is stored in the URL.
- **Colour by altitude** (same scale as the legend), trails, callsign labels, worldwide search by callsign, registration or ICAO hex, a details panel with the route when known, and a "follow" mode.
- **Works on phones.**

The only dependency is Python 3.8+ (standard library). MapLibre GL and deck.gl ship in `public/vendor/`, so there is no `npm install` and no build step.

## Run it

```bash
python3 openflight.py
```

Then open http://localhost:8080.

To try it with simulated traffic (no internet needed, and every date works):

```bash
python3 openflight.py --demo
```

## Deploy to Vercel (free)

Import the repo into Vercel. The website comes from `public/`, and each file in `api/` becomes a small serverless function that runs the same code as `openflight.py`. No build step is needed. `vercel.json` sets everything else; in the project settings just make sure **Root Directory** is the repo root (left empty), not `public`.

Optional environment variables (Project → Settings → Environment Variables):

| Variable | Effect |
|---|---|
| `OPENFLIGHT_DEMO=1` | simulated traffic instead of real flights |
| `OPENFLIGHT_PROVIDERS=adsb.fi,airplanes.live` | choose live sources and their fallback order |
| `OPENFLIGHT_TILES=https://…/{z}/{x}/{y}.png` | a different map tile server |
| `OPENFLIGHT_NO_ARCHIVE=1` | turn off replay from the adsb.lol archive |
| `OPENFLIGHT_NO_SAMPLES=1` | turn off 1st-of-month history |

**What's different on Vercel:** serverless functions can't run in the background or keep files, so the hosted site doesn't record anything itself. Replay still works for any time in roughly the last 45 days, worldwide, from the adsb.lol archive (see below), up to the most recent finished half hour. Only your own recording, by running `python3 openflight.py`, can replay the last few minutes.

## Where the data comes from

| What | Source | Cost / key |
|---|---|---|
| Map | [OpenStreetMap](https://www.openstreetmap.org) raster tiles | free, no key |
| Live aircraft | [adsb.lol](https://adsb.lol), then [airplanes.live](https://airplanes.live), then [adsb.fi](https://adsb.fi) as fallbacks. [OpenSky](https://opensky-network.org) is optional. | free, no key |
| Routes (origin → destination) | adsb.lol route lookup | free, no key |
| History | [adsb.lol's public replay archive](https://adsb.lol) (last ~45 days, worldwide), your own local recording, and [ADS-B Exchange sample data](https://www.adsbexchange.com/products/historical-data/) for the 1st of each month | free, no key |

All requests go through the small Python server. This keeps API calls to a minimum: one upstream request per second, shared by every open browser tab that is looking at roughly the same area.

### How history works (and its limits)

When you pick a time, OpenFlight looks for flights in this order:

1. **Your own recording** (local server only, details below).
2. **The adsb.lol archive:** worldwide, any time from about 45 days ago up to the last finished half hour. adsb.lol saves every aircraft it tracked into one file per half hour, which is what its own map's replay feature uses. OpenFlight downloads the half hour you're viewing (about 12 MB), keeps it in memory and decodes your area from it. The first request for each half hour takes a few seconds to load; after that it's instant. The archive has no aircraft types or registrations, and headings and climb rates are worked out from consecutive positions.
3. **ADS-B Exchange samples**, for the 1st day of any month further back.

Recording on your own machine adds what the archive can't: the last few minutes, and keeping data for longer than 45 days.

1. **Everything you watch live is recorded** into `openflight.db` (SQLite), with a position for each aircraft every 5 seconds by default. Any time and place you've viewed can be replayed later.
2. **To record an area even when no browser is open**, run:
   ```bash
   python3 openflight.py --record 51.47,-0.45,150
   ```
   This records 150 nm around London Heathrow around the clock. `--record` can be given more than once.

Recordings older than 7 days are deleted automatically (`--retention-days`). As a rough guide, a busy 150 nm area at the default 5-second interval uses a few hundred MB per day. Use `--record-interval 10` or higher to cut that down. Replay interpolates between stored points, so playback stays smooth.

If you pick a time with no data, the app tells you.

## Options

```text
python3 openflight.py --help

  --port 8080                  web port
  --host 127.0.0.1             use 0.0.0.0 to open it to your network
  --demo                       simulated traffic
  --provider NAME              adsb.lol | airplanes.live | adsb.fi | opensky | demo (repeat for fallbacks)
  --record LAT,LON,NM          keep recording an area in the background (repeatable)
  --record-interval 5          seconds between stored positions per aircraft
  --retention-days 7           how long to keep recordings
  --db openflight.db           where recordings live
  --no-archive                 don't use the adsb.lol replay archive
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
- **adsb.lol archive**: open data under the [ODbL](https://opendatacommons.org/licenses/odbl/). Credit adsb.lol if you publish it. OpenFlight keeps the last few half-hour files in memory instead of downloading them again.
- **ADS-B Exchange samples**: check their terms before any commercial use.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests cover parsing, interpolation, recording and replay. A fake upstream server stands in for the live APIs.

```
openflight.py        HTTP server, data providers, recorder, replay
api/*.py             Vercel serverless entry points (thin wrappers around openflight.py)
vercel.json          Vercel settings
public/index.html    page layout
public/app.js        map, 3D rendering, time controls
public/plane-model.js  the low-poly 3D aircraft model, built in code (no model file)
public/style.css     styles
public/vendor/       MapLibre GL 4.7.1 (BSD-3) and deck.gl 9.1.15 (MIT), unmodified
```
