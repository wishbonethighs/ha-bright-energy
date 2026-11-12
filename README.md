# Bright Energy for Home Assistant

Brings UK smart meter data from Hildebrand's **Bright** app (via the DCC) into
Home Assistant's Energy dashboard. Electricity and gas, consumption and cost.
No extra hardware needed.

## How it works

Bright receives half-hourly readings from your smart meters through the DCC,
usually 30 minutes to a day late. This integration:

- signs in to Bright with your app login (kept only in Home Assistant);
- every 30 minutes, asks Bright for the readings it has and writes them to
  Home Assistant's long-term statistics **at the hour the energy was used**, so
  late readings don't pile up as spikes;
- re-reads the last two days on every update, and the last 7 days (adjustable)
  every 6 hours, so late or corrected readings are picked up; a reading that
  temporarily disappears from Bright never removes what is already stored;
- on first set-up, imports up to 13 months of history in the background.

It only talks to `api.glowmarkt.com` and has no extra Python dependencies.
A routine update is a handful of small web requests and database queries.

## Install

**Home Assistant 2026.11 or newer (Marketplace):**

1. Settings → **Marketplace** → ⋮ → **Add from link**, and enter
   `https://github.com/wishbonethighs/ha-bright-energy`.
2. Install **Bright Energy**; its setup starts straight away. Sign in with
   your Bright email and password.

**Home Assistant 2026.10 (HACS):** HACS → ⋮ → **Custom repositories** → add the
link above as an **Integration**, download **Bright Energy**, restart, then
Settings → Devices & services → **Add integration** → **Bright Energy**.

## Energy dashboard

Settings → Dashboards → **Energy**:

| Section | Choose |
| --- | --- |
| Electricity grid → Grid consumption | **Bright electricity consumption** |
| …cost: "Use an entity tracking the total costs" | **Bright electricity cost** |
| Gas consumption | **Bright gas consumption** |
| …cost: "Use an entity tracking the total costs" | **Bright gas cost** |

Costs come from Bright and exclude the daily standing charge.

Don't use the "Used today" sensors in the Energy dashboard. They are for cards
and automations.

## Sensors (per fuel)

| Sensor | Meaning |
| --- | --- |
| Used today / yesterday | kWh Bright has reported so far for the day |
| Cost today / yesterday | Cost of that usage (no standing charge) |
| Unit rate, Standing charge | Current tariff Bright holds for your meter |
| Latest reading | End of the newest hour Bright has data for. Useful for an alert if Bright stops updating |

## Options and service

- **Days of recent history to re-check** (default 7): Settings → Devices &
  services → Bright Energy → Configure.
- **Re-import history** (`bright_energy.reimport_history`): re-reads the last
  *N* days from Bright and rebuilds the statistics from that point.

## Notes

- Today's figures will always look low: Bright data arrives late.
- Gas is reported in kWh, as Bright provides it.
- Diagnostics (⋮ → Download diagnostics) redact your email and password.
- Not affiliated with Hildebrand or Glowmarkt.
