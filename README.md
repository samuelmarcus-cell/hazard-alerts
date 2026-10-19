# Hazard alerts

A small service that checks public warning feeds every 15 minutes and posts a
card to a Discord channel **only for big events**. Each card is coloured by
severity (red or orange), starts with an emoji for the hazard type, lists the
key details, and includes a map link when the feed gives a location.

| Source | What counts as "big" |
|---|---|
| BoM warnings, every state | Tropical cyclone, major flood, extreme heatwave warnings |
| NSW RFS, VicEmergency, QLD Fire, SA CFS | Anything at **Emergency Warning** level |
| GDACS (global) | Cyclones, floods and wildfires at **Orange or Red**; anything else (quakes, volcanoes) only at **Red**; droughts never |

You get one alert when an event first appears, and one more if it escalates
(e.g. GDACS Orange to Red). Repeated updates about the same event stay quiet.
No run sends more than 5 alerts; extras are bundled into one summary card.

## Files

- `monitor.py`: the whole program, with comments explaining each part
- `test_monitor.py`: offline tests using sample feed data
- `.github/workflows/monitor.yml`: tells GitHub to run it every 15 minutes
- `state.json`: created automatically; the "already told you about this" memory

## Setup (about 15 minutes)

**1. A private Discord server.** In Discord, tap **+** (Add a Server) →
**Create My Own** → **For me and my friends**, and call it something like
"Hazards". The default `#general` channel is fine, or make a `#alerts` one.

**2. Create a webhook.** On a computer: open the channel's settings (gear icon)
→ **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
Anyone with that URL can post into your channel, so treat it like a password.

**3. Make sure your phone buzzes.** On your phone, long-press the server icon →
**Notifications** → **All Messages**. Discord's default for servers is
"Only @mentions", which would make alerts arrive silently.

**4. GitHub repository.** Create a new **public** repository and upload these
files, keeping the `.github/workflows/` folder structure. Hidden folders are
easy to miss when uploading, so check `.github` made it.

Make it public because scheduled Actions are free and unlimited on public
repositories. A private one runs out of free minutes at this frequency.
Nothing sensitive is in the repo: the webhook URL lives in a secret, and
`state.json` only holds public event IDs.

**5. Add the secret.** In the repo, go to **Settings → Secrets and variables →
Actions → New repository secret**. Set the name to `DISCORD_WEBHOOK_URL` and
paste the webhook URL as the value.

**6. Send a test card.** Open the **Actions** tab → **Hazard monitor** →
**Run workflow**, tick **Just send a sample card**, and run it. A green ✅
card should appear in Discord, with a map link to Melbourne.

**7. First real run.** Run the workflow again with the box unticked. This first
run is deliberately silent: it records everything currently active as "already
seen", so you aren't flooded. After that it runs on its own every 15 minutes.

**8. Read the log.** Click into that run and open **Check feeds**. Each source
prints either `N item(s) pass the filter` or a `could not fetch` line. That's
how you'll know which feeds work from GitHub's servers.

## Trying it on your laptop

```bash
pip install requests
python -m unittest test_monitor.py     # offline tests
python monitor.py --dry-run            # live feeds, prints instead of sending

export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python monitor.py --test-message       # posts one sample card
```

`--dry-run` never changes `state.json`, so you can run it repeatedly while
tuning the rules.

## Tuning

All settings are at the top of `monitor.py` under **CONFIGURATION**:

- **More BoM warnings:** add a phrase and emoji to `BOM_KEEP_PHRASES`, e.g.
  `"severe thunderstorm": "⛈️"`. Be warned: in spring that can be daily.
- **Different card colours:** change `COLOUR_RED` / `COLOUR_ORANGE`.
- **Quieter GDACS:** remove `"FL"` from `GDACS_ORANGE_TYPES` so floods need Red.
- **Fewer alerts per run:** lower `MAX_PUSHES_PER_RUN`.
- **Less often:** change the cron line in the workflow, e.g. `"*/30 * * * *"`.

## Known gaps

- **BoM may block GitHub's servers.** BoM is strict about automated access. If
  the log shows `403` for every BoM feed, the rest still works. Options are
  running on your own machine instead or using BoM's FTP warning products.
- **QLD and SA feeds are untested live.** I couldn't download them while
  building this. NSW and VIC were checked against real data. If QLD or SA show
  `could not read`, the field names need adjusting.
- **Not yet covered:** WA, TAS and NT fire and emergency feeds (BoM weather
  warnings for those states *are* covered). ACT fires appear in the NSW RFS feed.
- **GDACS rates humanitarian impact, not size.** A huge fire in remote Canada
  can stay Green and never notify.
- **GitHub timing.** Scheduled runs can start a few minutes late at busy times.
- **BoM cards have no map link.** The BoM feeds don't include coordinates.

Data: Bureau of Meteorology, NSW RFS, Emergency Management Victoria, QFD,
SA CFS, GDACS (EC-JRC/UN). Personal, non-commercial use.
