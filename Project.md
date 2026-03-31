# Project Status

## Current state (March 2026)

The integration is **functional and tested in a real HA instance**. Core logic is implemented, unit and live API tests pass, CI is set up, and info.md/README are in place.

### What works

- Config flow with region selection
- Agile Predict API fetching with fallback to alt endpoint
- Octopus historical rate fetching with pagination
- Linear regression calibration from historical Agile vs Tracker data
- Forecast transformation: daily means, confidence levels, clamping
- 6 entities grouped under a device: today rank, full forecast, cheapest 5d, cheapest 10d, last updated, forecast calendar
- Calibration model persisted to HA storage (survives restarts, cleaned up on removal)
- 40 unit tests (calibration + coordinator + calendar + persistence)
- 13 live API tests (Agile Predict + Octopus Energy)
- GitHub Actions CI (unit tests on 3.13/3.14, live API tests, JSON validation)
- Tested in a real HA instance via manual file copy — entities appear and update correctly
- Auto recalibration not yet verified (needs ~7 days of runtime)

## Possible next steps

### High priority

1. ~~**Auto recalibration persistence**~~ — **Done.** Calibration model is persisted via HA's `Store` to `.storage/tracker_predict.calibration.<entry_id>`. Loaded on startup; skips recalibration if still within interval. Falls back to cached/default model on API outage. Storage file cleaned up on integration removal. Type-validated on load.

### Medium priority

2. **Services** — `tracker_predict.force_recalibrate` service to trigger on-demand recalibration
3. **Configurable product codes** — UI option to override the hardcoded AGILE/SILVER product codes. Useful if Octopus changes codes or for users on different tariff variants.
4. ~~**Forecast visualisation**~~ — **Done.** Dashboard examples added to README (ApexCharts bar chart, Markdown table, Calendar card). The existing `forecast` attribute format works well with third-party cards — no integration code changes needed. The `weather` platform was evaluated but rejected as a poor semantic fit (fields are temperature/precipitation, not prices).
5. **Options flow** — Allow changing calibration interval, cheap threshold percentile, forecast window after initial setup. Only calibration interval and forecast window are candidates currently.

### Someday

6. **Gas tracker prediction** — Same approach could work for Octopus Tracker gas rates
7. **Multiple region support** — Allow configuring multiple entries for different regions
8. **Historical accuracy tracking** — Log predicted vs actual rates over time to show calibration quality
9. **HACS default repository submission** — Once stable, submit to the HACS default repo list
10. **Improve confidence levels** — Currently based on fixed thresholds; could use prediction intervals from regression
11. **Unit tests for config_flow** — Currently untested; needs HA test harness or more mocking
12. **Unit tests for __init__.py** — Test setup/unload entry lifecycle

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

### What to verify (testing checklist)

**Install and config:**
- [ ] Integration appears when searching "Tracker Predict" in Add Integration
- [ ] Config flow: region dropdown renders all 14 regions
- [ ] Selecting a region completes setup without error
- [ ] Integration card appears in Settings → Devices & Services

**Entities:**
- [ ] A "Tracker Predict (X)" device appears in Settings → Devices
- [ ] 6 entities grouped under it: today rank, forecast, cheapest_5d, cheapest_10d, cheap_today, calendar
- [ ] `sensor.*_today_rank` has an integer state (rank, 1 = cheapest) and attributes: `confidence`, `days_in_window`, `stale`
- [ ] `sensor.*_last_updated` has a timestamp state showing last successful fetch
- [ ] `sensor.*_forecast` has a `forecast` attribute (list of day objects with `date`, `tracker_est`, `rank`) and a `forecast_generated_at` attribute showing when AgilePredict generated the forecast
- [ ] `sensor.*_cheapest_5d` and `*_cheapest_10d` show a future date as their state
- [ ] `calendar.*` appears in calendar card and shows all-day events with predicted rates and rank labels

**Behaviour over time:**
- [ ] Check HA logs (Settings → System → Logs → filter `tracker_predict`) for errors
- [ ] After ~7 days: confirm calibration runs and `model_last_calibrated` attribute updates
- [ ] Reload integration (⋮ → Reload) and confirm entities recover cleanly

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

- **Tracker product discovery is broken**: Octopus delisted Tracker products from their API. We hardcode `SILVER-24-04-03`. If Octopus changes product codes, this will need updating.
- **Agile product discovery may also break**: Currently discovers via API but falls back to `AGILE-24-10-01`.
- ~~**No persistent storage**~~: Fixed — calibration model now persisted via HA `Store`.
- **Default model may drift**: The fallback slope=0.56/intercept=12.75 is from 2025 East England data. May need periodic updating.
