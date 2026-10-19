"""
Offline tests for monitor.py — no internet needed.

Each test feeds a small, hand-made sample (shaped like the real feeds) through
the filters and checks the result. Run with:

    python -m unittest test_monitor.py
"""

import json
import unittest

import monitor


BOM_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>19/14:05 EST Tropical Cyclone Warning for Cape York Peninsula</title>
        <link>http://www.bom.gov.au/products/IDQ20023.shtml</link></item>
  <item><title>19/14:10 EST Severe Thunderstorm Warning for Darling Downs</title>
        <link>http://www.bom.gov.au/products/IDQ21035.shtml</link></item>
  <item><title>19/14:20 EST Moderate to Major Flood Warning for the Fitzroy River</title>
        <link>http://www.bom.gov.au/products/IDQ20885.shtml</link></item>
  <item><title>19/15:00 EST Minor Flood Warning for the Logan River</title>
        <link>http://www.bom.gov.au/products/IDQ20950.shtml</link></item>
  <item><title>19/16:00 EST Cancellation of Tropical Cyclone Warning</title>
        <link>http://www.bom.gov.au/products/IDQ20024.shtml</link></item>
</channel></rss>"""

NSW_SAMPLE = json.dumps({"type": "FeatureCollection", "features": [
    {"properties": {"title": "OIL RIG RD, GIBBERAGEE", "category": "Advice",
                    "guid": "https://incidents.rfs.nsw.gov.au/api/v1/incidents/1",
                    "link": "https://www.rfs.nsw.gov.au/fire-information/fires-near-me"}},
    {"geometry": {"type": "GeometryCollection", "geometries": [
        {"type": "Point", "coordinates": [150.32, -35.48]},
        {"type": "GeometryCollection", "geometries": []}]},
     "properties": {"title": "BIG FIRE RD, SOMEWHERE", "category": "Emergency Warning",
                    "guid": "https://incidents.rfs.nsw.gov.au/api/v1/incidents/2",
                    "link": "https://www.rfs.nsw.gov.au/fire-information/fires-near-me",
                    "description": "ALERT LEVEL: Emergency Warning <br />LOCATION: SOMEWHERE 2539 "
                                   "<br />STATUS: Out of control <br />TYPE: Bush Fire "
                                   "<br />SIZE: 5200 ha <br />UPDATED: 19 Oct 2026 14:00"}},
]}).encode()

VIC_SAMPLE = json.dumps({"type": "FeatureCollection", "features": [
    {"properties": {"feedType": "incident", "sourceTitle": "Swallow Cr",
                    "id": "ESTA:1", "category1": "Fire", "status": "Safe"}},
    {"properties": {"feedType": "warning", "sourceTitle": "Grampians fire",
                    "id": "W-99", "category1": "Emergency Warning"}},
]}).encode()

GDACS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"
     xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel>
  <item><title>Orange alert for tropical cyclone X</title><link>https://gdacs.org/1</link>
    <gdacs:eventtype>TC</gdacs:eventtype><gdacs:alertlevel>Orange</gdacs:alertlevel>
    <gdacs:eventid>1001</gdacs:eventid><gdacs:country>Philippines</gdacs:country>
    <gdacs:severity unit="km/h" value="185">Typhoon (maximum wind speed of 185 km/h)</gdacs:severity>
    <geo:Point><geo:lat>14.5</geo:lat><geo:long>124.2</geo:long></geo:Point></item>
  <item><title>Orange drought</title><link>https://gdacs.org/2</link>
    <gdacs:eventtype>DR</gdacs:eventtype><gdacs:alertlevel>Orange</gdacs:alertlevel>
    <gdacs:eventid>2002</gdacs:eventid></item>
  <item><title>Green flood</title><link>https://gdacs.org/3</link>
    <gdacs:eventtype>FL</gdacs:eventtype><gdacs:alertlevel>Green</gdacs:alertlevel>
    <gdacs:eventid>3003</gdacs:eventid></item>
  <item><title>Orange earthquake</title><link>https://gdacs.org/4</link>
    <gdacs:eventtype>EQ</gdacs:eventtype><gdacs:alertlevel>Orange</gdacs:alertlevel>
    <gdacs:eventid>4004</gdacs:eventid></item>
  <item><title>Red earthquake</title><link>https://gdacs.org/5</link>
    <gdacs:eventtype>EQ</gdacs:eventtype><gdacs:alertlevel>Red</gdacs:alertlevel>
    <gdacs:eventid>5005</gdacs:eventid></item>
</channel></rss>"""


class FilterTests(unittest.TestCase):

    def test_bom_keeps_only_top_tier(self):
        alerts = monitor.parse_bom("QLD", BOM_SAMPLE)
        self.assertEqual(len(alerts), 2)
        self.assertIn("Tropical Cyclone Warning", alerts[0]["title"])
        self.assertEqual(alerts[0]["emoji"], "🌀")
        self.assertIn("Moderate to Major Flood", alerts[1]["title"])
        self.assertEqual(alerts[1]["emoji"], "🌊")
        # thunderstorm, minor flood and the cancellation are all dropped

    def test_nsw_keeps_only_emergency_warning(self):
        feed = monitor.STATE_FEEDS[0]                    # NSW RFS
        alerts = monitor.parse_state_geojson(feed, NSW_SAMPLE)
        self.assertEqual([a["key"] for a in alerts],
                         ["NSW RFS:https://incidents.rfs.nsw.gov.au/api/v1/incidents/2"])
        alert = alerts[0]
        # Point dug out of the nested GeometryCollection, lat/lon swapped round
        self.assertEqual((alert["lat"], alert["lon"]), (-35.48, 150.32))
        # Useful parts of the description become card fields; others ignored
        self.assertIn(("Status", "Out of control"), alert["fields"])
        self.assertIn(("Size", "5200 ha"), alert["fields"])
        self.assertNotIn("Updated", [name for name, _ in alert["fields"]])

    def test_vic_searches_all_fields(self):
        feed = monitor.STATE_FEEDS[1]                    # VicEmergency
        alerts = monitor.parse_state_geojson(feed, VIC_SAMPLE)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Grampians fire", alerts[0]["title"])

    def test_gdacs_rules(self):
        keys = {a["key"] for a in monitor.parse_gdacs(GDACS_SAMPLE)}
        # Orange cyclone: yes. Drought: never. Green flood: no.
        # Orange quake: no (non-weather needs Red). Red quake: yes.
        self.assertEqual(keys, {"gdacs:TC:1001", "gdacs:EQ:5005"})

    def test_gdacs_card_details(self):
        cyclone = monitor.parse_gdacs(GDACS_SAMPLE)[0]
        self.assertEqual((cyclone["lat"], cyclone["lon"]), (14.5, 124.2))
        self.assertEqual(cyclone["colour"], monitor.COLOUR_ORANGE)
        self.assertIn(("Country", "Philippines"), cyclone["fields"])
        self.assertIn("185 km/h", dict(cyclone["fields"])["Severity"])


class EmbedTests(unittest.TestCase):

    def test_embed_has_map_link_and_colour(self):
        alert = monitor.parse_gdacs(GDACS_SAMPLE)[0]
        embed = monitor.build_embed(alert)
        self.assertTrue(embed["title"].startswith("🌀"))
        self.assertEqual(embed["color"], monitor.COLOUR_ORANGE)
        self.assertEqual(embed["url"], "https://gdacs.org/1")
        map_field = embed["fields"][-1]
        self.assertEqual(map_field["name"], "Map")
        self.assertIn("openstreetmap.org", map_field["value"])

    def test_embed_without_location_has_no_map(self):
        alert = monitor.parse_bom("QLD", BOM_SAMPLE)[0]
        names = [f["name"] for f in monitor.build_embed(alert)["fields"]]
        self.assertNotIn("Map", names)

    def test_long_title_is_trimmed(self):
        alert = monitor.make_alert(key="x", source="s", title="A" * 500)
        self.assertLessEqual(len(monitor.build_embed(alert)["title"]), 256)


class DecideTests(unittest.TestCase):

    def alert(self, key, rank):
        return monitor.make_alert(key=key, rank=rank, source="s", title=key)

    def test_new_alert_is_sent(self):
        sent, _ = monitor.decide({"GDACS": [self.alert("a", 1)]}, {})
        self.assertEqual(len(sent), 1)

    def test_repeat_is_quiet(self):
        old = {"GDACS": {"a": 1}}
        sent, _ = monitor.decide({"GDACS": [self.alert("a", 1)]}, old)
        self.assertEqual(sent, [])

    def test_escalation_is_sent(self):
        old = {"GDACS": {"a": 1}}                        # was Orange
        sent, state = monitor.decide({"GDACS": [self.alert("a", 2)]}, old)
        self.assertEqual(len(sent), 1)                   # now Red -> notify
        self.assertEqual(state["GDACS"]["a"], 2)

    def test_downgrade_then_reescalate_is_quiet(self):
        old = {"GDACS": {"a": 2}}                        # was Red
        _, state = monitor.decide({"GDACS": [self.alert("a", 1)]}, old)
        self.assertEqual(state["GDACS"]["a"], 2)         # remembers the peak
        sent, _ = monitor.decide({"GDACS": [self.alert("a", 2)]}, state)
        self.assertEqual(sent, [])

    def test_failed_fetch_keeps_memory(self):
        old = {"GDACS": {"a": 1}}
        sent, state = monitor.decide({"GDACS": None}, old)
        self.assertEqual(sent, [])
        self.assertEqual(state, old)

    def test_finished_event_is_forgotten(self):
        old = {"GDACS": {"a": 1}}
        _, state = monitor.decide({"GDACS": []}, old)
        self.assertEqual(state["GDACS"], {})


class DeliverTests(unittest.TestCase):

    def test_overflow_is_summarised(self):
        sent = []
        original_send, original_sleep = monitor.send_discord, monitor.time.sleep
        monitor.send_discord = sent.append              # capture instead of sending
        monitor.time.sleep = lambda seconds: None       # don't actually wait
        try:
            many = [monitor.make_alert(key=f"k{i}", source="s", title=f"t{i}")
                    for i in range(8)]
            monitor.deliver(many, dry_run=False)
        finally:
            monitor.send_discord, monitor.time.sleep = original_send, original_sleep
        self.assertEqual(len(sent), monitor.MAX_PUSHES_PER_RUN + 1)
        self.assertIn("+3 more", sent[-1]["title"])


if __name__ == "__main__":
    unittest.main()
