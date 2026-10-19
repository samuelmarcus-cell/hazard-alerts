"""
Hazard alert monitor
====================

Checks public warning feeds every 15 minutes and posts a Discord card ONLY for
the biggest events:

  Australia, weather   BoM warnings, every state: tropical cyclones, major
                       floods, extreme heatwaves.
  Australia, fire etc. State emergency feeds (NSW, VIC, QLD, SA, WA, TAS, ACT):
                       only items at "Emergency Warning", the top level of the
                       Australian Warning System. (NT has no machine-readable
                       feed; its cyclones and floods come through BoM.)
  International        GDACS: cyclones, floods, wildfires at Orange or Red;
                       anything else at Red; droughts never.
  Large fires          Canada (CWFIS) and the US (NIFC): any wildfire past
                       50,000 ha, again past 250,000 ha.
  Deployments          ABC "Just In" stories that mention interstate or
                       international crews, the ADF, or disaster declarations
                       together with a hazard.

Each run:
  1. FETCH + FILTER every source
  2. DECIDE what changed since last run (state.json):
       new event         -> post a card
       higher level      -> update the original card + post a short update
       gone from feed    -> grey out the original card ("no longer listed")
  3. LOG every change to events_log.csv (a research record of major events)
  4. HEALTH: if a feed has been failing for 24 hours, post a warning card,
     and another when it recovers
  5. SAVE state.json

Run locally:  python monitor.py --dry-run        (prints, sends nothing)
              python monitor.py --test-message   (posts one sample card)
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

try:                                   # Pillow draws the map previews. If it's
    from PIL import Image, ImageDraw   # missing, cards simply go without a map.
except ImportError:
    Image = None

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Australia/Melbourne")
except Exception:                      # no time-zone database: fall back to UTC
    LOCAL_TZ = timezone.utc


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USERNAME = "Hazard Monitor"

COLOUR_RED = 0xD32F2F       # Emergency Warning, GDACS Red, 250,000 ha+ fires
COLOUR_ORANGE = 0xF57C00    # BoM top-tier warnings, GDACS Orange, 50,000 ha+ fires
COLOUR_BLUE = 0x1E88E5      # deployment news
COLOUR_GREY = 0x607D8B      # finished events, summaries
COLOUR_GREEN = 0x43A047     # feed recovered, test card

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "events_log.csv"

MAX_PUSHES_PER_RUN = 5          # new/escalated cards per run; extras are summarised
FEED_FAILURE_WARNING_HOURS = 24  # warn once a feed has failed this long

LARGE_FIRE_HA = 50_000
HUGE_FIRE_HA = 250_000
ACRES_PER_HA = 2.47105

# Identify ourselves; BoM rejects Python's default, and OpenStreetMap's tile
# policy asks for an identifying User-Agent.
HEADERS = {"User-Agent": "hazard-alerts/2.0 (personal, non-commercial; "
                         "+https://github.com/samuelmarcus-cell/hazard-alerts)"}

# --- BoM: one warnings RSS feed per state ---------------------------------
BOM_FEEDS = {
    "NSW/ACT": "http://www.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml",
    "NT":      "http://www.bom.gov.au/fwo/IDZ00055.warnings_nt.xml",
    "QLD":     "http://www.bom.gov.au/fwo/IDZ00056.warnings_qld.xml",
    "SA":      "http://www.bom.gov.au/fwo/IDZ00057.warnings_sa.xml",
    "TAS":     "http://www.bom.gov.au/fwo/IDZ00058.warnings_tas.xml",
    "VIC":     "http://www.bom.gov.au/fwo/IDZ00059.warnings_vic.xml",
    "WA":      "http://www.bom.gov.au/fwo/IDZ00060.warnings_wa.xml",
}
BOM_KEEP_PHRASES = {               # phrase in title -> emoji on the card
    "tropical cyclone": "🌀",
    "major flood": "🌊",           # also catches "Moderate to Major Flood Warning"
    "extreme heatwave": "🌡️",
}
BOM_SKIP_PHRASES = ["cancellation", "final flood warning"]

# --- State emergency feeds in GeoJSON -------------------------------------
# level_field: the property holding the warning level; None = search all.
STATE_FEEDS = [
    {"name": "NSW RFS", "level_field": "category",
     "url": "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"},
    {"name": "VicEmergency", "level_field": None,
     "url": "https://emergency.vic.gov.au/public/events-geojson.json"},
    {"name": "QLD Fire", "level_field": None,
     "url": "https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com"
            "/content/Feeds/BushfireCurrentIncidents/bushfireAlert.json"},
]
STATE_KEEP_LEVEL = "emergency warning"

# --- State emergency feeds in CAP (Common Alerting Protocol) XML ----------
# id_param: what identifies one incident across re-issued warnings:
#   "identifier" = the CAP message ID (ACT keeps it per incident)
#   any other name = a CAP <parameter> with that name (WA's CAD_ID)
#   None = the event type plus the area description
CAP_FEEDS = [
    {"name": "SA CFS", "id_param": None,
     "url": "https://data.eso.sa.gov.au/feeds/prod/cap-au.xml"},
    {"name": "WA DFES", "id_param": "CAD_ID",
     "url": "https://api.emergency.wa.gov.au/v1/capau"},
    {"name": "TasALERT", "id_param": None,
     "url": "https://alert.tas.gov.au/data/cap-au.xml"},
    {"name": "ACT ESA", "id_param": "identifier",
     "url": "https://data.esa.act.gov.au/feeds/esa-cap-incidents.xml"},
]

# --- GDACS: global disasters ----------------------------------------------
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
GDACS_NS = {"gdacs": "http://www.gdacs.org",
            "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
GDACS_TYPES = {                    # code -> (name, emoji, map zoom)
    "TC": ("Tropical cyclone", "🌀", 4),
    "FL": ("Flood", "🌊", 5),
    "WF": ("Wildfire", "🔥", 6),
    "EQ": ("Earthquake", "🌍", 5),
    "VO": ("Volcano", "🌋", 6),
    "TS": ("Tsunami", "🌊", 4),
    "DR": ("Drought", "🏜️", 4),
}
GDACS_ORANGE_TYPES = {"TC", "FL", "WF"}
GDACS_NEVER_TYPES = {"DR"}
LEVEL_RANK = {"green": 0, "orange": 1, "red": 2}

# --- Large North American fires -------------------------------------------
CANADA_URL = "https://geoserver.cwfif.nrcan.gc.ca/geoserver/ows?" + urlencode({
    "service": "WFS", "version": "2.0.0", "request": "GetFeature",
    "outputFormat": "application/json", "srsName": "EPSG:4326",
    "typeName": "public:cwfif_national_activefires",
    "CQL_FILTER": f"record_start<=now() AND record_end>now() AND fire_size>={LARGE_FIRE_HA}",
})
US_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
          "WFIGS_Incident_Locations_Current/FeatureServer/0/query?" + urlencode({
              "where": (f"IncidentTypeCategory='WF' "
                        f"AND IncidentSize>={round(LARGE_FIRE_HA * ACRES_PER_HA)} "
                        f"AND (PercentContained IS NULL OR PercentContained<100)"),
              "outFields": "IncidentName,UniqueFireIdentifier,IncidentSize,"
                           "PercentContained,POOState",
              "f": "geojson",
          }))
CANADA_REGIONS = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador", "NS": "Nova Scotia",
    "NT": "Northwest Territories", "NU": "Nunavut", "ON": "Ontario",
    "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan",
    "YT": "Yukon", "PC": "Parks Canada",
}
CANADA_STAGES = {"OC": "Out of control", "BH": "Being held", "UC": "Under control"}

# --- ABC news: deployments and declarations -------------------------------
ABC_URL = "https://www.abc.net.au/news/feed/10719976/rss.xml"     # "Just In"
DEPLOYMENT_PHRASES = [
    "interstate firefighters", "interstate crews", "interstate fire crews",
    "interstate personnel", "interstate help", "interstate assistance",
    "interstate reinforcements", "interstate strike team",
    "firefighters deployed", "crews deployed", "australian firefighters",
    "firefighters head to", "defence force", "adf personnel", "adf troops",
    "disaster declaration", "state of emergency",
]
HAZARD_WORDS = ["fire", "flood", "cyclone", "storm", "hail", "landslide",
                "earthquake", "tsunami"]


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------

def fetch(url):
    """Download a URL; return bytes, or None on any network/HTTP error."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.content
    except requests.RequestException as error:
        print(f"  ! could not fetch {url[:90]}: {error}")
        return None


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def local_time(moment):
    """'19 Oct 15:07' in Melbourne time, for cards."""
    return moment.astimezone(LOCAL_TZ).strftime("%-d %b %H:%M")


def make_alert(**values):
    """An alert with defaults filled in. Fields every parser can set:

    key, source, rank (bigger = worse), level (readable rank), emoji, title,
    message, link, colour, fields [(label, value)], lat, lon, zoom (map),
    track_end (False = don't grey out / log when it leaves the feed).
    """
    alert = {"rank": 1, "level": "", "emoji": "⚠️", "message": "", "link": "",
             "colour": COLOUR_ORANGE, "fields": [], "lat": None, "lon": None,
             "zoom": 7, "track_end": True}
    alert.update(values)
    return alert


def local_name(element):
    """'{namespace}entry' -> 'entry'. CAP feeds vary in namespaces."""
    return element.tag.rsplit("}", 1)[-1]


def first_text(element, names):
    """Text of the first descendant whose tag is in `names`, else ''."""
    for child in element.iter():
        if local_name(child) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


# ---------------------------------------------------------------------------
# PARSERS — each turns one feed's raw bytes into a list of alerts
# ---------------------------------------------------------------------------

def parse_bom(region, content):
    alerts = []
    for item in ET.fromstring(content).iter("item"):
        title = (item.findtext("title") or "").strip()
        lower = title.lower()
        emoji = next((e for phrase, e in BOM_KEEP_PHRASES.items() if phrase in lower), None)
        if emoji is None or any(p in lower for p in BOM_SKIP_PHRASES):
            continue
        link = (item.findtext("link") or "").strip()
        alerts.append(make_alert(
            key=f"bom:{link or title}", source="Bureau of Meteorology",
            level="Warning", emoji=emoji, title=title, link=link,
            fields=[("Region", region)],
        ))
    return alerts


TITLE_FIELDS = ["title", "sourceTitle", "WarningTitle", "Title", "name", "Location", "location"]
ID_FIELDS = ["guid", "id", "sourceId", "UniqueID", "OBJECTID", "IncidentNo"]
LINK_FIELDS = ["link", "url", "Link", "URL"]
DESCRIPTION_LABELS = ["LOCATION", "COUNCIL AREA", "STATUS", "TYPE", "SIZE"]


def first_present(props, field_names):
    for name in field_names:
        value = props.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def find_point(geometry):
    """(lat, lon) from a GeoJSON geometry, looking inside collections."""
    if not geometry:
        return None, None
    if geometry.get("type") == "Point":
        lon, lat = geometry["coordinates"][:2]      # GeoJSON order is lon, lat
        return lat, lon
    for inner in geometry.get("geometries", []):
        lat, lon = find_point(inner)
        if lat is not None:
            return lat, lon
    return None, None


def parse_description(text):
    """'STATUS: Out of control <br />TYPE: Bush Fire' -> card fields."""
    fields = []
    for part in re.split(r"<br\s*/?>", text or ""):
        label, _, value = part.partition(":")
        label, value = label.strip(), value.strip()
        if label in DESCRIPTION_LABELS and value:
            fields.append((label.title(), value))
    return fields


def parse_state_geojson(feed, content):
    alerts = []
    if not content.strip():
        return alerts
    data = json.loads(content)
    if isinstance(data, list):                       # some feeds are bare lists
        features = [{"properties": r} for r in data if isinstance(r, dict)]
    else:
        features = data.get("features", [])

    for feature in features:
        props = feature.get("properties") or {}
        if feed["level_field"]:
            level_text = str(props.get(feed["level_field"], ""))
        else:
            level_text = " ".join(str(v) for v in props.values())
        if STATE_KEEP_LEVEL not in level_text.lower():
            continue

        title = first_present(props, TITLE_FIELDS) or "Unnamed incident"
        lat, lon = find_point(feature.get("geometry"))
        alerts.append(make_alert(
            key=f"{feed['name']}:{first_present(props, ID_FIELDS) or title}",
            source=feed["name"], level="Emergency Warning", emoji="🔥",
            title=f"EMERGENCY WARNING: {title}",
            message=f"{feed['name']} has an Emergency Warning current.",
            link=first_present(props, LINK_FIELDS), colour=COLOUR_RED,
            fields=parse_description(props.get("description")), lat=lat, lon=lon,
        ))
    return alerts


def cap_parameters(alert_el):
    """CAP <parameter><valueName>X</valueName><value>Y</value> -> {X: Y}."""
    params = {}
    for el in alert_el.iter():
        if local_name(el) == "parameter":
            name = first_text(el, {"valueName"})
            if name:
                params[name] = first_text(el, {"value"})
    return params


def cap_level(alert_el, params):
    """The Australian Warning System level of one CAP alert, or ''.

    Order of trust: an explicit AlertLevel parameter (TAS, ACT), then the
    headline wording (WA: "Bushfire Emergency Warning ..."), then CAP-AU's
    severity mapping (Extreme = Emergency Warning). We deliberately do NOT
    search the description, which often says things like "this may be
    upgraded to an Emergency Warning".
    """
    level = params.get("AlertLevel", "").strip()
    if level and level.lower() not in ("n/a", "not yet known", "none"):
        return level
    headline = first_text(alert_el, {"headline", "title"}).lower()
    for name in ("Emergency Warning", "Watch and Act", "Advice"):
        if name.lower() in headline:
            return name
    if first_text(alert_el, {"severity"}).lower() == "extreme":
        return "Emergency Warning"
    return ""


def parse_cap(feed, content):
    alerts = []
    if not content.strip():
        return alerts
    root = ET.fromstring(content)

    # A CAP feed is a list of <alert>s (often wrapped in EDXL or Atom). Fall
    # back to Atom <entry> or RSS <item> if there are no <alert> elements.
    for tag in ("alert", "entry", "item"):
        items = [e for e in root.iter() if local_name(e) == tag]
        if items:
            break

    for item in items:
        params = cap_parameters(item)
        if cap_level(item, params).lower() != STATE_KEEP_LEVEL:
            continue

        headline = first_text(item, {"headline", "title"}) or "Unnamed warning"
        area = first_text(item, {"areaDesc"})
        event = first_text(item, {"event"})
        id_param = feed.get("id_param")
        if id_param == "identifier":
            event_id = first_text(item, {"identifier", "id"})
        else:
            event_id = params.get(id_param, "") if id_param else ""
        if not event_id and (event or area):
            event_id = f"{event}|{area}"
        if not event_id:
            event_id = first_text(item, {"identifier", "id"}) or headline

        lat = lon = None
        spot = first_text(item, {"circle", "point"})
        match = re.match(r"\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", spot)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))

        link = first_text(item, {"web"})
        if not link:
            link_el = next((e for e in item.iter() if local_name(e) == "link"), None)
            link = link_el.get("href", "") if link_el is not None else ""

        fields = [("Area", area)] if area else []
        if event:
            fields.append(("Type", event))
        alerts.append(make_alert(
            key=f"{feed['name']}:{event_id}", source=feed["name"],
            level="Emergency Warning", emoji="🔥" if "fire" in event.lower() else "⚠️",
            title=f"EMERGENCY WARNING: {headline}",
            message=strip_html(first_text(item, {"description", "summary"}))[:500],
            link=link, colour=COLOUR_RED, fields=fields, lat=lat, lon=lon,
        ))
    return alerts


def parse_gdacs(content):
    alerts = []
    for item in ET.fromstring(content).iter("item"):
        event_type = item.findtext("gdacs:eventtype", namespaces=GDACS_NS) or ""
        level = (item.findtext("gdacs:alertlevel", namespaces=GDACS_NS) or "").lower()
        rank = LEVEL_RANK.get(level, 0)
        if event_type in GDACS_NEVER_TYPES:
            continue
        needed = LEVEL_RANK["orange"] if event_type in GDACS_ORANGE_TYPES else LEVEL_RANK["red"]
        if rank < needed:
            continue

        event_id = item.findtext("gdacs:eventid", namespaces=GDACS_NS) or ""
        country = item.findtext("gdacs:country", namespaces=GDACS_NS) or ""
        severity = (item.findtext("gdacs:severity", namespaces=GDACS_NS) or "").strip()
        kind, emoji, zoom = GDACS_TYPES.get(event_type, (event_type, "⚠️", 5))
        fields = [("Level", level.title()), ("Type", kind)]
        if country:
            fields.append(("Country", country))
        if severity:
            fields.append(("Severity", severity))

        alerts.append(make_alert(
            key=f"gdacs:{event_type}:{event_id}", rank=rank, level=level.title(),
            source="GDACS", emoji=emoji,
            title=f"{kind}{' — ' + country if country else ''}",
            message=(item.findtext("title") or "").strip(),
            link=(item.findtext("link") or "").strip(),
            colour=COLOUR_RED if rank == LEVEL_RANK["red"] else COLOUR_ORANGE,
            fields=fields, zoom=zoom,
            lat=to_float(item.findtext("geo:Point/geo:lat", namespaces=GDACS_NS)),
            lon=to_float(item.findtext("geo:Point/geo:long", namespaces=GDACS_NS)),
        ))
    return alerts


def fire_size_alert(key, source, title, hectares, link, fields, lat, lon):
    """Shared shape for the North American large-fire alerts."""
    rank = 2 if hectares >= HUGE_FIRE_HA else 1
    threshold = HUGE_FIRE_HA if rank == 2 else LARGE_FIRE_HA
    return make_alert(
        key=key, source=source, rank=rank, level=f"{threshold:,}+ ha",
        emoji="🔥", title=title, message=f"{hectares:,.0f} hectares burned.",
        link=link, colour=COLOUR_RED if rank == 2 else COLOUR_ORANGE,
        fields=[("Size", f"{hectares:,.0f} ha")] + fields,
        lat=lat, lon=lon, zoom=6,
    )


def parse_canada(content):
    alerts = []
    for feature in json.loads(content).get("features", []):
        p = feature.get("properties") or {}
        hectares = to_float(p.get("fire_size")) or 0
        if hectares < LARGE_FIRE_HA:
            continue
        region = CANADA_REGIONS.get(p.get("agency_code", ""), p.get("agency_code", ""))
        stage = p.get("stage_of_control_status", "")
        alerts.append(fire_size_alert(
            key=f"ca:{p.get('national_fire_id') or p.get('id')}",
            source="Canada (CWFIS)", title=f"Large wildfire — {region}, Canada",
            hectares=hectares,
            link="https://cwfis.cfs.nrcan.gc.ca/en/interactive-map",
            fields=[("Status", CANADA_STAGES.get(stage, stage or "Unknown")),
                    ("Fire ID", p.get("agency_fire_id", ""))],
            lat=to_float(p.get("latitude")), lon=to_float(p.get("longitude")),
        ))
    return alerts


def parse_us(content):
    alerts = []
    for feature in json.loads(content).get("features", []):
        p = feature.get("properties") or {}
        hectares = (to_float(p.get("IncidentSize")) or 0) / ACRES_PER_HA
        if hectares < LARGE_FIRE_HA:
            continue
        lat, lon = find_point(feature.get("geometry"))
        state = (p.get("POOState") or "").replace("US-", "")
        contained = p.get("PercentContained")
        alerts.append(fire_size_alert(
            key=f"us:{p.get('UniqueFireIdentifier') or p.get('IncidentName')}",
            source="United States (NIFC)",
            title=f"Large wildfire — {p.get('IncidentName', 'Unnamed')}, {state}, USA",
            hectares=hectares, link="https://inciweb.wildfire.gov/",
            fields=[("Contained", f"{contained:.0f}%" if contained is not None else "Unknown")],
            lat=lat, lon=lon,
        ))
    return alerts


def parse_abc(content):
    alerts = []
    for item in ET.fromstring(content).iter("item"):
        title = (item.findtext("title") or "").strip()
        summary = strip_html(item.findtext("description"))
        text = f"{title} {summary}".lower()
        if not any(p in text for p in DEPLOYMENT_PHRASES):
            continue
        if not any(h in text for h in HAZARD_WORDS):
            continue
        link = (item.findtext("link") or "").strip()
        alerts.append(make_alert(
            key=f"abc:{(item.findtext('guid') or link or title).strip()}",
            source="ABC News", level="Deployment news", emoji="📰",
            title=title, message=summary[:400], link=link, colour=COLOUR_BLUE,
            track_end=False,          # stories leave the feed; that isn't an "end"
        ))
    return alerts


# ---------------------------------------------------------------------------
# COLLECTING
# ---------------------------------------------------------------------------

def safe_parse(source, parser, content):
    """Run a parser; None if the download failed or the content is unreadable."""
    if content is None:
        return None
    try:
        alerts = parser(content)
        print(f"  {source}: {len(alerts)} item(s) pass the filter")
        return alerts
    except (ET.ParseError, json.JSONDecodeError, AttributeError,
            KeyError, TypeError, ValueError) as error:
        preview = content[:150].decode("utf-8", errors="replace").replace("\n", " ")
        print(f"  ! could not read {source}: {error}\n    it starts with: {preview!r}")
        return None


def collect():
    """Fetch and filter every source -> {source: alerts, or None if it failed}."""
    jobs = []
    for region, url in BOM_FEEDS.items():
        jobs.append((f"BoM {region}", url, lambda c, r=region: parse_bom(r, c)))
    for feed in STATE_FEEDS:
        jobs.append((feed["name"], feed["url"], lambda c, f=feed: parse_state_geojson(f, c)))
    for feed in CAP_FEEDS:
        jobs.append((feed["name"], feed["url"], lambda c, f=feed: parse_cap(f, c)))
    jobs += [
        ("GDACS", GDACS_URL, parse_gdacs),
        ("Canada fires", CANADA_URL, parse_canada),
        ("US fires", US_URL, parse_us),
        ("ABC News", ABC_URL, parse_abc),
    ]
    return {source: safe_parse(source, parser, fetch(url)) for source, url, parser in jobs}


# ---------------------------------------------------------------------------
# DECIDING WHAT CHANGED  (no network or files here, so it's easy to test)
# ---------------------------------------------------------------------------

def decide(results, old_sources, now):
    """Compare this run's alerts with memory.

    results     {source: [alerts] or None (fetch failed)}
    old_sources {source: {key: record}}   from state.json
    now         datetime of this run

    Returns (changes, new_sources). Each change is (kind, source, alert, record):
      "seen_at_start"  first time we've ever read this source: record, don't post
      "new"            a new event
      "escalated"      a known event at a higher rank
      "ended"          a known event no longer in the feed (alert is None)

    A record remembers: key, rank (the PEAK seen), level, title, first_seen,
    history, lat/lon/link, track_end, and later the Discord message ids.
    """
    changes, new_sources = [], {}
    stamp = local_time(now)

    for source, alerts in results.items():
        if alerts is None:                        # feed failed: change nothing
            new_sources[source] = old_sources.get(source, {})
            continue

        seeding = source not in old_sources       # never read this source before
        previous = old_sources.get(source, {})
        current = {}

        for alert in alerts:
            key = alert["key"]
            record = previous.get(key)
            if record is None:
                record = {
                    "key": key, "rank": alert["rank"], "level": alert["level"],
                    "title": alert["title"], "first_seen": now.isoformat(),
                    "history": [f"{alert['level'] or 'Listed'} · {stamp}"],
                    "lat": alert["lat"], "lon": alert["lon"], "link": alert["link"],
                    "track_end": alert["track_end"],
                }
                changes.append(("seen_at_start" if seeding else "new", source, alert, record))
            elif alert["rank"] > record["rank"]:
                record = dict(record, rank=alert["rank"], level=alert["level"],
                              history=record.get("history", []) + [f"{alert['level']} · {stamp}"])
                changes.append(("escalated", source, alert, record))
            # Same or lower rank: stay quiet, and keep the PEAK rank so a
            # cyclone wobbling Red -> Orange -> Red doesn't notify twice.
            current[key] = record

        for key, record in previous.items():
            if key not in current and record.get("track_end", True):
                changes.append(("ended", source, None, record))
        new_sources[source] = current

    return changes, new_sources


def update_health(results, health, now):
    """Track failing feeds. Returns (cards_to_post, new_health).

    health {source: {"failing_since": iso or None, "warned": bool}}
    """
    cards, new_health = [], {}
    limit = timedelta(hours=FEED_FAILURE_WARNING_HOURS)

    for source, alerts in results.items():
        entry = dict(health.get(source) or {"failing_since": None, "warned": False})
        if alerts is None:
            entry["failing_since"] = entry["failing_since"] or now.isoformat()
            since = datetime.fromisoformat(entry["failing_since"])
            if now - since >= limit and not entry["warned"]:
                entry["warned"] = True
                cards.append(make_alert(
                    key=f"health:{source}", source="Hazard Monitor", emoji="🛠️",
                    title=f"Feed not working: {source}",
                    message=(f"{source} has failed every check since {local_time(since)}. "
                             "Its events won't be reported until it recovers. The "
                             "Actions log ('Check feeds') shows what it's returning."),
                    colour=COLOUR_GREY,
                ))
        else:
            if entry["warned"]:
                cards.append(make_alert(
                    key=f"health:{source}", source="Hazard Monitor", emoji="✅",
                    title=f"Feed working again: {source}", colour=COLOUR_GREEN,
                ))
            entry = {"failing_since": None, "warned": False}
        new_health[source] = entry
    return cards, new_health


# ---------------------------------------------------------------------------
# EVENT LOG (research record)
# ---------------------------------------------------------------------------

LOG_COLUMNS = ["time_utc", "change", "source", "key", "level", "title",
               "lat", "lon", "link"]


def log_changes(changes, now, path=LOG_FILE):
    """Append every change to events_log.csv (header written on first use)."""
    if not changes:
        return
    is_new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if is_new_file:
            writer.writerow(LOG_COLUMNS)
        for kind, source, alert, record in changes:
            writer.writerow([
                now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), kind,
                source, record["key"], record.get("level", ""), record.get("title", ""),
                record.get("lat") if record.get("lat") is not None else "",
                record.get("lon") if record.get("lon") is not None else "",
                record.get("link", ""),
            ])


# ---------------------------------------------------------------------------
# MAP PREVIEWS
# ---------------------------------------------------------------------------

def render_map(lat, lon, zoom, width=600, height=300):
    """PNG bytes of an OpenStreetMap view centred on (lat, lon), or None.

    Map tiles are 256 px squares in "Web Mercator". We work out which pixel
    of the whole-world map the point falls on, download the few tiles around
    it, paste them together, and draw a marker in the middle.
    """
    if Image is None or lat is None or lon is None:
        return None
    tile, n = 256, 2 ** zoom
    x = (lon + 180) / 360 * n * tile
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n * tile
    left, top = x - width / 2, y - height / 2

    canvas = Image.new("RGB", (width, height), (225, 225, 225))
    for tx in range(math.floor(left / tile), math.floor((left + width) / tile) + 1):
        for ty in range(math.floor(top / tile), math.floor((top + height) / tile) + 1):
            if not 0 <= ty < n:
                continue
            data = fetch(f"https://tile.openstreetmap.org/{zoom}/{tx % n}/{ty}.png")
            if data is None:
                return None
            piece = Image.open(io.BytesIO(data)).convert("RGB")
            canvas.paste(piece, (round(tx * tile - left), round(ty * tile - top)))

    draw = ImageDraw.Draw(canvas)
    cx, cy, r = width / 2, height / 2, 9
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(211, 47, 47),
                 outline=(255, 255, 255), width=3)
    credit = "© OpenStreetMap contributors"
    w = draw.textlength(credit)
    draw.rectangle((width - w - 8, height - 16, width, height), fill=(255, 255, 255))
    draw.text((width - w - 4, height - 14), credit, fill=(60, 60, 60))

    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def build_embed(alert, history=None, status=None):
    """One alert -> a Discord embed (the coloured card). Trimmed to Discord's
    limits: title 256 chars, description 2000, field values 1024."""
    embed = {
        "title": f"{alert['emoji']} {alert['title']}"[:256],
        "color": alert["colour"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": alert.get("source", "")},
    }
    if alert.get("message"):
        embed["description"] = alert["message"][:2000]
    if alert.get("link"):
        embed["url"] = alert["link"]

    fields = list(alert.get("fields", []))
    if history and len(history) > 1:
        fields.append(("History", "\n".join(history[-6:])))
    if status:
        fields.append(("Status", status))
    if alert.get("lat") is not None and alert.get("lon") is not None:
        lat, lon = alert["lat"], alert["lon"]
        fields.append(("Map", f"[Open map](https://www.openstreetmap.org/?mlat={lat:.4f}"
                              f"&mlon={lon:.4f}#map=8/{lat:.4f}/{lon:.4f})"))
    embed["fields"] = [{"name": name, "value": str(value)[:1024] or "—",
                        "inline": name not in ("History", "Status")}
                       for name, value in fields]
    return embed


def discord_call(method, url, payload, image=None):
    """Send to Discord, waiting and retrying once if rate-limited (429).
    Returns the response JSON (or {}), or None on failure."""
    for attempt in range(2):
        try:
            if image:
                response = requests.request(
                    method, url, data={"payload_json": json.dumps(payload)},
                    files={"files[0]": ("map.png", image, "image/png")}, timeout=30)
            else:
                response = requests.request(method, url, json=payload, timeout=30)
            if response.status_code == 429 and attempt == 0:
                time.sleep(float(response.json().get("retry_after", 2)))
                continue
            response.raise_for_status()
            return response.json() if response.content else {}
        except (requests.RequestException, ValueError) as error:
            print(f"  ! Discord {method} failed: {error}")
            return None
    return None


def post_card(embed, image=None):
    """Post a new card. Returns (message_id, attachment_id) or (None, None)."""
    if image:
        embed = dict(embed, image={"url": "attachment://map.png"})
    reply = discord_call("POST", f"{DISCORD_WEBHOOK_URL}?wait=true",
                         {"username": DISCORD_USERNAME, "embeds": [embed]}, image)
    if not reply:
        return None, None
    attachments = reply.get("attachments") or []
    return reply.get("id"), (attachments[0]["id"] if attachments else None)


def edit_card(message_id, embed, attachment_id=None):
    """Replace an earlier card's embed, keeping its map image."""
    payload = {"embeds": [embed], "attachments": []}
    if attachment_id:
        payload["embeds"] = [dict(embed, image={"url": "attachment://map.png"})]
        payload["attachments"] = [{"id": attachment_id}]
    return discord_call("PATCH", f"{DISCORD_WEBHOOK_URL}/messages/{message_id}", payload)


_channel_link = None


def message_link(message_id):
    """https://discord.com/channels/<server>/<channel>/<message> for a card."""
    global _channel_link
    if _channel_link is None:
        info = discord_call("GET", DISCORD_WEBHOOK_URL, None) or {}
        _channel_link = (f"https://discord.com/channels/{info.get('guild_id')}/"
                         f"{info.get('channel_id')}") if info.get("guild_id") else ""
    return f"{_channel_link}/{message_id}" if _channel_link and message_id else ""


def deliver(changes, health_cards, now, dry_run=False):
    """Turn changes into Discord posts/edits, saving message ids on records."""
    stamp = local_time(now)
    loud = [c for c in changes if c[0] in ("new", "escalated")]
    quiet_ends = [c for c in changes if c[0] == "ended"]
    sent, overflow = loud[:MAX_PUSHES_PER_RUN], loud[MAX_PUSHES_PER_RUN:]

    for kind, source, alert, record in sent:
        if dry_run:
            print(f"  [{kind}] {alert['emoji']} {alert['title']}")
            continue
        embed = build_embed(alert, history=record["history"])
        if kind == "new" or not record.get("message_id"):
            image = render_map(alert["lat"], alert["lon"], alert["zoom"])
            record["message_id"], record["attachment_id"] = post_card(embed, image)
            record["embed"] = embed
        else:
            # Update the original card in place, then post a short pointer so
            # the escalation still makes your phone buzz.
            edit_card(record["message_id"], embed, record.get("attachment_id"))
            record["embed"] = embed
            link = message_link(record["message_id"])
            post_card({
                "title": f"⬆️ Escalated to {alert['level']}: {alert['title']}"[:256],
                "description": f"[See the original alert]({link})" if link else "",
                "color": alert["colour"],
                "footer": {"text": source},
            })
        time.sleep(1)                                   # stay under rate limits

    if overflow:
        names = "\n".join(f"• {a['emoji']} {a['title']}" for _, _, a, _ in overflow)[:2000]
        if dry_run:
            print(f"  [summary] +{len(overflow)} more")
        else:
            post_card({"title": f"📋 +{len(overflow)} more alerts this check",
                       "description": names, "color": COLOUR_GREY})

    for kind, source, alert, record in quiet_ends:
        if dry_run:
            print(f"  [ended] {record.get('title')}")
            continue
        if record.get("message_id") and record.get("embed"):
            embed = dict(record["embed"], color=COLOUR_GREY)
            embed["fields"] = [f for f in embed.get("fields", []) if f["name"] != "Status"]
            embed["fields"].append({"name": "Status", "value": f"No longer listed · {stamp}",
                                    "inline": False})
            edit_card(record["message_id"], embed, record.get("attachment_id"))
            time.sleep(1)

    for card in health_cards:
        if dry_run:
            print(f"  [health] {card['title']}")
        else:
            post_card(build_embed(card))


# ---------------------------------------------------------------------------
# STATE FILE
# ---------------------------------------------------------------------------

def load_state(path=STATE_FILE):
    """Read state.json, upgrading the old {source: {key: rank}} layout."""
    if not path.exists():
        return {"version": 2, "sources": {}, "health": {}}
    raw = json.loads(path.read_text())
    if raw.get("version") == 2:
        return raw
    sources = {}
    for source, events in raw.items():
        sources[source] = {key: {"key": key, "rank": rank, "level": "", "title": key,
                                 "history": [], "track_end": True}
                           for key, rank in events.items()}
    return {"version": 2, "sources": sources, "health": {}}


def save_state(state, path=STATE_FILE):
    path.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Strict hazard alert monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen; send and save nothing")
    parser.add_argument("--test-message", action="store_true",
                        help="post one sample card to Discord, then stop")
    args = parser.parse_args()

    if not args.dry_run and not DISCORD_WEBHOOK_URL:
        sys.exit("Set DISCORD_WEBHOOK_URL (or use --dry-run).")

    if args.test_message:
        card = make_alert(key="test", source="Hazard Monitor (test)", emoji="✅",
                          title="Hazard Monitor is connected", colour=COLOUR_GREEN,
                          message="This is what an alert card looks like.",
                          fields=[("Level", "Test"), ("Type", "Setup check")],
                          lat=-37.8136, lon=144.9631, zoom=9)
        post_card(build_embed(card), render_map(card["lat"], card["lon"], card["zoom"]))
        print("Test message sent.")
        return

    now = datetime.now(timezone.utc)
    state = load_state()

    print("Checking feeds...")
    results = collect()
    changes, state["sources"] = decide(results, state["sources"], now)
    health_cards, state["health"] = update_health(results, state.get("health", {}), now)

    counts = {}
    for kind, *_ in changes:
        counts[kind] = counts.get(kind, 0) + 1
    print("Changes:", ", ".join(f"{n} {k}" for k, n in counts.items()) or "none")

    if args.dry_run:
        deliver(changes, health_cards, now, dry_run=True)
        return

    log_changes(changes, now)
    deliver(changes, health_cards, now)
    save_state(state)


if __name__ == "__main__":
    main()
