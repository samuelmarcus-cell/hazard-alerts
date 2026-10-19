"""
Offline tests for monitor.py — no internet needed. Run with:

    python -m unittest test_monitor.py
"""

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import monitor

NOW = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)

BOM_SAMPLE = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Tropical Cyclone Warning for Cape York Peninsula</title>
        <link>http://www.bom.gov.au/products/IDQ20023.shtml</link></item>
  <item><title>Severe Thunderstorm Warning for Darling Downs</title>
        <link>http://www.bom.gov.au/products/IDQ21035.shtml</link></item>
  <item><title>Moderate to Major Flood Warning for the Fitzroy River</title>
        <link>http://www.bom.gov.au/products/IDQ20885.shtml</link></item>
  <item><title>Minor Flood Warning for the Logan River</title>
        <link>http://www.bom.gov.au/products/IDQ20950.shtml</link></item>
  <item><title>Cancellation of Tropical Cyclone Warning</title>
        <link>http://www.bom.gov.au/products/IDQ20024.shtml</link></item>
</channel></rss>"""

NSW_SAMPLE = json.dumps({"type": "FeatureCollection", "features": [
    {"properties": {"title": "OIL RIG RD", "category": "Advice", "guid": "nsw/1"}},
    {"properties": {"title": "WATCH RD", "category": "Watch and Act", "guid": "nsw/3"}},
    {"geometry": {"type": "GeometryCollection", "geometries": [
        {"type": "Point", "coordinates": [150.32, -35.48]}]},
     "properties": {"title": "BIG FIRE RD", "category": "Emergency Warning", "guid": "nsw/2",
                    "link": "https://www.rfs.nsw.gov.au/fire-information/fires-near-me",
                    "description": "ALERT LEVEL: Emergency Warning <br />STATUS: Out of control"
                                   " <br />SIZE: 5200 ha <br />UPDATED: 19 Oct 2026 14:00"}},
]}).encode()

# WA style: EDXL wrapper, level only in the headline, CAD_ID parameter.
WA_SAMPLE = b"""<?xml version="1.0"?>
<edxlde:EDXLDistribution xmlns:edxlde="urn:oasis:names:tc:emergency:EDXL:DE:1.0"
    xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2"><edxlde:contentObject>
 <cap:alert><cap:identifier>a1</cap:identifier><cap:info><cap:event>Bushfire</cap:event>
   <cap:severity>Minor</cap:severity>
   <cap:headline>Bushfire Advice MONITOR CONDITIONS - GIBB</cap:headline>
   <cap:description>This could become an Emergency Warning.</cap:description>
   <cap:parameter><cap:valueName>CAD_ID</cap:valueName><cap:value>824261</cap:value></cap:parameter>
   <cap:area><cap:areaDesc>GIBB</cap:areaDesc><cap:circle>-16.5,126.2 0.0</cap:circle></cap:area>
 </cap:info></cap:alert>
 <cap:alert><cap:identifier>a2</cap:identifier><cap:info><cap:event>Bushfire</cap:event>
   <cap:severity>Extreme</cap:severity>
   <cap:headline>Bushfire Emergency Warning - TOODYAY</cap:headline>
   <cap:web>https://emergency.wa.gov.au/warnings/a2</cap:web>
   <cap:parameter><cap:valueName>CAD_ID</cap:valueName><cap:value>900001</cap:value></cap:parameter>
   <cap:area><cap:areaDesc>TOODYAY</cap:areaDesc><cap:circle>-31.55,116.47 0.0</cap:circle></cap:area>
 </cap:info></cap:alert>
</edxlde:contentObject></edxlde:EDXLDistribution>"""

# TAS/ACT style: explicit AlertLevel parameter.
TAS_SAMPLE = b"""<?xml version="1.0"?>
<EDXLDistribution xmlns="urn:oasis:names:tc:emergency:EDXL:DE:1.0">
 <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>t1</identifier><info>
   <event>Bushfire</event><severity>Moderate</severity>
   <headline>Bushfire Advice - Kingston</headline>
   <parameter><valueName>AlertLevel</valueName><value>Advice</value></parameter>
   <area><areaDesc>Kingston</areaDesc></area></info></alert>
 <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>t2-87918</identifier><info>
   <event>Bushfire</event><severity>Severe</severity>
   <headline>Bushfire - Dolphin Sands - Leave now</headline>
   <parameter><valueName>AlertLevel</valueName><value>Emergency Warning</value></parameter>
   <area><areaDesc>Dolphin Sands</areaDesc><circle>-42.1,148.1 10</circle></area></info></alert>
 <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>t3</identifier><info>
   <event>Other Urgent Alerts</event><severity>Unknown</severity>
   <headline>AMBULANCE RESPONSE - PAGE</headline>
   <parameter><valueName>AlertLevel</valueName><value>N/A</value></parameter>
   <area><areaDesc>PAGE</areaDesc></area></info></alert>
</EDXLDistribution>"""

GDACS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"
     xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel>
  <item><title>Orange alert for tropical cyclone X</title><link>https://gdacs.org/1</link>
    <gdacs:eventtype>TC</gdacs:eventtype><gdacs:alertlevel>Orange</gdacs:alertlevel>
    <gdacs:eventid>1001</gdacs:eventid><gdacs:country>Philippines</gdacs:country>
    <gdacs:severity>Typhoon (maximum wind speed of 185 km/h)</gdacs:severity>
    <geo:Point><geo:lat>14.5</geo:lat><geo:long>124.2</geo:long></geo:Point></item>
  <item><title>Orange drought</title><gdacs:eventtype>DR</gdacs:eventtype>
    <gdacs:alertlevel>Orange</gdacs:alertlevel><gdacs:eventid>2002</gdacs:eventid></item>
  <item><title>Green flood</title><gdacs:eventtype>FL</gdacs:eventtype>
    <gdacs:alertlevel>Green</gdacs:alertlevel><gdacs:eventid>3003</gdacs:eventid></item>
  <item><title>Orange earthquake</title><gdacs:eventtype>EQ</gdacs:eventtype>
    <gdacs:alertlevel>Orange</gdacs:alertlevel><gdacs:eventid>4004</gdacs:eventid></item>
  <item><title>Green wildfire</title><gdacs:eventtype>WF</gdacs:eventtype>
    <gdacs:alertlevel>Green</gdacs:alertlevel><gdacs:eventid>6006</gdacs:eventid></item>
  <item><title>Red earthquake</title><gdacs:eventtype>EQ</gdacs:eventtype>
    <gdacs:alertlevel>Red</gdacs:alertlevel><gdacs:eventid>5005</gdacs:eventid></item>
</channel></rss>"""

CANADA_SAMPLE = json.dumps({"type": "FeatureCollection", "features": [
    {"properties": {"national_fire_id": "2026_BC_G1", "agency_code": "BC",
                    "agency_fire_id": "G1", "fire_size": 64000.5,
                    "stage_of_control_status": "OC", "latitude": 57.1, "longitude": -122.3}},
    {"properties": {"national_fire_id": "2026_AB_X", "agency_code": "AB",
                    "fire_size": 312000, "stage_of_control_status": "BH",
                    "latitude": 58.0, "longitude": -116.0}},
    {"properties": {"national_fire_id": "mid", "agency_code": "ON", "fire_size": 20000}},
    {"properties": {"national_fire_id": "small", "agency_code": "ON", "fire_size": 12}},
]}).encode()

US_SAMPLE = json.dumps({"type": "FeatureCollection", "features": [
    {"geometry": {"type": "Point", "coordinates": [-120.8, 48.0]},
     "properties": {"IncidentName": "LITTLE GIANT", "UniqueFireIdentifier": "2026-WAOWF-1",
                    "IncidentSize": 172912, "PercentContained": 95, "POOState": "US-WA"}},
    {"geometry": {"type": "Point", "coordinates": [-119.0, 48.1]},
     "properties": {"IncidentName": "SMALLISH", "UniqueFireIdentifier": "2026-X",
                    "IncidentSize": 20000, "PercentContained": 10, "POOState": "US-WA"}},
]}).encode()

ABC_SAMPLE = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Interstate crews arrive as bushfire threatens Gippsland towns</title>
    <link>https://www.abc.net.au/news/1</link><guid>abc-1</guid>
    <description>&lt;p&gt;More than 200 interstate firefighters have arrived.&lt;/p&gt;</description></item>
  <item><title>Defence force budget announced</title><link>https://www.abc.net.au/news/2</link>
    <guid>abc-2</guid><description>The defence force will get new ships.</description></item>
  <item><title>Footy finals preview</title><link>https://www.abc.net.au/news/3</link>
    <guid>abc-3</guid><description>Who will win?</description></item>
</channel></rss>"""


def alert(key, rank=1, track_end=True):
    return monitor.make_alert(key=key, rank=rank, level=f"L{rank}", source="s",
                              title=key, track_end=track_end)


class ParserTests(unittest.TestCase):

    def test_bom_tiers(self):
        alerts = monitor.parse_bom("QLD", BOM_SAMPLE)
        # cyclone + major flood get cards; thunderstorm is recorded only;
        # minor flood and the cancellation are dropped.
        self.assertEqual([(a["emoji"], a["notify"]) for a in alerts],
                         [("🌀", True), ("⛈️", False), ("🌊", True)])

    def test_nsw_levels(self):
        alerts = monitor.parse_state_geojson(monitor.STATE_FEEDS[0], NSW_SAMPLE)
        self.assertEqual([(a["key"], a["notify"]) for a in alerts],
                         [("NSW RFS:nsw/3", False), ("NSW RFS:nsw/2", True)])
        ew = alerts[1]
        self.assertEqual((ew["lat"], ew["lon"]), (-35.48, 150.32))
        self.assertIn(("Size", "5200 ha"), ew["fields"])
        self.assertTrue(alerts[0]["title"].startswith("Watch and Act"))

    def test_empty_geojson_feed(self):
        self.assertEqual(monitor.parse_state_geojson(monitor.STATE_FEEDS[1], b" "), [])

    def test_wa_cap_uses_headline_not_description(self):
        feed = {"name": "WA DFES", "id_param": "CAD_ID"}
        alerts = monitor.parse_cap(feed, WA_SAMPLE)
        # The Advice mentions "Emergency Warning" only in its description: ignored.
        self.assertEqual([a["key"] for a in alerts], ["WA DFES:900001"])
        self.assertEqual((alerts[0]["lat"], alerts[0]["lon"]), (-31.55, 116.47))
        self.assertEqual(alerts[0]["link"], "https://emergency.wa.gov.au/warnings/a2")

    def test_tas_cap_uses_alert_level_parameter(self):
        feed = {"name": "TasALERT", "id_param": None}
        alerts = monitor.parse_cap(feed, TAS_SAMPLE)
        self.assertEqual([a["key"] for a in alerts], ["TasALERT:Bushfire|Dolphin Sands"])

    def test_act_identifier_key(self):
        feed = {"name": "ACT ESA", "id_param": "identifier"}
        alerts = monitor.parse_cap(feed, TAS_SAMPLE)
        self.assertEqual([a["key"] for a in alerts], ["ACT ESA:t2-87918"])

    def test_cap_atom_entries(self):
        sample = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>sa-2</id>
            <title>Emergency Warning - Big Scrub</title></entry>
            <entry><id>sa-1</id><title>Advice - Quiet Rd</title></entry></feed>"""
        alerts = monitor.parse_cap({"name": "SA CFS", "id_param": None}, sample)
        self.assertEqual([a["key"] for a in alerts], ["SA CFS:sa-2"])

    def test_gdacs_rules(self):
        alerts = monitor.parse_gdacs(GDACS_SAMPLE)
        cards = {a["key"] for a in alerts if a["notify"]}
        recorded = {a["key"] for a in alerts if not a["notify"]}
        self.assertEqual(cards, {"gdacs:TC:1001", "gdacs:EQ:5005"})
        self.assertEqual(recorded, {"gdacs:FL:3003", "gdacs:EQ:4004"})   # no drought
        cyclone = next(a for a in alerts if a["key"] == "gdacs:TC:1001")
        self.assertEqual((cyclone["lat"], cyclone["lon"], cyclone["zoom"]), (14.5, 124.2, 4))

    def test_canada_size_tiers(self):
        alerts = {a["key"]: a for a in monitor.parse_canada(CANADA_SAMPLE)}
        self.assertEqual(set(alerts), {"ca:2026_BC_G1", "ca:2026_AB_X", "ca:mid"})
        self.assertEqual((alerts["ca:mid"]["rank"], alerts["ca:mid"]["notify"]), (0, False))
        self.assertEqual(alerts["ca:2026_BC_G1"]["rank"], 1)
        self.assertEqual(alerts["ca:2026_AB_X"]["rank"], 2)
        self.assertIn("British Columbia", alerts["ca:2026_BC_G1"]["title"])
        self.assertIn(("Status", "Out of control"), alerts["ca:2026_BC_G1"]["fields"])

    def test_us_acres_converted(self):
        alerts = monitor.parse_us(US_SAMPLE)
        self.assertEqual([a["key"] for a in alerts], ["us:2026-WAOWF-1"])
        self.assertIn(("Size", "69,975 ha"), alerts[0]["fields"])
        self.assertIn("WA, USA", alerts[0]["title"])

    def test_abc_needs_deployment_and_hazard(self):
        alerts = monitor.parse_abc(ABC_SAMPLE)
        self.assertEqual([a["key"] for a in alerts], ["abc:abc-1"])
        self.assertFalse(alerts[0]["track_end"])
        self.assertNotIn("<p>", alerts[0]["message"])


class DecideTests(unittest.TestCase):

    def test_first_read_of_a_source_is_silent(self):
        changes, _ = monitor.decide({"GDACS": [alert("a")]}, {}, NOW)
        self.assertEqual([c[0] for c in changes], ["seen_at_start"])

    def test_new_alert(self):
        changes, _ = monitor.decide({"GDACS": [alert("a")]}, {"GDACS": {}}, NOW)
        self.assertEqual([c[0] for c in changes], ["new"])

    def test_repeat_is_quiet(self):
        _, state = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW)
        changes, _ = monitor.decide({"G": [alert("a")]}, state, NOW)
        self.assertEqual(changes, [])

    def test_escalation_keeps_message_id(self):
        _, state = monitor.decide({"G": [alert("a", 1)]}, {"G": {}}, NOW)
        state["G"]["a"]["message_id"] = "123"
        changes, state = monitor.decide({"G": [alert("a", 2)]}, state, NOW)
        self.assertEqual([c[0] for c in changes], ["escalated"])
        self.assertEqual(state["G"]["a"]["message_id"], "123")
        self.assertEqual(len(state["G"]["a"]["history"]), 2)

    def test_peak_is_remembered(self):
        _, state = monitor.decide({"G": [alert("a", 2)]}, {"G": {}}, NOW)
        _, state = monitor.decide({"G": [alert("a", 1)]}, state, NOW)
        changes, _ = monitor.decide({"G": [alert("a", 2)]}, state, NOW)
        self.assertEqual(changes, [])

    def test_failed_fetch_changes_nothing(self):
        _, state = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW)
        changes, after = monitor.decide({"G": None}, state, NOW)
        self.assertEqual(changes, [])
        self.assertEqual(after, state)

    def test_ended(self):
        _, state = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW)
        changes, after = monitor.decide({"G": []}, state, NOW)
        self.assertEqual([c[0] for c in changes], ["ended"])
        self.assertEqual(after["G"], {})

    def test_news_does_not_end(self):
        _, state = monitor.decide({"N": [alert("a", track_end=False)]}, {"N": {}}, NOW)
        changes, _ = monitor.decide({"N": []}, state, NOW)
        self.assertEqual(changes, [])


class HealthTests(unittest.TestCase):

    def test_warns_once_after_a_day_then_recovers(self):
        cards, health = monitor.update_health({"F": None}, {}, NOW)
        self.assertEqual(cards, [])
        cards, health = monitor.update_health({"F": None}, health, NOW + timedelta(hours=23))
        self.assertEqual(cards, [])
        cards, health = monitor.update_health({"F": None}, health, NOW + timedelta(hours=24))
        self.assertEqual([c["title"] for c in cards], ["Feed not working: F"])
        cards, health = monitor.update_health({"F": None}, health, NOW + timedelta(hours=30))
        self.assertEqual(cards, [])                      # only warned once
        cards, health = monitor.update_health({"F": []}, health, NOW + timedelta(hours=31))
        self.assertEqual([c["title"] for c in cards], ["Feed working again: F"])
        self.assertEqual(health["F"], {"failing_since": None, "warned": False})

    def test_short_outage_is_silent(self):
        _, health = monitor.update_health({"F": None}, {}, NOW)
        cards, _ = monitor.update_health({"F": []}, health, NOW + timedelta(hours=2))
        self.assertEqual(cards, [])


class FileTests(unittest.TestCase):

    def test_log_writes_header_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            changes, _ = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW)
            monitor.log_changes(changes, NOW, path)
            monitor.log_changes(changes, NOW, path)
            with path.open() as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], monitor.LOG_COLUMNS)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][1:4], ["new", "G", "a"])

    def test_old_state_is_upgraded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"GDACS": {"gdacs:TC:1": 2}, "BoM VIC": {}}))
            state = monitor.load_state(path)
            self.assertEqual(state["version"], 2)
            self.assertEqual(state["sources"]["GDACS"]["gdacs:TC:1"]["rank"], 2)
            self.assertEqual(state["sources"]["BoM VIC"], {})


class DiscordTests(unittest.TestCase):

    def test_embed(self):
        cyclone = monitor.parse_gdacs(GDACS_SAMPLE)[0]
        embed = monitor.build_embed(cyclone, history=["Orange · x", "Red · y"])
        self.assertTrue(embed["title"].startswith("🌀"))
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("History", names)
        self.assertEqual(names[-1], "Map")

    def test_map_gives_up_quietly_without_tiles(self):
        original = monitor.fetch
        monitor.fetch = lambda url: None
        try:
            self.assertIsNone(monitor.render_map(-37.8, 145.0, 7))
        finally:
            monitor.fetch = original

    def test_overflow_is_summarised(self):
        posted = []
        original_post, original_render, original_sleep = (
            monitor.post_card, monitor.render_map, monitor.time.sleep)
        monitor.post_card = lambda embed, image=None, thread=None: posted.append(embed) or ("m", None)
        monitor.render_map = lambda *a: None
        monitor.time.sleep = lambda s: None
        try:
            alerts = [alert(f"k{i}") for i in range(8)]
            changes, _ = monitor.decide({"G": alerts}, {"G": {}}, NOW)
            monitor.deliver(changes, [], NOW)
        finally:
            monitor.post_card, monitor.render_map, monitor.time.sleep = (
                original_post, original_render, original_sleep)
        self.assertEqual(len(posted), monitor.MAX_PUSHES_PER_RUN + 1)
        self.assertIn("+3 more", posted[-1]["title"])


NSW_FDR_SAMPLE = b"""<?xml version="1.0"?><FireDangerMap>
  <District><Name>Greater Sydney Region</Name><DangerLevelToday>HIGH</DangerLevelToday>
    <DangerLevelTomorrow>EXTREME</DangerLevelTomorrow></District>
  <District><Name>Far South Coast</Name><DangerLevelToday>MODERATE</DangerLevelToday>
    <DangerLevelTomorrow>CATASTROPHIC</DangerLevelTomorrow></District>
  <District><Name>North Coast</Name><DangerLevelToday>MODERATE</DangerLevelToday>
    <DangerLevelTomorrow>HIGH</DangerLevelTomorrow></District>
</FireDangerMap>"""

VIC_FDR_SAMPLE = json.dumps({"type": "FeatureCollection", "features": [], "properties": {
    "conditions": {"forecasts": [
        {"date": "2026-10-18T13:00:00.000Z", "fdr": {"Mallee": "EXTREME", "Central": "HIGH"}},
        {"date": "2026-10-19T13:00:00.000Z", "fdr": {"Mallee": "HIGH"}},
        {"date": "2026-10-17T13:00:00.000Z", "fdr": {"Mallee": "CATASTROPHIC"}},
    ]}}}).encode()

TODAY = NOW.astimezone(monitor.LOCAL_TZ).date()       # Mon 19 Oct 2026


class FireDangerTests(unittest.TestCase):

    def test_nsw(self):
        alerts = monitor.parse_nsw_fdr(NSW_FDR_SAMPLE, TODAY)
        self.assertEqual(len(alerts), 1)                    # only tomorrow is bad
        a = alerts[0]
        self.assertEqual(a["key"], "fdr:NSW:2026-10-20")
        self.assertEqual((a["rank"], a["level"]), (2, "Catastrophic"))
        self.assertIn(("Catastrophic", "Far South Coast"), a["fields"])
        self.assertIn(("Extreme", "Greater Sydney Region"), a["fields"])
        self.assertFalse(a["track_end"])

    def test_vic_dates_are_local_and_past_days_skipped(self):
        alerts = monitor.parse_vic_fdr(VIC_FDR_SAMPLE, TODAY)
        self.assertEqual([a["key"] for a in alerts], ["fdr:VIC:2026-10-19"])
        self.assertEqual(alerts[0]["level"], "Extreme")


def record(title, rank=1, notify=True, lat=None, lon=None):
    return {"key": title, "title": title, "rank": rank, "notify": notify,
            "level": "Emergency Warning", "lat": lat, "lon": lon, "link": "",
            "colour": monitor.COLOUR_RED, "emoji": "🔥"}


class ConcurrentTests(unittest.TestCase):

    def test_three_jurisdictions(self):
        sources = {"NSW RFS": {"a": record("A")}, "VicEmergency": {"b": record("B")},
                   "BoM QLD": {"c": record("C")}, "TasALERT": {"w": record("W", 0, False)}}
        alerts = monitor.concurrent_alerts(sources)
        self.assertEqual([a["key"] for a in alerts], ["concurrent:australia"])
        self.assertIn("3 states", alerts[0]["title"])

    def test_two_plus_overseas_red(self):
        sources = {"NSW RFS": {"a": record("A")}, "WA DFES": {"b": record("B")},
                   "GDACS": {"g": record("Cyclone", rank=2)}}
        self.assertEqual([a["key"] for a in monitor.concurrent_alerts(sources)],
                         ["concurrent:global"])

    def test_quiet_when_not_overlapping(self):
        sources = {"NSW RFS": {"a": record("A")}, "GDACS": {"g": record("C", rank=1)}}
        self.assertEqual(monitor.concurrent_alerts(sources), [])


class WatchAndActTests(unittest.TestCase):

    def test_escalation_to_emergency_warning_posts_a_card(self):
        watch = monitor.make_alert(key="k", rank=0, notify=False, level="Watch and Act",
                                   source="s", title="Fire")
        ew = monitor.make_alert(key="k", rank=1, notify=True, level="Emergency Warning",
                                source="s", title="Fire")
        changes, state = monitor.decide({"NSW RFS": [watch]}, {"NSW RFS": {}}, NOW)
        self.assertFalse(changes[0][2]["notify"])
        changes, state = monitor.decide({"NSW RFS": [ew]}, state, NOW)
        self.assertEqual(changes[0][0], "escalated")
        posted = []
        saved = monitor.post_card, monitor.render_map, monitor.time.sleep
        monitor.post_card = lambda e, image=None, thread=None: posted.append(e) or ("m1", None)
        monitor.render_map = lambda *a: None
        monitor.time.sleep = lambda s: None
        try:
            monitor.deliver(changes, [], NOW)
        finally:
            monitor.post_card, monitor.render_map, monitor.time.sleep = saved
        self.assertEqual(len(posted), 1)                 # a full card, not "escalated"
        self.assertEqual(state["NSW RFS"]["k"]["message_id"], "m1")

    def test_seed_all_records_quietly(self):
        changes, _ = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW, seed_all=True)
        self.assertEqual(changes[0][0], "seen_at_start")

    def test_narrowed_filter_does_not_log_ends(self):
        _, state = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW)
        changes, after = monitor.decide({"G": []}, state, NOW, seed_all=True)
        self.assertEqual(changes, [])
        self.assertEqual(after["G"], {})


class DigestDashboardWeeklyTests(unittest.TestCase):

    def test_log_digest(self):
        changes, _ = monitor.decide({"G": [alert("a"), alert("b", track_end=True)]},
                                    {"G": {}}, NOW)
        text = monitor.log_digest(changes, NOW)
        self.assertIn("🆕", text)
        self.assertEqual(text.count("\n"), 2)
        start, _ = monitor.decide({"G": [alert("a")]}, {}, NOW)
        self.assertIn("1 event(s) already active", monitor.log_digest(start, NOW))
        self.assertEqual(monitor.log_digest([], NOW), "")

    def test_dashboard(self):
        sources = {"NSW RFS": {"a": record("Big fire", lat=-33.0, lon=150.0),
                               "w": dict(record("Small", 0, False), level="Watch and Act")},
                   "GDACS": {"g": dict(record("Cyclone", 2, lat=14.0, lon=124.0), link="https://x")},
                   "Fire danger (NSW)": {"f": record("Extreme fire danger")},
                   "ABC News": {"n": record("Story")}}
        results = {"NSW RFS": [], "GDACS": [], "US fires": None}
        embed, markers = monitor.dashboard_embed(sources, results, NOW)
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("🇦🇺 Australia · 1 major · 1 Watch and Act", names)
        self.assertIn("🌏 International · 1 major", names)
        values = " ".join(f["value"] for f in embed["fields"])
        self.assertIn("[Cyclone](https://x)", values)
        self.assertIn("Extreme fire danger", values)
        self.assertNotIn("Story", values)                 # news isn't "active"
        self.assertIn("not responding: US fires", values)
        self.assertEqual(len(markers), 2)
        self.assertEqual(embed["color"], monitor.COLOUR_RED)

    def test_weekly_due_once_per_week(self):
        state = {}
        sunday = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)      # Sun 3pm AEDT
        monday_7am = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)  # Mon 7am
        monday_9am = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)  # Mon 9am
        state["last_weekly"] = monitor.weekly_due({}, sunday).isoformat()  # last week's
        self.assertIsNone(monitor.weekly_due(state, sunday))
        self.assertIsNone(monitor.weekly_due(state, monday_7am))
        due = monitor.weekly_due(state, monday_9am)
        self.assertIsNotNone(due)
        state["last_weekly"] = due.isoformat()
        self.assertIsNone(monitor.weekly_due(state, monday_9am + timedelta(hours=5)))

    def test_weekly_embed(self):
        rows = [
            {"time_utc": "2026-10-18T04:00:00Z", "change": "new", "source": "NSW RFS",
             "title": "Big fire", "link": "https://x", "major": "yes"},
            {"time_utc": "2026-10-18T05:00:00Z", "change": "new", "source": "BoM QLD",
             "title": "Storm", "link": "", "major": "no"},
            {"time_utc": "2026-10-18T06:00:00Z", "change": "ended", "source": "NSW RFS",
             "title": "Big fire", "link": "", "major": "yes"},
        ]
        embed = monitor.weekly_embed(rows, {"a": [], "b": None}, NOW)
        self.assertIn("**1** major events", embed["description"])
        self.assertIn("**2** events recorded", embed["description"])
        self.assertIn("[Big fire](https://x)", embed["fields"][0]["value"])
        self.assertIn("Not responding now: b", embed["fields"][2]["value"])

    def test_log_gains_new_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            path.write_text("time_utc,change,source,key,level,title,lat,lon,link\n"
                            "2026-10-19T04:25:52Z,seen_at_start,US fires,k,x,t,,,\n")
            changes, _ = monitor.decide({"G": [alert("a")]}, {"G": {}}, NOW)
            monitor.log_changes(changes, NOW, path)
            with path.open() as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], monitor.LOG_COLUMNS)
            self.assertEqual(rows[1][-1], "")
            self.assertEqual(rows[2][-1], "yes")

    def test_dashboard_map_renders(self):
        import io
        from PIL import Image
        def fake(url):
            b = io.BytesIO()
            Image.new("RGB", (256, 256), (170, 210, 170)).save(b, "PNG")
            return b.getvalue()
        saved = monitor.fetch
        monitor.fetch = fake
        try:
            png = monitor.render_dashboard_map([(-33.0, 150.0, (211, 47, 47)),
                                                (14.0, 124.0, (245, 124, 0))])
        finally:
            monitor.fetch = saved
        self.assertEqual(Image.open(io.BytesIO(png)).size, (960, 300))


if __name__ == "__main__":
    unittest.main()
