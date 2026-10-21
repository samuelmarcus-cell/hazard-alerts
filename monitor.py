"""
Hazard alert monitor
====================

Checks public warning feeds every 15 minutes and keeps a Discord server up to
date. Everything goes through one webhook on the #alerts channel:

  #alerts                cards for major events only (buzzes your phone)
  📊 Dashboard thread    one live status message, edited every run, with a map
  📒 Event log thread    a quiet line for EVERYTHING recorded, including
                         lower-level events that don't get a card
  🗓️ Weekly round-up     a summary every Monday at 8am (Melbourne time)

What gets a card in #alerts:
  BoM (all states)       tropical cyclone, major flood, extreme heatwave warnings
  State emergency feeds  Emergency Warning level (NSW, VIC, QLD, SA, WA, TAS, ACT)
  GDACS (global)         cyclones/floods/wildfires at Orange or Red; others at Red
  Canada + US fires      past 50,000 ha, again past 250,000 ha
  ABC News               deployment / disaster-declaration stories about a hazard
  Fire danger            forecast Extreme or Catastrophic days (NSW, VIC)
  Concurrent events      major events in 3+ Australian jurisdictions at once, or
                         2+ while there's a top-level disaster overseas
  Feed health            a feed failing for 24 hours, and its recovery

Recorded but NOT carded (log + Event log thread only): Watch and Act warnings,
severe thunderstorm / severe weather / moderate flood / fire weather / severe
heatwave warnings, GDACS Green cyclones/floods/fires and Orange quakes etc.,
North American fires past 10,000 ha.

Run locally:  python monitor.py --dry-run        (prints, sends nothing)
              python monitor.py --test-message   (posts one sample card)
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

try:                                   # Pillow draws the maps. Without it, cards
    from PIL import Image, ImageDraw   # and the dashboard simply have no image.
except ImportError:
    Image = None

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Australia/Melbourne")
except Exception:
    LOCAL_TZ = timezone.utc


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USERNAME = "Hazard Monitor"

# Threads inside #alerts (IDs aren't secret; they only work with the webhook).
DISCORD_THREADS = {
    "dashboard": "1561598501882961990",
    "log": "1561598739443879936",
    "weekly": "1561598862957748316",
    "news": "1561640830702329887",
}

COLOUR_RED = 0xD32F2F
COLOUR_ORANGE = 0xF57C00
COLOUR_YELLOW = 0xF9A825
COLOUR_BLUE = 0x1E88E5
COLOUR_PURPLE = 0x8E24AA
COLOUR_GREY = 0x607D8B
COLOUR_GREEN = 0x43A047

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "events_log.csv"

MAX_PUSHES_PER_RUN = 5
FEED_FAILURE_WARNING_HOURS = 24
WEEKLY_WEEKDAY, WEEKLY_HOUR = 0, 8          # Monday 8am, Melbourne time

# Bump this whenever the filters change, so newly-included lower-level events
# already in the feeds are recorded as "seen_at_start" rather than "new".
FILTERS_VERSION = 4

LOG_FIRE_HA = 10_000        # recorded
LARGE_FIRE_HA = 50_000      # card
HUGE_FIRE_HA = 250_000      # escalation card
ACRES_PER_HA = 2.47105

HEADERS = {"User-Agent": "hazard-alerts/2.0 (personal, non-commercial; "
                         "+https://github.com/samuelmarcus-cell/hazard-alerts)"}
# BoM only accepts this exact browser-style string.
BOM_HEADERS = {"User-Agent": "Mozilla/5.0 (personal hazard-alert script; non-commercial)"}

# --- BoM ------------------------------------------------------------------
BOM_FEEDS = {
    "NSW/ACT": "http://www.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml",
    "NT":      "http://www.bom.gov.au/fwo/IDZ00055.warnings_nt.xml",
    "QLD":     "http://www.bom.gov.au/fwo/IDZ00056.warnings_qld.xml",
    "SA":      "http://www.bom.gov.au/fwo/IDZ00057.warnings_sa.xml",
    "TAS":     "http://www.bom.gov.au/fwo/IDZ00058.warnings_tas.xml",
    "VIC":     "http://www.bom.gov.au/fwo/IDZ00059.warnings_vic.xml",
    "WA":      "http://www.bom.gov.au/fwo/IDZ00060.warnings_wa.xml",
}
BOM_CARD_PHRASES = {               # title contains -> emoji; gets a card
    "tropical cyclone": "🌀",
    "major flood": "🌊",           # also "Moderate to Major Flood Warning"
    "extreme heatwave": "🌡️",
}
BOM_LOG_PHRASES = {                # recorded only
    "severe thunderstorm": "⛈️",
    "severe weather": "🌬️",
    "moderate flood": "🌊",
    "fire weather": "🔥",
    "severe heatwave": "🌡️",
}
BOM_SKIP_PHRASES = ["cancellation", "final flood warning"]

# --- State emergency feeds ------------------------------------------------
STATE_FEEDS = [            # GeoJSON
    {"name": "NSW RFS", "level_field": "category",
     "url": "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"},
    {"name": "VicEmergency", "level_field": None,
     "url": "https://emergency.vic.gov.au/public/events-geojson.json"},
    {"name": "QLD Fire", "level_field": None,
     "url": "https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com"
            "/content/Feeds/BushfireCurrentIncidents/bushfireAlert.json"},
]
CAP_FEEDS = [              # CAP (Common Alerting Protocol) XML; see parse_cap
    {"name": "SA CFS", "id_param": None,
     "url": "https://data.eso.sa.gov.au/feeds/prod/cap-au.xml"},
    {"name": "WA DFES", "id_param": "CAD_ID",
     "url": "https://api.emergency.wa.gov.au/v1/capau"},
    {"name": "TasALERT", "id_param": None,
     "url": "https://alert.tas.gov.au/data/cap-au.xml"},
    {"name": "ACT ESA", "id_param": "identifier",
     "url": "https://data.esa.act.gov.au/feeds/esa-cap-incidents.xml"},
]
# Australian Warning System: what we keep, and its rank.
AWS_RANK = {"Emergency Warning": 1, "Watch and Act": 0}

# Which jurisdiction each Australian source belongs to (for concurrency).
AU_JURISDICTION = {
    "NSW RFS": "NSW", "VicEmergency": "VIC", "QLD Fire": "QLD", "SA CFS": "SA",
    "WA DFES": "WA", "TasALERT": "TAS", "ACT ESA": "ACT",
    "BoM NSW/ACT": "NSW", "BoM NT": "NT", "BoM QLD": "QLD", "BoM SA": "SA",
    "BoM TAS": "TAS", "BoM VIC": "VIC", "BoM WA": "WA",
}

# --- Fire danger forecasts ------------------------------------------------
NSW_FDR_URL = "https://www.rfs.nsw.gov.au/feeds/fdrToban.xml"
FDR_RANK = {"EXTREME": 1, "CATASTROPHIC": 2}

# --- GDACS ----------------------------------------------------------------
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
GDACS_NS = {"gdacs": "http://www.gdacs.org",
            "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
GDACS_TYPES = {
    "TC": ("Tropical cyclone", "🌀", 4), "FL": ("Flood", "🌊", 5),
    "WF": ("Wildfire", "🔥", 6), "EQ": ("Earthquake", "🌍", 5),
    "VO": ("Volcano", "🌋", 6), "TS": ("Tsunami", "🌊", 4),
    "DR": ("Drought", "🏜️", 4),
}
GDACS_WEATHER_TYPES = {"TC", "FL", "WF"}   # card at Orange (others: card at Red)
GDACS_GREEN_TYPES = {"TC", "FL"}           # also recorded at Green (Green wildfires
                                           # are hundreds of small satellite detections)
GDACS_NEVER_TYPES = {"DR"}                 # everything else recorded from Orange
LEVEL_RANK = {"green": 0, "orange": 1, "red": 2}

# --- North American fires -------------------------------------------------
CANADA_URL = "https://geoserver.cwfif.nrcan.gc.ca/geoserver/ows?" + urlencode({
    "service": "WFS", "version": "2.0.0", "request": "GetFeature",
    "outputFormat": "application/json", "srsName": "EPSG:4326",
    "typeName": "public:cwfif_national_activefires",
    "CQL_FILTER": f"record_start<=now() AND record_end>now() AND fire_size>={LOG_FIRE_HA}",
})
US_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
          "WFIGS_Incident_Locations_Current/FeatureServer/0/query?" + urlencode({
              "where": (f"IncidentTypeCategory='WF' "
                        f"AND IncidentSize>={round(LOG_FIRE_HA * ACRES_PER_HA)} "
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

# --- ABC news -------------------------------------------------------------
ABC_URL = "https://www.abc.net.au/news/feed/10719976/rss.xml"     # "Just In"

# --- ASEAN transboundary haze alerts (ASMC) -------------------------------
# A table on this page lists each alert, newest first: date, level, details
# naming the region. Level 3 (the highest) gets a card; Level 2 is recorded.
ASMC_URL = "https://asmc.asean.org/asmc-alerts/"
ASMC_REGIONS = {                     # region -> rough centre, for the map
    "Southern": (0.5, 110.0),        # Borneo / Sumatra
    "Northern": (18.0, 101.0),       # Mekong sub-region
}
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

# Hazard news (no card; posted quietly to the Hazard news thread). A story
# counts if its TITLE matches one of these, or its summary matches two.
HAZARD_NEWS_PATTERNS = [
    r"\bbush ?fires?\b", r"\bgrass ?fires?\b", r"\bwild ?fires?\b",
    r"\bfire (danger|ban|weather|season)", r"\bfloods?\b", r"\bflooding\b",
    r"\bflash flood", r"\b(thunder)?storms?\b", r"\bhail(stones?|storms?)?\b",
    r"\bcyclones?\b", r"\bheat ?waves?\b", r"\bsevere weather\b",
    r"\bdamaging winds?\b", r"\btornado(es)?\b", r"\blandslides?\b",
    r"\bearthquakes?\b", r"\btsunamis?\b", r"\bevacuat", r"\bemergency warning\b",
    r"\bwatch and act\b", r"\bheavy rain", r"\brainfall\b", r"\bweather bureau\b",
    r"\bbureau of meteorology\b", r"\bhaze\b", r"\bforest ?fires?\b",
    r"\bpeat(land)? ?fires?\b", r"\bland (and forest )?fires?\b", r"\bsmoke\b",
    r"\bair quality\b", r"\bair pollution\b",
]
# Titles containing these aren't hazard stories ("Melbourne Storm", "a flood of").
HAZARD_NEWS_EXCLUDE = [
    r"\bmelbourne storm\b", r"\bperfect storm\b", r"\bpolitical storm\b",
    r"\bstorm of\b", r"\bflood of\b", r"\bflooded with\b", r"\bflooding (in|back)\b",
]
MAX_NEWS_PER_RUN = 8


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------

def fetch(url):
    """Download a URL; return bytes, or None on any network/HTTP error."""
    headers = BOM_HEADERS if "bom.gov.au" in url else HEADERS
    try:
        response = requests.get(url, headers=headers, timeout=30)
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
    return moment.astimezone(LOCAL_TZ).strftime("%-d %b %H:%M")


def local_day(day):
    return day.strftime("%a %-d %b")


def make_alert(**values):
    """An alert with defaults. notify=False means "record, but no card"."""
    alert = {"rank": 1, "level": "", "emoji": "⚠️", "message": "", "link": "",
             "colour": COLOUR_ORANGE, "fields": [], "lat": None, "lon": None,
             "zoom": 7, "track_end": True, "notify": True, "news": False}
    alert.update(values)
    # Feeds sometimes put line breaks inside titles (BoM does); Discord then
    # shows only the first line. Collapse all runs of whitespace to one space.
    for field in ("title", "message"):
        alert[field] = " ".join(str(alert.get(field) or "").split())
    return alert


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def first_text(element, names):
    for child in element.iter():
        if local_name(child) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def aws_level(text):
    """The highest Australian Warning System level we keep, found in text."""
    lower = text.lower()
    for name in AWS_RANK:                     # Emergency Warning checked first
        if name.lower() in lower:
            return name
    return ""


def rgb(colour):
    return (colour >> 16) & 255, (colour >> 8) & 255, colour & 255


# ---------------------------------------------------------------------------
# PARSERS — each turns one feed's raw bytes into a list of alerts
# ---------------------------------------------------------------------------

def parse_bom(region, content):
    alerts = []
    for item in ET.fromstring(content).iter("item"):
        title = (item.findtext("title") or "").strip()
        lower = title.lower()
        if any(p in lower for p in BOM_SKIP_PHRASES):
            continue
        card = next((e for p, e in BOM_CARD_PHRASES.items() if p in lower), None)
        logged = next((e for p, e in BOM_LOG_PHRASES.items() if p in lower), None)
        if not (card or logged):
            continue
        link = (item.findtext("link") or "").strip()
        title = " ".join(title.split())
        fields = [("Region", region)]
        issued = re.match(r"(\d{1,2}/\d{1,2}:\d{2} [A-Z]{3,4})\s+(.*)", title)
        if issued:                     # "20/11:13 EST Severe Thunderstorm ..."
            fields.append(("Issued", issued.group(1)))
            title = issued.group(2)
        alerts.append(make_alert(
            key=f"bom:{link or title}", source="Bureau of Meteorology",
            rank=1 if card else 0, notify=bool(card),
            level="Major warning" if card else "Warning",
            emoji=card or logged, title=title, link=link, fields=fields,
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
        lon, lat = geometry["coordinates"][:2]
        return lat, lon
    for inner in geometry.get("geometries", []):
        lat, lon = find_point(inner)
        if lat is not None:
            return lat, lon
    return None, None


def parse_description(text):
    fields = []
    for part in re.split(r"<br\s*/?>", text or ""):
        label, _, value = part.partition(":")
        label, value = label.strip(), value.strip()
        if label in DESCRIPTION_LABELS and value:
            fields.append((label.title(), value))
    return fields


def warning_alert(source, key, level, headline, **extra):
    """Shared shape for Emergency Warning / Watch and Act alerts."""
    major = level == "Emergency Warning"
    return make_alert(
        key=f"{source}:{key}", source=source, rank=AWS_RANK[level], level=level,
        notify=major, colour=COLOUR_RED if major else COLOUR_YELLOW,
        title=f"{'EMERGENCY WARNING' if major else 'Watch and Act'}: {headline}",
        **extra,
    )


def parse_state_geojson(feed, content):
    alerts = []
    if not content.strip():
        return alerts
    data = json.loads(content)
    if isinstance(data, list):
        features = [{"properties": r} for r in data if isinstance(r, dict)]
    else:
        features = data.get("features", [])

    for feature in features:
        props = feature.get("properties") or {}
        if feed["level_field"]:
            level_text = str(props.get(feed["level_field"], ""))
        else:
            level_text = " ".join(str(v) for v in props.values())
        level = aws_level(level_text)
        if not level:
            continue
        title = first_present(props, TITLE_FIELDS) or "Unnamed incident"
        lat, lon = find_point(feature.get("geometry"))
        alerts.append(warning_alert(
            feed["name"], first_present(props, ID_FIELDS) or title, level, title,
            emoji="🔥", message=f"{feed['name']}: {level} current.",
            link=first_present(props, LINK_FIELDS),
            fields=parse_description(props.get("description")), lat=lat, lon=lon,
        ))
    return alerts


def cap_parameters(alert_el):
    params = {}
    for el in alert_el.iter():
        if local_name(el) == "parameter":
            name = first_text(el, {"valueName"})
            if name:
                params[name] = first_text(el, {"value"})
    return params


def cap_level(alert_el, params):
    """Australian Warning System level of a CAP alert. Trusts, in order: an
    AlertLevel parameter (TAS, ACT), the headline (WA), then CAP-AU severity
    (Extreme = Emergency Warning). Never the description, which can say
    "this may be upgraded to an Emergency Warning"."""
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
    for tag in ("alert", "entry", "item"):
        items = [e for e in root.iter() if local_name(e) == tag]
        if items:
            break

    for item in items:
        params = cap_parameters(item)
        level = next((n for n in AWS_RANK if n.lower() == cap_level(item, params).lower()), "")
        if not level:
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
        match = re.match(r"\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", first_text(item, {"circle", "point"}))
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
        link = first_text(item, {"web"})
        if not link:
            link_el = next((e for e in item.iter() if local_name(e) == "link"), None)
            link = link_el.get("href", "") if link_el is not None else ""

        fields = [("Area", area)] if area else []
        if event:
            fields.append(("Type", event))
        alerts.append(warning_alert(
            feed["name"], event_id, level, headline,
            emoji="🔥" if "fire" in event.lower() else "⚠️",
            message=strip_html(first_text(item, {"description", "summary"}))[:500],
            link=link, fields=fields, lat=lat, lon=lon,
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
        weather = event_type in GDACS_WEATHER_TYPES
        if rank < (LEVEL_RANK["green"] if event_type in GDACS_GREEN_TYPES else LEVEL_RANK["orange"]):
            continue
        notify = rank >= (LEVEL_RANK["orange"] if weather else LEVEL_RANK["red"])

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
            notify=notify, source="GDACS", emoji=emoji,
            title=f"{kind}{' — ' + country if country else ''}",
            message=(item.findtext("title") or "").strip(),
            link=(item.findtext("link") or "").strip(),
            colour={2: COLOUR_RED, 1: COLOUR_ORANGE}.get(rank, COLOUR_GREEN),
            fields=fields, zoom=zoom,
            lat=to_float(item.findtext("geo:Point/geo:lat", namespaces=GDACS_NS)),
            lon=to_float(item.findtext("geo:Point/geo:long", namespaces=GDACS_NS)),
        ))
    return alerts


def fire_size_alert(key, source, title, hectares, link, fields, lat, lon):
    if hectares >= HUGE_FIRE_HA:
        rank, threshold, colour = 2, HUGE_FIRE_HA, COLOUR_RED
    elif hectares >= LARGE_FIRE_HA:
        rank, threshold, colour = 1, LARGE_FIRE_HA, COLOUR_ORANGE
    else:
        rank, threshold, colour = 0, LOG_FIRE_HA, COLOUR_YELLOW
    return make_alert(
        key=key, source=source, rank=rank, level=f"{threshold:,}+ ha",
        notify=rank >= 1, emoji="🔥", title=title,
        message=f"{hectares:,.0f} hectares burned.", link=link, colour=colour,
        fields=[("Size", f"{hectares:,.0f} ha")] + fields,
        lat=lat, lon=lon, zoom=6,
    )


def parse_canada(content):
    alerts = []
    for feature in json.loads(content).get("features", []):
        p = feature.get("properties") or {}
        hectares = to_float(p.get("fire_size")) or 0
        if hectares < LOG_FIRE_HA:
            continue
        region = CANADA_REGIONS.get(p.get("agency_code", ""), p.get("agency_code", ""))
        stage = p.get("stage_of_control_status", "")
        alerts.append(fire_size_alert(
            key=f"ca:{p.get('national_fire_id') or p.get('id')}",
            source="Canada (CWFIS)", title=f"Large wildfire — {region}, Canada",
            hectares=hectares, link="https://cwfis.cfs.nrcan.gc.ca/en/interactive-map",
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
        if hectares < LOG_FIRE_HA:
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


def is_hazard_news(title, summary):
    """True for stories about a natural hazard (see HAZARD_NEWS_PATTERNS)."""
    title_l, summary_l = title.lower(), summary.lower()
    if any(re.search(p, title_l) for p in HAZARD_NEWS_EXCLUDE):
        return False
    if any(re.search(p, title_l) for p in HAZARD_NEWS_PATTERNS):
        return True
    return sum(1 for p in HAZARD_NEWS_PATTERNS if re.search(p, summary_l)) >= 2


def parse_abc(content):
    """Deployment/declaration stories -> cards; other hazard stories -> news."""
    alerts = []
    for item in ET.fromstring(content).iter("item"):
        title = (item.findtext("title") or "").strip()
        summary = strip_html(item.findtext("description"))
        text = f"{title} {summary}".lower()
        deployment = (any(p in text for p in DEPLOYMENT_PHRASES)
                      and any(h in text for h in HAZARD_WORDS))
        if not deployment and not is_hazard_news(title, summary):
            continue
        link = (item.findtext("link") or "").strip()
        alerts.append(make_alert(
            key=f"abc:{(item.findtext('guid') or link or title).strip()}",
            source="ABC News", rank=1 if deployment else 0, notify=deployment,
            news=not deployment, level="Deployment news" if deployment else "Hazard news",
            emoji="📰", title=title, message=summary[:400], link=link,
            colour=COLOUR_BLUE, track_end=False,
        ))
    return alerts


def parse_asmc(content):
    """Current ASMC haze alert level for each ASEAN region."""
    page = content.decode("utf-8", errors="replace")
    current = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S | re.I):
        cells = [" ".join(re.sub(r"<[^>]+>", " ", c).split())
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)]
        if len(cells) < 3:
            continue                    # the level-definition table has 2 columns
        issued, level_text, details = cells[0], cells[1], cells[2]
        level = re.search(r"Level\s*(\d)", level_text)
        region = re.search(r"(Southern|Northern) ASEAN", details)
        if not level or not region or region.group(1) in current:
            continue                    # rows are newest first: keep the first per region
        current[region.group(1)] = (int(level.group(1)), issued, details)

    alerts = []
    for region, (level, issued, details) in current.items():
        if level < 2:
            continue
        lat, lon = ASMC_REGIONS[region]
        alerts.append(make_alert(
            key=f"asmc:{region}", source="ASMC (ASEAN haze)",
            rank=1 if level >= 3 else 0, notify=level >= 3,
            level=f"Alert Level {level}", emoji="🌫️",
            title=f"Transboundary haze — Alert Level {level}, {region} ASEAN",
            message=details[:500], link=ASMC_URL,
            colour=COLOUR_RED if level >= 3 else COLOUR_ORANGE,
            fields=[("Issued", issued), ("Region", f"{region} ASEAN")],
            lat=lat, lon=lon, zoom=4,
        ))
    return alerts


def fdr_alerts(state, by_day, link):
    """{day: {district: rating}} -> one alert per day with Extreme+ districts."""
    alerts = []
    for day, ratings in sorted(by_day.items()):
        bad = {d: r for d, r in ratings.items() if r in FDR_RANK}
        if not bad:
            continue
        rank = max(FDR_RANK[r] for r in bad.values())
        worst = "Catastrophic" if rank == 2 else "Extreme"
        fields = []
        for rating in ("CATASTROPHIC", "EXTREME"):
            names = sorted(d for d, r in bad.items() if r == rating)
            if names:
                fields.append((rating.title(), ", ".join(names)))
        alerts.append(make_alert(
            key=f"fdr:{state}:{day.isoformat()}", source=f"Fire danger ({state})",
            rank=rank, level=worst, emoji="🔥",
            title=f"{worst} fire danger forecast — {state}, {local_day(day)}",
            message=f"{len(bad)} district(s) forecast Extreme or higher.",
            link=link, colour=COLOUR_RED if rank == 2 else COLOUR_ORANGE,
            fields=fields, track_end=False,
        ))
    return alerts


def parse_nsw_fdr(content, today):
    by_day = {today: {}, today + timedelta(days=1): {}}
    for district in ET.fromstring(content).iter("District"):
        name = (district.findtext("Name") or "").strip()
        by_day[today][name] = (district.findtext("DangerLevelToday") or "").strip().upper()
        by_day[today + timedelta(days=1)][name] = (
            district.findtext("DangerLevelTomorrow") or "").strip().upper()
    return fdr_alerts("NSW", by_day,
                      "https://www.rfs.nsw.gov.au/fire-information/fdr-and-tobans")


def parse_vic_fdr(content, today):
    data = json.loads(content)
    forecasts = ((data.get("properties") or {}).get("conditions") or {}).get("forecasts", [])
    by_day = {}
    for forecast in forecasts:
        stamp = forecast.get("date")
        if not stamp:
            continue
        day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(LOCAL_TZ).date()
        if day < today:
            continue
        by_day[day] = {d: str(r).strip().upper() for d, r in (forecast.get("fdr") or {}).items()}
    return fdr_alerts("VIC", by_day, "https://emergency.vic.gov.au/respond/")


# ---------------------------------------------------------------------------
# COLLECTING
# ---------------------------------------------------------------------------

def safe_parse(source, parser, content):
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


def collect(now):
    """Fetch and filter every source -> {source: alerts, or None if it failed}."""
    today = now.astimezone(LOCAL_TZ).date()
    jobs = []
    for region, url in BOM_FEEDS.items():
        jobs.append((f"BoM {region}", url, lambda c, r=region: parse_bom(r, c)))
    for feed in STATE_FEEDS:
        jobs.append((feed["name"], feed["url"], lambda c, f=feed: parse_state_geojson(f, c)))
    for feed in CAP_FEEDS:
        jobs.append((feed["name"], feed["url"], lambda c, f=feed: parse_cap(f, c)))
    jobs += [
        ("Fire danger (NSW)", NSW_FDR_URL, lambda c: parse_nsw_fdr(c, today)),
        ("Fire danger (VIC)", STATE_FEEDS[1]["url"], lambda c: parse_vic_fdr(c, today)),
        ("GDACS", GDACS_URL, parse_gdacs),
        ("Canada fires", CANADA_URL, parse_canada),
        ("US fires", US_URL, parse_us),
        ("ABC News", ABC_URL, parse_abc),
        ("ASMC haze", ASMC_URL, parse_asmc),
    ]
    downloaded = {}                       # VicEmergency is used twice: fetch once
    results = {}
    for source, url, parser in jobs:
        if url not in downloaded:
            downloaded[url] = fetch(url)
        results[source] = safe_parse(source, parser, downloaded[url])
    return results


# ---------------------------------------------------------------------------
# DECIDING WHAT CHANGED  (pure functions: no network, no files)
# ---------------------------------------------------------------------------

def is_major(record):
    """Did/does this event get a card? (Older records lack the flag.)"""
    return record.get("notify", record.get("rank", 0) >= 1)


def decide(results, old_sources, now, seed_all=False):
    """Compare this run's alerts with memory.

    Returns (changes, new_sources); each change is (kind, source, alert, record):
      seen_at_start  first read of a source (or filters changed): record only
      new            a new event
      escalated      a known event at a higher rank
      ended          a known event no longer in the feed (alert is None)
    Records keep the PEAK rank, so wobbling Red->Orange->Red notifies once.
    """
    changes, new_sources = [], {}
    stamp = local_time(now)

    for source, alerts in results.items():
        if alerts is None:
            new_sources[source] = old_sources.get(source, {})
            continue

        seeding = seed_all or source not in old_sources
        previous = old_sources.get(source, {})
        current = {}

        for alert in alerts:
            key = alert["key"]
            record = previous.get(key)
            if record is None:
                record = {
                    "key": key, "rank": alert["rank"], "level": alert["level"],
                    "title": alert["title"], "emoji": alert["emoji"],
                    "colour": alert["colour"], "notify": alert["notify"],
                    "first_seen": now.isoformat(),
                    "history": [f"{alert['level'] or 'Listed'} · {stamp}"],
                    "lat": alert["lat"], "lon": alert["lon"], "link": alert["link"],
                    "track_end": alert["track_end"], "news": alert["news"],
                }
                changes.append(("seen_at_start" if seeding else "new", source, alert, record))
                current[key] = record
                continue

            # Known event: refresh its descriptive details from the feed
            # (rank and level keep their peak).
            for field in ("title", "link", "lat", "lon", "emoji"):
                if alert.get(field) not in (None, ""):
                    record[field] = alert[field]
            if alert["rank"] > record["rank"]:
                record = dict(
                    record, rank=alert["rank"], level=alert["level"],
                    colour=alert["colour"], notify=is_major(record) or alert["notify"],
                    history=record.get("history", []) + [f"{alert['level']} · {stamp}"])
                changes.append(("escalated", source, alert, record))
            current[key] = record

        # Items that vanish because a filter was narrowed haven't "ended".
        if not seed_all:
            for key, record in previous.items():
                if key not in current and record.get("track_end", True):
                    changes.append(("ended", source, None, record))
        new_sources[source] = current

    return changes, new_sources


def concurrent_alerts(sources):
    """Alerts describing overlapping major events, from the current state."""
    by_place = {}
    for source, events in sources.items():
        place = AU_JURISDICTION.get(source)
        if place:
            for record in events.values():
                if is_major(record):
                    by_place.setdefault(place, []).append(record["title"])
    overseas = [r["title"] for s in ("GDACS", "Canada fires", "US fires")
                for r in sources.get(s, {}).values() if r.get("rank", 0) >= 2]

    def lines(names):
        return "\n".join(f"• {n[:90]}" for n in names[:6]) + (
            f"\n…and {len(names) - 6} more" if len(names) > 6 else "")

    alerts = []
    places = sorted(by_place)
    if len(places) >= 3:
        rank = 2 if len(places) >= 5 else 1
        alerts.append(make_alert(
            key="concurrent:australia", source="Concurrent events", rank=rank,
            level=f"{len(places)} jurisdictions", emoji="🧭",
            title=f"Major events in {len(places)} states and territories at once",
            message=("When several jurisdictions are responding at the same time, "
                     "interstate resource sharing is likely to be stretched."),
            colour=COLOUR_PURPLE,
            fields=[(p, lines(by_place[p])) for p in places],
        ))
    if len(places) >= 2 and overseas:
        alerts.append(make_alert(
            key="concurrent:global", source="Concurrent events",
            level="Australia + overseas", emoji="🌏",
            title="Major Australian events overlapping a top-level disaster overseas",
            message=("Major events in " + ", ".join(places) + " while a Red-level or "
                     "250,000 ha+ event is active overseas, competing for the same "
                     "international resources."),
            colour=COLOUR_PURPLE,
            fields=[("Australia", lines([t for p in places for t in by_place[p]])),
                    ("Overseas", lines(overseas))],
        ))
    return alerts


def update_health(results, health, now):
    """Track failing feeds. Returns (cards_to_post, new_health)."""
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
                    colour=COLOUR_GREY))
        else:
            if entry["warned"]:
                cards.append(make_alert(
                    key=f"health:{source}", source="Hazard Monitor", emoji="✅",
                    title=f"Feed working again: {source}", colour=COLOUR_GREEN))
            entry = {"failing_since": None, "warned": False}
        new_health[source] = entry
    return cards, new_health


# ---------------------------------------------------------------------------
# EVENT LOG FILE
# ---------------------------------------------------------------------------

LOG_COLUMNS = ["time_utc", "change", "source", "key", "level", "title",
               "lat", "lon", "link", "major"]


def ensure_log_columns(path):
    """Add any new columns to an existing log (older rows get blanks)."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] == LOG_COLUMNS:
        return
    width = len(LOG_COLUMNS)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(LOG_COLUMNS)
        for row in rows[1:]:
            writer.writerow((row + [""] * width)[:width])


def log_changes(changes, now, path=LOG_FILE):
    if not changes:
        return
    ensure_log_columns(path)
    is_new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if is_new_file:
            writer.writerow(LOG_COLUMNS)
        for kind, source, alert, record in changes:
            writer.writerow([
                now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), kind,
                source, record["key"], record.get("level", ""), record.get("title", ""),
                "" if record.get("lat") is None else record["lat"],
                "" if record.get("lon") is None else record["lon"],
                record.get("link", ""), "yes" if is_major(record) else "no",
            ])


def read_log(since, path=LOG_FILE):
    """Rows (as dicts) logged at or after `since`."""
    if not path.exists():
        return []
    cutoff = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("time_utc", "") >= cutoff]


# ---------------------------------------------------------------------------
# MAPS
# ---------------------------------------------------------------------------

def render_view(lat, lon, zoom, width, height, markers):
    """An OpenStreetMap view centred on (lat, lon) with coloured dots, as a
    Pillow image, or None if Pillow or any tile is unavailable.

    markers: [(lat, lon, (r, g, b)), ...]
    """
    if Image is None:
        return None
    tile, n = 256, 2 ** zoom
    world = n * tile

    def to_pixel(p_lat, p_lon):           # Web Mercator: lat/lon -> world pixels
        x = (p_lon + 180) / 360 * world
        p_lat = max(min(p_lat, 85.0), -85.0)
        y = (1 - math.asinh(math.tan(math.radians(p_lat))) / math.pi) / 2 * world
        return x, y

    cx, cy = to_pixel(lat, lon)
    left, top = cx - width / 2, cy - height / 2
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
    radius = 9 if len(markers) == 1 else 6
    for m_lat, m_lon, colour in markers:
        x, y = to_pixel(m_lat, m_lon)
        x -= left
        if x < 0:
            x += world                    # wrap around the date line
        elif x > width:
            x -= world
        y -= top
        if 0 <= x <= width and 0 <= y <= height:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         fill=colour, outline=(255, 255, 255), width=2)
    return canvas


def to_png(image):
    """Add the OpenStreetMap credit and return PNG bytes."""
    draw = ImageDraw.Draw(image)
    credit = "© OpenStreetMap contributors"
    w = draw.textlength(credit)
    width, height = image.size
    draw.rectangle((width - w - 8, height - 16, width, height), fill=(255, 255, 255))
    draw.text((width - w - 4, height - 14), credit, fill=(60, 60, 60))
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def render_map(lat, lon, zoom, width=600, height=300):
    """Single-event map for a card, or None."""
    if lat is None or lon is None:
        return None
    view = render_view(lat, lon, zoom, width, height, [(lat, lon, rgb(COLOUR_RED))])
    return to_png(view) if view else None


def render_dashboard_map(markers):
    """World view and Australia view side by side, or None."""
    world = render_view(20, 20, 1, 512, 300, markers)
    australia = render_view(-27, 134, 3, 440, 300, markers)
    if world is None or australia is None:
        return None
    combined = Image.new("RGB", (512 + 8 + 440, 300), (255, 255, 255))
    combined.paste(world, (0, 0))
    combined.paste(australia, (520, 0))
    return to_png(combined)


# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def webhook(path="", thread=None, wait=False):
    params = []
    if wait:
        params.append("wait=true")
    if thread:
        params.append(f"thread_id={thread}")
    return DISCORD_WEBHOOK_URL + path + ("?" + "&".join(params) if params else "")


def discord_call(method, url, payload, image=None):
    """Send to Discord, retrying once if rate-limited. Response JSON or None."""
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


def attachment_id(reply):
    """ID of a message's map image. Images shown inside an embed often aren't
    listed under "attachments", so fall back to the ID in the image's address:
    .../attachments/<channel id>/<attachment id>/map.png"""
    reply = reply or {}
    attachments = reply.get("attachments") or []
    if attachments:
        return attachments[0]["id"]
    for embed in reply.get("embeds") or []:
        for key in ("url", "proxy_url"):
            match = re.search(r"/attachments/\d+/(\d+)/", (embed.get("image") or {}).get(key, ""))
            if match:
                return match.group(1)
    return None


def post_card(embed, image=None, thread=None):
    """Post a card. Returns (message_id, attachment_id) or (None, None)."""
    if image:
        embed = dict(embed, image={"url": "attachment://map.png"})
    reply = discord_call("POST", webhook(thread=thread, wait=True),
                         {"username": DISCORD_USERNAME, "embeds": [embed]}, image)
    return ((reply or {}).get("id"), attachment_id(reply)) if reply else (None, None)


def post_text(text, thread=None, quiet=True, previews=False):
    """Post plain text (split to Discord's 2000-char limit). quiet = no
    notification sound; previews = show link preview cards."""
    chunk, chunks = "", []
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 1900:
            chunks.append(chunk)
            chunk = ""
        chunk += line[:1900] + "\n"
    if chunk.strip():
        chunks.append(chunk)
    for part in chunks:
        payload = {"username": DISCORD_USERNAME, "content": part}
        flags = (4096 if quiet else 0) | (0 if previews else 4)
        if flags:                     # 4096 = no notification sound, 4 = no previews
            payload["flags"] = flags
        discord_call("POST", webhook(thread=thread, wait=True), payload)
        time.sleep(0.5)


def edit_card(message_id, embed, keep_attachment=None, thread=None, image=None):
    """Replace a card's embed. Keeps its existing image unless `image` is new."""
    payload = {"embeds": [embed], "attachments": []}
    if image:
        payload["embeds"] = [dict(embed, image={"url": "attachment://map.png"})]
        payload["attachments"] = [{"id": 0, "filename": "map.png"}]
    elif keep_attachment:
        payload["embeds"] = [dict(embed, image={"url": "attachment://map.png"})]
        payload["attachments"] = [{"id": keep_attachment}]
    return discord_call("PATCH", webhook(f"/messages/{message_id}", thread=thread),
                        payload, image)


def delete_message(message_id, thread=None):
    discord_call("DELETE", webhook(f"/messages/{message_id}", thread=thread), None)


_channel_link = None


def message_link(message_id):
    global _channel_link
    if _channel_link is None:
        info = discord_call("GET", DISCORD_WEBHOOK_URL, None) or {}
        _channel_link = (f"https://discord.com/channels/{info.get('guild_id')}/"
                         f"{info.get('channel_id')}") if info.get("guild_id") else ""
    return f"{_channel_link}/{message_id}" if _channel_link and message_id else ""


def build_embed(alert, history=None, status=None):
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
    long_fields = {"History", "Status", "Australia", "Overseas"}
    embed["fields"] = [{"name": str(name)[:256], "value": str(value)[:1024] or "—",
                        "inline": name not in long_fields and len(str(value)) < 60}
                       for name, value in fields][:25]
    return embed


# ---------------------------------------------------------------------------
# DELIVERY: cards, event log thread, dashboard, weekly round-up
# ---------------------------------------------------------------------------

def deliver(changes, health_cards, now, dry_run=False):
    """Cards in #alerts for major changes; grey out ended cards."""
    stamp = local_time(now)
    loud = [c for c in changes if c[0] in ("new", "escalated") and c[2]["notify"]]
    sent, overflow = loud[:MAX_PUSHES_PER_RUN], loud[MAX_PUSHES_PER_RUN:]

    for kind, source, alert, record in sent:
        if dry_run:
            print(f"  [{kind}] {alert['emoji']} {alert['title']}")
            continue
        embed = build_embed(alert, history=record["history"])
        if not record.get("message_id"):
            image = render_map(alert["lat"], alert["lon"], alert["zoom"])
            record["message_id"], record["attachment_id"] = post_card(embed, image)
        else:
            edit_card(record["message_id"], embed, record.get("attachment_id"))
            link = message_link(record["message_id"])
            post_card({
                "title": f"⬆️ Escalated to {alert['level']}: {alert['title']}"[:256],
                "description": f"[See the original alert]({link})" if link else "",
                "color": alert["colour"], "footer": {"text": source},
            })
        record["embed"] = embed
        time.sleep(1)

    if overflow:
        names = "\n".join(f"• {a['emoji']} {a['title']}" for _, _, a, _ in overflow)[:2000]
        if dry_run:
            print(f"  [summary] +{len(overflow)} more")
        else:
            post_card({"title": f"📋 +{len(overflow)} more alerts this check",
                       "description": names, "color": COLOUR_GREY})

    for kind, source, alert, record in changes:
        if kind != "ended" or not record.get("message_id") or not record.get("embed"):
            continue
        if dry_run:
            print(f"  [ended] {record.get('title')}")
            continue
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


def post_news(changes, dry_run=False):
    """Quietly post new hazard stories, one message each with its preview."""
    stories = [c[2] for c in changes if c[0] == "new" and c[2].get("news")]
    thread = DISCORD_THREADS.get("news") or DISCORD_THREADS["log"]
    for story in stories[:MAX_NEWS_PER_RUN]:
        if dry_run:
            print(f"  [news] {story['title']}")
        else:
            post_text(f"📰 **{story['title']}**\n{story['link']}", thread=thread, previews=True)
    extra = stories[MAX_NEWS_PER_RUN:]
    if extra and not dry_run:
        post_text("📰 More hazard stories:\n" + "\n".join(
            f"• [{s['title']}](<{s['link']}>)" for s in extra), thread=thread)


CHANGE_ICONS = {"new": "🆕", "escalated": "⬆️", "ended": "✔️"}


def log_digest(changes, now):
    """Text for the Event log thread, or '' if nothing to say."""
    changes = [c for c in changes if not c[3].get("news")]
    starting = [c for c in changes if c[0] == "seen_at_start"]
    others = [c for c in changes if c[0] != "seen_at_start"]
    if not changes:
        return ""
    lines = [f"**{local_time(now)}**"]
    for kind, source, alert, record in others:
        title = record.get("title", "")[:110]
        link = record.get("link")
        shown = f"[{title}](<{link}>)" if link else title
        star = " ⭐" if is_major(record) else ""
        lines.append(f"{CHANGE_ICONS[kind]} {record.get('emoji', '')} "
                     f"**{record.get('level') or kind}** · {source} · {shown}{star}")
    if starting:
        lines.append(f"📥 {len(starting)} event(s) already active when monitoring of "
                     f"their source began (recorded in events_log.csv)")
    return "\n".join(lines)


def dashboard_embed(sources, results, now):
    """(embed, markers) describing everything that's currently major."""
    australia, international, danger, concurrent = [], [], [], []
    watch_and_act = 0
    markers = []

    for source, events in sources.items():
        for record in events.values():
            if source in AU_JURISDICTION and record.get("level") == "Watch and Act":
                watch_and_act += 1
            if not is_major(record) or source == "ABC News":
                continue
            line = (f"{record.get('emoji', '')} "
                    + (f"[{record['title'][:70]}]({record['link']})" if record.get("link")
                       else record["title"][:70]))
            if source.startswith("Fire danger"):
                danger.append(line)
            elif source == "Concurrent events":
                concurrent.append(line)
            elif source in AU_JURISDICTION:
                australia.append((record.get("rank", 0), line))
            else:
                international.append((record.get("rank", 0), line))
            if record.get("lat") is not None and record.get("lon") is not None:
                markers.append((round(record["lat"], 3), round(record["lon"], 3),
                                rgb(record.get("colour", COLOUR_RED))))

    def block(items, empty):
        items = [i[1] if isinstance(i, tuple) else i
                 for i in sorted(items, key=lambda x: -x[0] if isinstance(x, tuple) else 0)]
        text, shown = "", 0
        for item in items:
            if len(text) + len(item) + 30 > 1000:
                break
            text += item + "\n"
            shown += 1
        if shown < len(items):
            text += f"…and {len(items) - shown} more"
        return text.strip() or empty

    failing = sorted(s for s, a in results.items() if a is None)
    total = len(results)
    feeds = (f"✅ All {total} feeds working" if not failing else
             f"⚠️ {total - len(failing)}/{total} working · not responding: "
             + ", ".join(failing))

    week = [r for r in read_log(now - timedelta(days=7)) if r.get("level") != "Hazard news"]
    week_major = sum(1 for r in week if r.get("major") == "yes" and r["change"] in ("new", "escalated"))
    week_logged = sum(1 for r in week if r["change"] in ("new", "escalated"))

    worst = max([r.get("rank", 0) for e in sources.values() for r in e.values() if is_major(r)],
                default=-1)
    colour = COLOUR_RED if worst >= 2 else COLOUR_ORANGE if worst >= 1 else COLOUR_GREEN

    fields = []
    if concurrent:
        fields.append(("🧭 Overlapping events", block(concurrent, "")))
    fields += [
        (f"🇦🇺 Australia · {len(australia)} major · {watch_and_act} Watch and Act",
         block(australia, "No major warnings current.")),
        (f"🌏 International · {len(international)} major",
         block(international, "No major events current.")),
        ("🔥 Fire danger outlook", block(danger, "No Extreme or Catastrophic days forecast.")),
        ("📒 Last 7 days", f"{week_major} new major events · {week_logged} new events recorded"),
        ("🛠️ Feeds", feeds),
    ]
    embed = {
        "title": "📊 Hazard dashboard",
        "description": (f"Updated {local_time(now)} (Melbourne time). Refreshes every "
                        "check, about every 15 minutes."),
        "color": colour,
        "fields": [{"name": n[:256], "value": v[:1024], "inline": False} for n, v in fields],
        "footer": {"text": "Map: dots are current major events. Left: world · Right: Australia"},
    }
    return embed, sorted(set(markers))


def update_dashboard(state, results, now, dry_run=False):
    embed, markers = dashboard_embed(state["sources"], results, now)
    if dry_run:
        print("  [dashboard]", " | ".join(f["name"] for f in embed["fields"]))
        return
    dash = state.setdefault("dashboard", {})
    thread = DISCORD_THREADS["dashboard"]
    map_hash = hashlib.sha1(json.dumps(markers).encode()).hexdigest()[:12]
    image = None
    if map_hash != dash.get("map_hash") or not dash.get("attachment_id"):
        image = render_dashboard_map(markers)

    # Edit the existing dashboard message. If that fails (first run, message
    # deleted, or the thread archived itself after a quiet week), post a new
    # one, which also re-opens the thread, and remove the old one.
    reply = None
    if dash.get("message_id"):
        reply = edit_card(dash["message_id"], embed, dash.get("attachment_id"), thread, image)
    if reply is None:
        old = dash.get("message_id")
        if image is None:
            image = render_dashboard_map(markers)
        message_id, att = post_card(embed, image, thread)
        if message_id:
            if old:
                delete_message(old, thread)
            dash.update(message_id=message_id, attachment_id=att,
                        map_hash=map_hash if image else None)
        return
    if image:
        dash.update(attachment_id=attachment_id(reply) or dash.get("attachment_id"),
                    map_hash=map_hash)


def weekly_due(state, now):
    """The Monday-8am mark for this week if a round-up is due, else None."""
    local = now.astimezone(LOCAL_TZ)
    monday = local.date() - timedelta(days=(local.weekday() - WEEKLY_WEEKDAY) % 7)
    mark = datetime.combine(monday, dtime(WEEKLY_HOUR), LOCAL_TZ)
    if local >= mark and state.get("last_weekly") != mark.isoformat():
        return mark
    return None


def weekly_embed(rows, results, now):
    news = sum(1 for r in rows if r.get("level") == "Hazard news" and r["change"] == "new")
    rows = [r for r in rows if r.get("level") != "Hazard news"]
    majors = [r for r in rows if r.get("major") == "yes" and r["change"] in ("new", "escalated")]
    recorded = [r for r in rows if r["change"] in ("new", "escalated")]
    ended = [r for r in rows if r["change"] == "ended"]

    by_source = {}
    for r in recorded:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    top = sorted(by_source.items(), key=lambda kv: -kv[1])[:10]

    major_lines = []
    for r in majors[:15]:
        when = datetime.strptime(r["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        title = r["title"][:80]
        shown = f"[{title}]({r['link']})" if r.get("link") else title
        major_lines.append(f"{local_time(when)} · {shown}")
    if len(majors) > 15:
        major_lines.append(f"…and {len(majors) - 15} more")

    failing = sorted(s for s, a in results.items() if a is None)
    start = (now - timedelta(days=7)).astimezone(LOCAL_TZ)
    return {
        "title": f"🗓️ Week of {start.strftime('%-d %b')} – {local_day(now.astimezone(LOCAL_TZ))}",
        "description": (f"**{len(majors)}** major events (cards) · **{len(recorded)}** events "
                        f"recorded in total · **{len(ended)}** ended · **{news}** hazard news "
                        f"stories."),
        "color": COLOUR_BLUE,
        "fields": [
            {"name": "Major events", "value": ("\n".join(major_lines) or "None this week.")[:1024],
             "inline": False},
            {"name": "Recorded by source",
             "value": ("\n".join(f"{s}: {n}" for s, n in top) or "Nothing recorded.")[:1024],
             "inline": False},
            {"name": "Feeds",
             "value": (f"✅ All {len(results)} working" if not failing else
                       "⚠️ Not responding now: " + ", ".join(failing))[:1024],
             "inline": False},
        ],
        "footer": {"text": "Full detail: events_log.csv in the GitHub repo"},
    }


# ---------------------------------------------------------------------------
# STATE FILE
# ---------------------------------------------------------------------------

def load_state(path=STATE_FILE):
    if not path.exists():
        return {"version": 2, "sources": {}, "health": {}}
    raw = json.loads(path.read_text())
    if raw.get("version") == 2:
        return raw
    sources = {s: {k: {"key": k, "rank": r, "level": "", "title": k, "history": [],
                       "track_end": True} for k, r in events.items()}
               for s, events in raw.items()}
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
    seed_all = state.get("filters_version") != FILTERS_VERSION

    print("Checking feeds...")
    results = collect(now)
    changes, state["sources"] = decide(results, state["sources"], now, seed_all=seed_all)

    # Overlapping events are judged from the updated state, then go through
    # the same new/escalated/ended logic as everything else.
    concurrent = {"Concurrent events": concurrent_alerts(state["sources"])}
    more, updated = decide(concurrent, state["sources"], now)
    changes += more
    state["sources"].update(updated)

    health_cards, state["health"] = update_health(results, state.get("health", {}), now)

    counts = {}
    for kind, *_ in changes:
        counts[kind] = counts.get(kind, 0) + 1
    print("Changes:", ", ".join(f"{n} {k}" for k, n in counts.items()) or "none")

    due = weekly_due(state, now)
    if args.dry_run:
        deliver(changes, health_cards, now, dry_run=True)
        post_news(changes, dry_run=True)
        print(log_digest(changes, now) or "  [log] nothing")
        update_dashboard(state, results, now, dry_run=True)
        if due:
            print("  [weekly] round-up would be posted")
        return

    log_changes(changes, now)
    deliver(changes, health_cards, now)
    post_news(changes)
    digest = log_digest(changes, now)
    if digest:
        post_text(digest, thread=DISCORD_THREADS["log"])
    update_dashboard(state, results, now)
    if due:
        post_card(weekly_embed(read_log(now - timedelta(days=7)), results, now),
                  thread=DISCORD_THREADS["weekly"])
        state["last_weekly"] = due.isoformat()
    state["filters_version"] = FILTERS_VERSION
    save_state(state)


if __name__ == "__main__":
    main()
