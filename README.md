# Hazard alerts

Checks public warning feeds every 15 minutes and posts a card to Discord
**only for big events**. Each card is coloured by severity, has a map preview,
and links to the official page.

| Source | What gets a card |
|---|---|
| BoM warnings, every state | Tropical cyclone, major flood, extreme heatwave warnings |
| NSW RFS, VicEmergency, QLD Fire, SA CFS, WA DFES, TasALERT, ACT ESA | **Emergency Warning** level only |
| GDACS (global) | Cyclones, floods, wildfires at **Orange/Red**; quakes, volcanoes etc. at **Red**; droughts never |
| Canada (CWFIS) and US (NIFC) fires | Any wildfire past **50,000 ha**, again past **250,000 ha** |
| ABC News "Just In" | Stories mentioning interstate/international crews, the ADF, or disaster declarations **and** a hazard |

NT has no machine-readable warnings feed; its cyclones and floods come through BoM.

## How alerts behave

- **New event:** one card.
- **Escalation** (GDACS Orange → Red, fire 50k → 250k ha): the original card is
  updated in place with a history line, and a short "Escalated" card links back to it.
- **Event ends** (leaves the feed): the original card turns grey with
  "No longer listed". No new message.
- **Busy days:** at most 5 cards per check; the rest are summarised in one card.
- **Broken feeds:** if a feed fails for 24 hours you get a 🛠️ card, and a ✅ card when it recovers.
- **New sources** are read silently the first time, so adding one never floods you
  with everything already active.

## Event log

Every change is appended to `events_log.csv` in the repo:

`time_utc, change, source, key, level, title, lat, lon, link`

`change` is one of `seen_at_start`, `new`, `escalated`, `ended`. Over time this
is a consistent, timestamped record of major events. Download it from GitHub
whenever you want it.

## Files

- `monitor.py`: the program
- `test_monitor.py`: offline tests (`python -m unittest test_monitor.py`)
- `.github/workflows/monitor.yml`: runs it every 15 minutes, commits state and log
- `state.json`: what's been seen, and the Discord message for each event
- `events_log.csv`: the event log

## Setup (already done for this repo)

1. Discord: a server, a channel, and a webhook (channel settings → Integrations → Webhooks).
2. GitHub secret `DISCORD_WEBHOOK_URL` = the webhook URL.
3. Actions → Hazard monitor → Run workflow (tick "Just send a sample card" to test).

## Tuning

Settings are at the top of `monitor.py` under **CONFIGURATION**:

- `BOM_KEEP_PHRASES`: add e.g. `"severe thunderstorm": "⛈️"` (expect daily cards in spring).
- `GDACS_ORANGE_TYPES`: remove `"FL"` so floods need Red.
- `LARGE_FIRE_HA` / `HUGE_FIRE_HA`: the North American fire thresholds.
- `DEPLOYMENT_PHRASES` / `HAZARD_WORDS`: what counts as deployment news.
- `MAX_PUSHES_PER_RUN`, `FEED_FAILURE_WARNING_HOURS`.

## Notes

- GitHub pauses schedules after 60 days without commits. Quiet periods can
  trigger this; re-enable from the Actions tab.
- GDACS scores humanitarian impact, so remote fires stay Green; the CWFIS/NIFC
  size rules cover that gap for North America.
- Maps © OpenStreetMap contributors. Data: Bureau of Meteorology, NSW RFS,
  Emergency Management Victoria, QFD, SA CFS, DFES, TasALERT, ACT ESA, GDACS
  (EC-JRC/UN), NRCan CWFIS, NIFC, ABC. Personal, non-commercial use.
