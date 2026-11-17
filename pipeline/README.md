# 10 m surface-temperature pipeline (modelled, not measured)

A GitHub Actions job that builds one 10 m map of Tashkent for every new, clear Landsat 8/9 scene and publishes it to Cloudflare R2, where the app's **Modelled 10 m** layer reads it.

```
Landsat 8/9 (new clear scene)  ─┐
Sentinel-2 (±3 days)            ├─►  U-Net ensemble (CPU)  ─►  checks  ─►  PNG + manifest on R2  ─►  app
ERA5-Land weather (overpass)    │        (4 seeds, +OSM water)            (skip, never publish bad maps)
Landsat 120 m as the anchor    ─┘
```

Every map is built the way the training data was: Sentinel-2 median within ±3 days of the Landsat date, ERA5-Land at the overpass hour, the scene's 120 m Landsat temperature as the anchor. That is why the validation numbers apply to it. A map for a day without a Landsat scene would be a different, untested product, so the pipeline does not make one.

## What it publishes (bucket `tashkent-uhi`)

| Key | Content |
|---|---|
| `unet10m/scenes/unet10m_<date>.png` | transparent PNG, the app's palette, 25 to 55 °C |
| `unet10m/manifest.json` | `maps` (date, url, corner coordinates, stats, model version, checks) and `skipped` (date and reason) |
| `unet10m/models/<version>/` | model bundle: 4 seed files, `osm_water.json`, `model.json` (file hashes, metrics) |
| `unet10m/models/latest.json` | which version the pipeline uses |

## When maps appear

- Only April 1 to October 15: the model was trained on those months. Winter (snow, frost) is outside what it has seen.
- A scene is mapped once it is at least 4 days old, so the whole ±3 day Sentinel-2 window exists.
- Cloudy Landsat scenes (over 25%) are ignored. A scene is skipped, with the reason recorded in the manifest, if Sentinel-2 is mostly cloudy, Landsat has no valid pixels, or a check fails: fewer than 90% of the tiles estimated, less than 85% of the city covered, implausible temperatures, a city mean more than 4 °C from the Landsat mean, or Landsat covering under half of the tiles.
- Expect roughly one map per 8 to 16 days in summer, fewer in spring and autumn.

## One-time setup

1. **Repository secrets** (Settings → Secrets and variables → Actions). Same names as the Modal job: `GEE_SERVICE_ACCOUNT_KEY` (the service-account JSON), `GEE_PROJECT_ID`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. The service account must be registered for Earth Engine.
2. **Publish the model** from the machine that holds the trained files (the R2 variables set there):
   ```bash
   pip install boto3
   python pipeline/publish_model.py --version 2026-11-v1 --dir /home/jovyan/work --tag apr_oct_e35 --seeds 7,8,9,10 --water osm_water.json \
     --metrics "120 m RMSE: new places 1.68, strictest 1.66; degrade test 0.49 / 0.78; canals cooler in 93% of pixels"
   ```
3. **First run**: Actions → unet10m → Run workflow. Tick *dry run* with a known clear date (for example a July Landsat date) to test without uploading; then run it without. After that it runs daily on its own.
4. The bucket must be public (it already is for the Landsat PNGs) with CORS allowing the app's origin for `unet10m/`.

## Operations

- **New model**: train, evaluate on held-out data, publish with a new `--version`. The next run uses it. Existing maps keep the version they were built with (recorded in the manifest). To rebuild old dates with the new model, run the workflow per date or run `python pipeline/run_pipeline.py --force --max-scenes 20`.
- **Roll back**: `python pipeline/publish_model.py --version <older> --only-pointer`.
- **Backfill a season**: Actions → unet10m → Run workflow with `days_back` 240 and `max_scenes` 8 (each scene takes roughly 5 to 10 minutes; the manifest is updated after every scene, so run it again to continue where it stopped). Already mapped dates are skipped.
- **Failures**: the job fails (and GitHub emails the repo owner) only for unexpected errors. Skipped scenes are normal and listed in the run summary.
- **Local test**: `python pipeline/run_pipeline.py --dry-run --date YYYY-MM-DD` (needs the Earth Engine variables and a published model).
- `pipeline/unet_lst_v3.py` is the training script, copied here so training and serving share the same code (`render_tiles`, `scene_map`, `qc_scene`).

## What is and is not verified

- Tested: the 120 m block averages against Landsat on dates and places the model never saw (about 1.6 °C error against a real spread of about 2.7 °C), and the model on a city it never trained on (Bukhara, about 2 °C).
- Not verified: the detail inside a 120 m block, which only field measurements could check. Canal cooling size comes from the physics term, not from Landsat.
- This automation has been tested here with simulated Earth Engine and R2 (model download and checksum, scene selection, checks, upload, manifest). It has not yet run against live Earth Engine; the first dry run is that test.
