"""
Hazard alert monitor
====================

Checks a set of public warning feeds and posts a Discord message ONLY for the
biggest events:

  * Australia, weather  : BoM warnings for every state, but only the top-tier
                          products (tropical cyclones, major floods, extreme
                          heatwaves).
  * Australia, fire etc.: state emergency feeds, only items at the
                          "Emergency Warning" level (the top level of the
                          Australian Warning System).
  * International       : GDACS alerts at Orange or Red for cyclones, floods
                          and wildfires, plus Red for anything else.

How one run works (the whole program is this loop, run every 15 minutes):

  1. FETCH   each feed
  2. FILTER  keep only items that pass the strict rules
  3. COMPARE with what we saw last run (state.json)
             - brand new item          -> notify
             - same item, higher level -> notify (escalation, e.g. Orange->Red)
             - same item, same level   -> stay quiet
  4. NOTIFY  via a Discord webhook, then save the new state

Run it yourself with:   python monitor.py --dry-run
(--dry-run prints what it WOULD send instead of sending it.)
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# CONFIGURATION — everything you might want to tweak lives here
# ---------------------------------------------------------------------------

# Your Discord webhook URL. On GitHub this comes from a repository secret.
# Treat it like a password: anyone with the URL can post into your channel.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# The name and look of the "bot" that posts the messages.
DISCORD_USERNAME = "Hazard Monitor"

# Card colours, written as hex numbers (Discord wants a number, not "#d32f2f").
COLOUR_RED = 0xD32F2F       # Emergency Warning, GDACS Red
COLOUR_ORANGE = 0xF57C00    # BoM top-tier warnings, GDACS Orange
COLOUR_GREY = 0x607D8B      # the "+N more" summary card

# Where we remember what we've already seen between runs.
STATE_FILE = Path(__file__).with_name("state.json")

# Safety valve: never send more than this many alerts in one run. If a big day
# produces more, you get the first few plus one summary message.
MAX_PUSHES_PER_RUN = 5

# Identify ourselves politely; BoM in particular rejects Python's default.
HEADERS = {"User-Agent": "Mozilla/5.0 (personal hazard-alert script; non-commercial)"}

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

# A BoM warning is only interesting if its title contains one of these.
# Each phrase is paired with the emoji shown on its Discord card.
# Deliberately NOT included: "severe thunderstorm", "severe weather",
# "minor flood", "moderate flood" — too frequent for an instant alert.
BOM_KEEP_PHRASES = {
    "tropical cyclone": "🌀",
    "major flood": "🌊",        # also catches "Moderate to Major Flood Warning"
    "extreme heatwave": "🌡️",
}
# ...unless the title also contains one of these (end-of-event messages).
BOM_SKIP_PHRASES = ["cancellation", "final flood warning"]

# --- State emergency feeds (GeoJSON) --------------------------------------
# level_field: the property that holds the warning level, if we know it.
#              None means "search every property for the words".
STATE_FEEDS = [
    {
        "name": "NSW RFS",
        "url": "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json",
        "level_field": "category",
    },
    {
        "name": "VicEmergency",
        "url": "https://emergency.vic.gov.au/public/events-geojson.json",
        "level_field": None,
    },
    {
        "name": "QLD Fire",
        "url": "https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com"
               "/content/Feeds/BushfireCurrentIncidents/bushfireAlert.json",
        "level_field": None,
    },
    {
        "name": "SA CFS",
        "url": "https://data.eso.sa.gov.au/prod/cfs/criimson/cfs_current_incidents.json",
        "level_field": None,
    },
]
STATE_KEEP_LEVEL = "emergency warning"

# --- GDACS: global disasters ----------------------------------------------
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
# GDACS uses two "prefixed" tag families; this spells out what each prefix means.
GDACS_NS = {
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
}

# Event type codes, with a readable name and an emoji for each.
GDACS_TYPES = {
    "TC": ("Tropical cyclone", "🌀"),
    "FL": ("Flood", "🌊"),
    "WF": ("Wildfire", "🔥"),
    "EQ": ("Earthquake", "🌍"),
    "VO": ("Volcano", "🌋"),
    "TS": ("Tsunami", "🌊"),
    "DR": ("Drought", "🏜️"),
}
GDACS_ORANGE_TYPES = {"TC", "FL", "WF"}   # these notify at Orange or Red
GDACS_NEVER_TYPES = {"DR"}                # droughts sit at Orange for months

# Turning colour words into numbers lets us say "is this level HIGHER?"
LEVEL_RANK = {"green": 0, "orange": 1, "red": 2}


# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------

def fetch(url):
    """Download a URL. Returns the raw bytes, or None if anything goes wrong.

    Returning None (instead of crashing) means one broken feed never stops
    the others from being checked.
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()          # turn 403/500 etc. into an error
        return response.content
    except requests.RequestException as error:
        print(f"  ! could not fetch {url}: {error}")
        return None


# ---------------------------------------------------------------------------
# PARSING + FILTERING — each returns a list of "alert" dictionaries
#
# Every alert has the same shape, whatever feed it came from:
#   key     : a stable ID for this event, used to recognise it next run
#   rank    : how serious (bigger = worse); used to detect escalation
#   source  : which feed it came from (shown small at the bottom of the card)
#   emoji   : shown in front of the title
#   title   : the card's headline
#   message : the card's main text
#   link    : where the title links to
#   colour  : the card's side-bar colour
#   fields  : list of (label, value) pairs shown as a small table
#   lat/lon : event location, if known (adds a "Map" link)
# ---------------------------------------------------------------------------

def make_alert(**values):
    """Build an alert dictionary, filling in defaults for anything left out."""
    alert = {"rank": 1, "emoji": "⚠️", "message": "", "link": "",
             "colour": COLOUR_ORANGE, "fields": [], "lat": None, "lon": None}
    alert.update(values)
    return alert


def parse_bom(region, content):
    """Pick the top-tier warnings out of one BoM state RSS feed."""
    alerts = []
    root = ET.fromstring(content)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        lower = title.lower()

        # Find which keep-phrase matched (if any) so we can use its emoji.
        emoji = next((e for phrase, e in BOM_KEEP_PHRASES.items() if phrase in lower), None)
        if emoji is None:
            continue
        if any(phrase in lower for phrase in BOM_SKIP_PHRASES):
            continue

        # The link points at the warning product page (e.g. IDQ20023.shtml).
        # It stays the same while BoM re-issues updates, so it makes a good
        # ID: one alert per warning, not one per update.
        link = (item.findtext("link") or "").strip()
        alerts.append(make_alert(
            key=f"bom:{link or title}",
            source="Bureau of Meteorology",
            emoji=emoji,
            title=title,
            link=link,
            fields=[("Region", region)],
        ))
    return alerts


# Different agencies name their fields differently, so we try a few.
TITLE_FIELDS = ["title", "sourceTitle", "WarningTitle", "Title", "name", "Location", "location"]
ID_FIELDS = ["guid", "id", "sourceId", "UniqueID", "OBJECTID", "IncidentNo"]
LINK_FIELDS = ["link", "url", "Link", "URL"]

# Labels from NSW-style descriptions worth showing on the card.
DESCRIPTION_LABELS = ["LOCATION", "COUNCIL AREA", "STATUS", "TYPE", "SIZE"]


def first_present(props, field_names):
    """Return the first of field_names that exists (and isn't empty) in props."""
    for name in field_names:
        value = props.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def find_point(geometry):
    """Dig a (lat, lon) out of a GeoJSON geometry, or return (None, None).

    GeoJSON stores points as [longitude, latitude] — the opposite order to
    how we usually say them — so we swap them here. NSW wraps its point inside
    a "GeometryCollection", so we look inside those too (this function calls
    itself on each inner geometry: that's called recursion).
    """
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
    """Turn 'STATUS: Out of control <br />TYPE: Bush Fire' into card fields."""
    fields = []
    for part in re.split(r"<br\s*/?>", text or ""):
        label, _, value = part.partition(":")
        label, value = label.strip(), value.strip()
        if label in DESCRIPTION_LABELS and value:
            fields.append((label.title(), value))
    return fields


def parse_state_geojson(feed, content):
    """Pick Emergency Warning-level items out of a state GeoJSON feed."""
    alerts = []
    data = json.loads(content)
    for feature in data.get("features", []):
        props = feature.get("properties") or {}

        # Which text do we search for the level words?
        if feed["level_field"]:
            level_text = str(props.get(feed["level_field"], ""))
        else:
            # Unknown layout: glue every property value into one string.
            level_text = " ".join(str(v) for v in props.values())

        if STATE_KEEP_LEVEL not in level_text.lower():
            continue

        title = first_present(props, TITLE_FIELDS) or "Unnamed incident"
        event_id = first_present(props, ID_FIELDS) or title
        lat, lon = find_point(feature.get("geometry"))
        alerts.append(make_alert(
            key=f"{feed['name']}:{event_id}",
            source=feed["name"],
            emoji="🔥",
            title=f"EMERGENCY WARNING: {title}",
            message=f"{feed['name']} has an Emergency Warning current.",
            link=first_present(props, LINK_FIELDS),
            colour=COLOUR_RED,
            fields=parse_description(props.get("description")),
            lat=lat,
            lon=lon,
        ))
    return alerts


def to_float(text):
    """Convert text to a number, or None if it isn't one."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_gdacs(content):
    """Pick Orange/Red disasters out of the GDACS global feed."""
    alerts = []
    root = ET.fromstring(content)
    for item in root.iter("item"):
        event_type = item.findtext("gdacs:eventtype", namespaces=GDACS_NS) or ""
        level = (item.findtext("gdacs:alertlevel", namespaces=GDACS_NS) or "").lower()
        event_id = item.findtext("gdacs:eventid", namespaces=GDACS_NS) or ""
        country = item.findtext("gdacs:country", namespaces=GDACS_NS) or ""
        severity = item.findtext("gdacs:severity", namespaces=GDACS_NS) or ""
        rank = LEVEL_RANK.get(level, 0)

        if event_type in GDACS_NEVER_TYPES:
            continue
        if event_type in GDACS_ORANGE_TYPES:
            if rank < LEVEL_RANK["orange"]:
                continue
        elif rank < LEVEL_RANK["red"]:       # every other type needs Red
            continue

        kind, emoji = GDACS_TYPES.get(event_type, (event_type, "⚠️"))
        fields = [("Level", level.title()), ("Type", kind)]
        if country:
            fields.append(("Country", country))
        if severity:
            fields.append(("Severity", severity.strip()))

        alerts.append(make_alert(
            # Key on type + event ID (not the episode), so a long-running
            # cyclone is ONE event that can escalate, not many events.
            key=f"gdacs:{event_type}:{event_id}",
            rank=rank,
            source="GDACS",
            emoji=emoji,
            title=f"{kind}{' — ' + country if country else ''}",
            message=(item.findtext("title") or "").strip(),
            link=(item.findtext("link") or "").strip(),
            colour=COLOUR_RED if rank == LEVEL_RANK["red"] else COLOUR_ORANGE,
            fields=fields,
            lat=to_float(item.findtext("geo:Point/geo:lat", namespaces=GDACS_NS)),
            lon=to_float(item.findtext("geo:Point/geo:long", namespaces=GDACS_NS)),
        ))
    return alerts


# ---------------------------------------------------------------------------
# DECIDING WHAT TO SEND
# ---------------------------------------------------------------------------

def decide(results, old_state):
    """Compare this run's alerts with last run's memory.

    results   : {source_name: list_of_alerts, or None if the fetch failed}
    old_state : {source_name: {alert_key: rank}} from state.json

    Returns (alerts_to_send, new_state).

    Kept free of any network or file access so it is easy to test.
    """
    to_send = []
    new_state = {}

    for source, alerts in results.items():
        previous = old_state.get(source, {})

        if alerts is None:
            # Fetch failed: remember exactly what we had, so a flaky feed
            # doesn't make every current event look "new" next time.
            new_state[source] = previous
            continue

        current = {}
        for alert in alerts:
            key, rank = alert["key"], alert["rank"]
            old_rank = previous.get(key)

            if old_rank is None or rank > old_rank:
                to_send.append(alert)

            # Remember the highest level ever seen, so a temporary drop
            # (Red -> Orange -> Red) doesn't re-notify.
            current[key] = max(rank, old_rank or 0)

        # Events that have left the feed are forgotten. If the same thing
        # flares up again later, that's a genuinely new alert.
        new_state[source] = current

    return to_send, new_state


# ---------------------------------------------------------------------------
# NOTIFYING (Discord)
# ---------------------------------------------------------------------------

def build_embed(alert):
    """Turn one alert into a Discord "embed" — the coloured card.

    Discord limits: title 256 characters, field values 1024. We trim to stay
    inside them, because Discord rejects the whole message otherwise.
    """
    embed = {
        "title": f"{alert['emoji']} {alert['title']}"[:256],
        "color": alert["colour"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": alert.get("source", "")},
    }
    if alert["message"]:
        embed["description"] = alert["message"][:2000]
    if alert["link"]:
        embed["url"] = alert["link"]

    fields = list(alert["fields"])
    if alert["lat"] is not None and alert["lon"] is not None:
        lat, lon = alert["lat"], alert["lon"]
        map_url = f"https://www.openstreetmap.org/?mlat={lat:.4f}&mlon={lon:.4f}#map=8/{lat:.4f}/{lon:.4f}"
        fields.append(("Map", f"[Open map]({map_url})"))

    # inline=True lets Discord place short fields side by side.
    embed["fields"] = [{"name": name, "value": str(value)[:1024], "inline": True}
                       for name, value in fields]
    return embed


def send_discord(alert):
    """Post one alert to the Discord channel behind the webhook.

    Discord allows only a few posts per second. If we go too fast it replies
    "429 Too Many Requests" and tells us how long to wait, so we wait and try
    once more.
    """
    payload = {"username": DISCORD_USERNAME, "embeds": [build_embed(alert)]}
    for attempt in range(2):
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
            if response.status_code == 429 and attempt == 0:
                wait = response.json().get("retry_after", 2)
                time.sleep(float(wait))
                continue
            response.raise_for_status()
            return
        except requests.RequestException as error:
            print(f"  ! Discord send failed: {error}")
            return


def deliver(to_send, dry_run):
    """Send the alerts, respecting MAX_PUSHES_PER_RUN."""
    first, overflow = to_send[:MAX_PUSHES_PER_RUN], to_send[MAX_PUSHES_PER_RUN:]

    if overflow:
        names = "\n".join(f"• {a['emoji']} {a['title']}" for a in overflow)
        first.append(make_alert(
            key="summary",
            source="Hazard Monitor",
            emoji="📋",
            title=f"+{len(overflow)} more alerts this check",
            message=names,
            colour=COLOUR_GREY,
        ))

    for alert in first:
        if dry_run:
            print(f"  [would send] {alert['emoji']} {alert['title']}")
            for name, value in alert["fields"]:
                print(f"               {name}: {value}")
        else:
            print(f"  sending: {alert['title']}")
            send_discord(alert)
            time.sleep(1)    # stay well under Discord's rate limit


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def collect():
    """Fetch and filter every source. Returns {source_name: alerts or None}."""
    results = {}

    for region, url in BOM_FEEDS.items():
        source = f"BoM {region}"
        content = fetch(url)
        results[source] = safe_parse(source, parse_bom, region, content)

    for feed in STATE_FEEDS:
        content = fetch(feed["url"])
        results[feed["name"]] = safe_parse(feed["name"], parse_state_geojson, feed, content)

    content = fetch(GDACS_URL)
    results["GDACS"] = safe_parse("GDACS", parse_gdacs, None, content)

    return results


def safe_parse(source, parser, arg, content):
    """Run a parser, turning a missing download or bad data into None."""
    if content is None:
        return None
    try:
        alerts = parser(content) if arg is None else parser(arg, content)
        print(f"  {source}: {len(alerts)} item(s) pass the filter")
        return alerts
    except (ET.ParseError, json.JSONDecodeError, AttributeError,
            KeyError, TypeError, ValueError) as error:
        print(f"  ! could not read {source}: {error}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Strict hazard alert monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending them")
    parser.add_argument("--test-message", action="store_true",
                        help="send one sample card to Discord, then stop")
    args = parser.parse_args()

    if not args.dry_run and not DISCORD_WEBHOOK_URL:
        sys.exit("Set the DISCORD_WEBHOOK_URL environment variable (or use --dry-run).")

    if args.test_message:
        send_discord(make_alert(
            key="test", source="Hazard Monitor (test)", emoji="✅",
            title="Hazard Monitor is connected",
            message="This is what an alert card looks like.",
            fields=[("Level", "Test"), ("Type", "Setup check")],
            lat=-37.8136, lon=144.9631,
        ))
        print("Test message sent.")
        return

    first_run = not STATE_FILE.exists()
    old_state = json.loads(STATE_FILE.read_text()) if not first_run else {}

    print("Checking feeds...")
    results = collect()
    to_send, new_state = decide(results, old_state)

    if args.dry_run:
        # Show what passes the filter, but don't touch state.json, so you can
        # dry-run as often as you like while tuning the rules.
        print(f"Dry run: {len(to_send)} alert(s) would be sent.")
        deliver(to_send, dry_run=True)
        return

    if first_run:
        # Without this, the very first run would post everything that's
        # currently active. Instead we quietly record it as "already seen".
        print(f"First run: recorded {len(to_send)} current alert(s) without notifying.")
    elif to_send:
        deliver(to_send, dry_run=False)
    else:
        print("Nothing new.")

    STATE_FILE.write_text(json.dumps(new_state, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
