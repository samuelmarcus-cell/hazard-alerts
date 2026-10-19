# Hazard alerts

Checks public warning feeds every 15 minutes and keeps a Discord server up to
date, using one webhook on the **#alerts** channel.

| Where | What |
|---|---|
| **#alerts** | A card for each major event (buzzes your phone) |
| **📊 Dashboard** thread | One live message, edited every check: everything current, fire danger outlook, feed status, and a world + Australia map |
| **📒 Event log** thread | A quiet line for everything recorded, including lower-level events with no card |
| **🗓️ Weekly round-up** thread | Monday 8am (Melbourne): the week's events, counts by source, feed status |
| **📰 Hazard news** thread | ABC stories about storms, floods, fires, cyclones, heatwaves etc., posted silently with a preview |

## What gets a card

| Source | Card | Recorded only |
|---|---|---|
| BoM warnings, every state | Tropical cyclone, major flood, extreme heatwave | Severe thunderstorm, severe weather, moderate flood, fire weather, severe heatwave |
| NSW RFS, VicEmergency, QLD Fire, SA CFS, WA DFES, TasALERT, ACT ESA | Emergency Warning | Watch and Act |
| GDACS (global) | Cyclones, floods, wildfires at Orange/Red; others at Red | Green cyclones/floods/wildfires; Orange quakes, volcanoes, tsunamis |
| Canada (CWFIS) and US (NIFC) fires | 50,000 ha+, again at 250,000 ha+ | 10,000 ha+ |
| ABC News "Just In" | Deployment / disaster-declaration stories mentioning a hazard | Other hazard stories → Hazard news thread |
| Fire danger forecasts (NSW, VIC) | Any district forecast Extreme or Catastrophic | — |
| Concurrent events | Major events in 3+ Australian jurisdictions at once; or 2+ while a Red / 250,000 ha+ event is active overseas | — |
| Feed health | A feed failing for 24 hours, and its recovery | — |

NT has no machine-readable warnings feed; its cyclones and floods come through BoM.

## How alerts behave

- **New major event:** one card in #alerts, with a map.
- **Escalation** (e.g. Watch and Act → Emergency Warning, Orange → Red): the
  original card is updated with a history line and a short "Escalated" card
  links to it. If there was no card yet (it was recorded only), a full card is posted.
- **Event ends:** its card turns grey with "No longer listed".
- **Busy days:** at most 5 cards per check; the rest are summarised in one card.
- **New sources or changed filters:** whatever is already active is recorded
  quietly as `seen_at_start`, so changes never flood you.

## Event log

`events_log.csv` in the repo gets a row for every change:

`time_utc, change, source, key, level, title, lat, lon, link, major`

`change` is `seen_at_start`, `new`, `escalated` or `ended`; `major` is `yes`
if the event got a card.

## Files

- `monitor.py`: the program (settings at the top under CONFIGURATION)
- `test_monitor.py`: offline tests (`python -m unittest test_monitor.py`)
- `.github/workflows/monitor.yml`: runs every 15 minutes, commits state and log
- `state.json`: what's been seen, and the Discord message for each event
- `events_log.csv`: the event log

## Tuning

In `monitor.py` under **CONFIGURATION**:

- `BOM_CARD_PHRASES` / `BOM_LOG_PHRASES`: which BoM warnings get cards vs are recorded.
- `GDACS_WEATHER_TYPES`: which GDACS types card at Orange.
- `LOG_FIRE_HA`, `LARGE_FIRE_HA`, `HUGE_FIRE_HA`: North American fire thresholds.
- `FDR_RANK`: fire danger levels that get a card.
- `DEPLOYMENT_PHRASES` / `HAZARD_WORDS`: what counts as deployment news.
- `HAZARD_NEWS_PATTERNS` / `HAZARD_NEWS_EXCLUDE`: what counts as hazard news.
- `MAX_PUSHES_PER_RUN`, `FEED_FAILURE_WARNING_HOURS`, `WEEKLY_WEEKDAY`, `WEEKLY_HOUR`.
- Changing a filter? Bump `FILTERS_VERSION` so existing events are recorded quietly.

## Notes

- Threads archive themselves after a quiet spell. The monitor re-posts the
  dashboard when that happens, which re-opens the thread.
- GitHub pauses schedules after 60 days without commits; the weekly round-up
  commit prevents that.
- GDACS scores humanitarian impact, so remote fires stay Green; the CWFIS/NIFC
  size rules cover that gap for North America.
- Maps © OpenStreetMap contributors. Data: Bureau of Meteorology, NSW RFS,
  Emergency Management Victoria, QFD, SA CFS, DFES, TasALERT, ACT ESA, GDACS
  (EC-JRC/UN), NRCan CWFIS, NIFC, ABC. Personal, non-commercial use.
