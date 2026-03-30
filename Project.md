# Project Status

## Current state (March 2026)

The integration is **functional but pre-release**. Core logic is implemented, unit and live API tests pass, and CI is set up. PR #1 is open for initial merge.

### What works

- Config flow with region selection
- Agile Predict API fetching with fallback to alt endpoint
- Octopus historical rate fetching with pagination
- Linear regression calibration from historical Agile vs Tracker data
- Forecast transformation: daily means, confidence levels, clamping
- 4 entities: today's rate, full forecast, cheapest day, cheap-today binary sensor
- 24 unit tests (calibration + coordinator)
- 13 live API tests (Agile Predict + Octopus Energy)
- GitHub Actions CI (unit tests on 3.13/3.14, live API tests, JSON validation)

### What hasn't been tested in a real HA instance

- The full integration lifecycle (install via HACS, configure, see entities)
- Config flow UI rendering
- DataUpdateCoordinator polling loop
- Entity state updates and attributes in HA dashboard
- Error recovery / stale data fallback in practice

## Possible next steps

### High priority

1. **Test in a real Home Assistant instance** — Install via HACS custom repo, configure, verify entities appear and update correctly
2. **Add `info.md` / README** — Required for HACS repository listing; describe what it does, how to install, screenshots
3. **Options flow** — Allow changing calibration interval, cheap threshold percentile, forecast window after initial setup
4. **Recalibration robustness** — Handle Octopus API outages gracefully; cache last-good calibration model to disk

### Medium priority

5. **More entity attributes** — Expose calibration model params (slope, intercept, R-squared) as diagnostic attributes
6. **Services** — `tracker_predict.force_recalibrate` service to trigger on-demand recalibration
7. **Unit tests for config_flow** — Currently untested; needs HA test harness or more mocking
8. **Unit tests for __init__.py** — Test setup/unload entry lifecycle
9. **Improve confidence levels** — Currently based on fixed thresholds; could use prediction intervals from regression

### Lower priority

10. **Gas tracker prediction** — Same approach could work for Octopus Tracker gas rates
11. **Multiple region support** — Allow configuring multiple entries for different regions
12. **Historical accuracy tracking** — Log predicted vs actual rates over time to show calibration quality
13. **Configurable product codes** — UI option to override the hardcoded AGILE/SILVER product codes
14. **HACS default repository submission** — Once stable, submit to the HACS default repo list

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
- [ ] 5 entities created (today, forecast, cheapest_5d, cheapest_10d, cheap_today)
- [ ] `sensor.*_today` has a numeric state in p/kWh and attributes: `tracker_estimate`, `tracker_low`, `tracker_high`, `confidence`, `model_r_squared`
- [ ] `sensor.*_forecast` has a `forecast` attribute containing a list of day objects with `date`, `tracker_est`, `rank`
- [ ] `sensor.*_cheapest_5d` and `*_cheapest_10d` show a future date as their state
- [ ] `binary_sensor.*_cheap_today` shows ON or OFF

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
- **No persistent storage**: Calibration model is recalculated on every HA restart. Could use `hass.data` or a JSON file for caching.
- **Default model may drift**: The fallback slope=0.56/intercept=12.75 is from 2025 East England data. May need periodic updating.
