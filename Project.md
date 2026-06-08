# Project Status

## Current state (June 2026)

The integration is **functional and tested in a real HA instance**. Core logic is implemented, unit and live API tests pass, CI is set up, and README is in place. Weekly automated recalibration has been running in production for months.

### What works

- Config flow with region selection
- Options flow to override poll interval, calibration days/interval, and Agile/Tracker product codes after setup
- Agile Predict API fetching with fallback to alt endpoint
- Octopus historical rate fetching with pagination
- Tracker product discovery by probing `KNOWN_TRACKER_PRODUCTS` (the listing API delists Tracker); Agile via listing API with fallback
- Linear regression calibration from historical Agile vs Tracker data
- Forecast transformation: daily means (UK-day bucketed), confidence levels, clamping
- 6 entities grouped under a device: today rank, full forecast, cheapest 5d, cheapest 10d, forecast generated, forecast calendar
- Calibration model persisted to HA storage via `Store` (`.storage/tracker_predict.calibration.<entry_id>`) — loaded on startup, skips recalibration if still within interval, falls back to cached/default model on API outage, cleaned up on removal, type-validated on load
- Automated weekly recalibration of per-region defaults via `recalibrate.yml` — maintains a single rolling PR; verified working in production
- Forecast visualisation: dashboard examples in README (ApexCharts band chart, Markdown table, Calendar card). The `forecast` attribute works with third-party cards — no integration code needed. (The `weather` platform was evaluated but rejected: its fields are temperature/precipitation, not prices.)
- ~95 unit tests (calibration + coordinator + calendar + persistence)
- 13 live API tests (Agile Predict + Octopus Energy)
- GitHub Actions CI (unit tests on 3.13/3.14, live API tests, JSON validation)
- Tested in a real HA instance via manual file copy — entities appear and update correctly

## Possible next steps

1. **Add a magnitude metric** — Measure relative-magnitude accuracy (predicted vs actual % difference between day pairs), backfill it into `calibration_history.json`, and surface it in recalibration output. This is the main remaining measurement gap (see `rank-accuracy-notes.md`).
2. **Services** — `tracker_predict.force_recalibrate` service to trigger on-demand recalibration
3. **Historical accuracy tracking** — Log predicted vs actual rates over time to show calibration quality in-app
4. **Improve confidence levels** — Currently based on fixed thresholds; could use prediction intervals from regression
5. **HACS default repository submission** — Once stable, submit to the HACS default repo list
6. **Unit tests for `config_flow.py` and `__init__.py`** — Both are currently untested (no `test_config_flow`/`test_init`); would need an HA test harness or more mocking

## Testing in a real Home Assistant instance

### Option 1: Manual file copy (fastest, no HACS required)

Copy the integration directly into your HA config directory — no GitHub involvement, no publishing:

```bash
cp -r custom_components/tracker_predict /path/to/ha-config/custom_components/
```

Then restart Home Assistant. The integration will appear in **Settings → Devices & Services → Add Integration**.

This is the best approach for rapid iteration: edit files locally, copy across, reload the integration (no full restart needed for most code changes — use the ⋮ menu on the integration card → **Reload**).

### Option 2: HACS Custom Repository (tests the full install UX)

This does **not** require the repo to be in the HACS default list — you can add any private or unlisted GitHub repo:

1. Install [HACS](https://hacs.xyz/docs/use/) in your test HA instance
2. Open HACS → Integrations → three-dot menu → **Custom repositories**
3. Add `https://github.com/jonathanjouty/hacs-octopus-tracker-predict` with category **Integration**
4. Search for "Tracker Predict" and install
5. Restart HA

Use this to test the end-to-end user install flow before submitting to the HACS default list.

### Getting a test Home Assistant instance

If you don't want to risk your production instance, the easiest option is Docker:

```bash
docker run -d \
  --name ha-test \
  -p 8123:8123 \
  -v "$PWD/ha-config:/config" \
  --restart unless-stopped \
  ghcr.io/home-assistant/home-assistant:stable
```

Open `http://localhost:8123`, complete the onboarding wizard, then copy `custom_components/tracker_predict/` into `./ha-config/custom_components/` and restart the container. The config directory is also where you find `home-assistant.log` for debugging.

Alternatives:
- **Home Assistant OS in a VM** (VirtualBox, UTM, Proxmox) — closest to real hardware, supports add-ons
- **Existing test/dev HA instance** — if you already have one separate from production

### Dev iteration tip

For code changes, the fastest cycle is:
1. Edit files in `custom_components/tracker_predict/` on your dev machine
2. Copy changed files to your HA config directory
3. In HA: Settings → Devices & Services → Tracker Predict → ⋮ → **Reload**

A full HA restart is only needed for changes to `manifest.json`.

## Release workflow

- **Now**: Option A — manual. Run `git tag vX.Y.Z && git push origin vX.Y.Z` locally, then create the GitHub release via the UI.
- **Later**: Option C — add a `release.yml` workflow with `workflow_dispatch` trigger and a version input, allowing a release to be triggered directly from the GitHub Actions tab without needing local access.

HACS requires a git tag matching the version in `manifest.json` and a corresponding GitHub release.

## Known issues

- **Tracker product discovery relies on a hardcoded list**: Octopus delists Tracker products from their listing API, so we probe `KNOWN_TRACKER_PRODUCTS` (in `const.py`) newest-first for the active code (currently `SILVER-25-09-02`). When Octopus releases a new Tracker version, add its code to that list.
- **Agile product discovery may break**: Discovers via the listing API but falls back to `AGILE-24-10-01`.
- **Default model may drift**: The overall fallback (slope≈0.62 / intercept≈11.4) and the per-region `DEFAULT_CALIBRATION` values are refreshed by the weekly recalibration workflow, but are only used until a region's first live calibration completes.
