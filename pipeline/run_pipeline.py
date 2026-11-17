#!/usr/bin/env python3
"""10 m surface-temperature pipeline for Tashkent (modelled, not measured).

What one run does
  1. Reads unet10m/models/latest.json from R2, downloads that model version (4-seed U-Net ensemble + OpenStreetMap water file) and checks the file hashes.
  2. Asks Earth Engine for clear Landsat 8/9 scenes of the last 60 days that are in the model's season (1 Apr - 15 Oct) and at least 4 days old
     (so the +-3 day Sentinel-2 window that the model was trained with is complete). One scene per date.
  3. For every date that has no map yet: builds the 10 m map (Sentinel-2 + ERA5-Land weather + Landsat anchor), checks it (see qc_scene in unet_lst_v3.py)
     and, only if it passes, uploads  unet10m/scenes/unet10m_<date>.png  and updates  unet10m/manifest.json.
  4. Dates that cannot be mapped (cloud over Sentinel-2, no Landsat pixels, failed checks) are written to the manifest under "skipped" with the reason, and are not retried
     (except when the cause is only that weather data is not available yet).
Exit code is 1 only for unexpected errors; skipped scenes are normal.

Environment: GEE_SERVICE_ACCOUNT_KEY (json), GEE_PROJECT_ID, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY; optional R2_BUCKET_NAME (tashkent-uhi), R2_PUBLIC_BASE.
Local test without uploading:  python run_pipeline.py --dry-run --date 2025-07-12"""
import argparse, datetime, hashlib, importlib.util, json, os, sys, tempfile, traceback
import requests

BUCKET = os.environ.get('R2_BUCKET_NAME', 'tashkent-uhi')
PUBLIC = os.environ.get('R2_PUBLIC_BASE', 'https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev').rstrip('/')
PREFIX = 'unet10m'
HERE = os.path.dirname(os.path.abspath(__file__))
TRANSIENT = ('ERA5',)       # reasons that may go away later (weather data arrives with a delay)


def get_json(url, default=None):
    r = requests.get(url, timeout=60, headers={'Cache-Control': 'no-cache'})
    if r.status_code == 404 and default is not None: return default
    r.raise_for_status(); return r.json()


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()


def fetch_model(workdir, version=None):
    """Downloads the published model into workdir with the file names the training script expects. Returns the model card."""
    version = version or get_json(f'{PUBLIC}/{PREFIX}/models/latest.json')['version']; base = f'{PUBLIC}/{PREFIX}/models/{version}'; card = get_json(f'{base}/model.json')
    for name, info in card['files'].items():
        dest = os.path.join(workdir, name)
        if not (os.path.exists(dest) and sha256(dest) == info['sha256']):
            print('downloading', name, round(info['bytes'] / 1e6, 1), 'MB', flush=True)
            with requests.get(f'{base}/{name}', stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(1 << 20): f.write(chunk)
        if sha256(dest) != info['sha256']: raise SystemExit(f'checksum mismatch for {name}: the download is corrupt or the bundle was changed')
    return card


def load_script(workdir, card):
    """The training script reads TAG / ENS from the environment when it is imported, so they are set first."""
    os.environ['TAG'] = card['tag']; os.environ['ENS'] = ','.join(str(s) for s in card['seeds']); os.environ.setdefault('CITY', 'tashkent')
    spec = importlib.util.spec_from_file_location('unet_lst_v3', os.path.join(HERE, 'unet_lst_v3.py')); u = importlib.util.module_from_spec(spec); sys.modules['unet_lst_v3'] = u; spec.loader.exec_module(u)
    return u


def r2_client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], config=Config(signature_version='s3v4', region_name='auto'))


def plan(scenes, manifest, force=False, only_date=None, max_scenes=6):
    """Which scenes still need a map: not in the manifest, not skipped for a permanent reason."""
    done = {m['date'] for m in manifest.get('maps', [])}; skipped = manifest.get('skipped', {}); todo = []
    for s in scenes:
        d = s['date']
        if only_date and d != only_date: continue
        if not force and not only_date:
            if d in done: continue
            if d in skipped and not skipped[d].get('transient'): continue
        todo.append(s)
    return todo[:max_scenes]


def merge_manifest(manifest, entry=None, skipped=None):
    maps = [m for m in manifest.get('maps', []) if not (entry and m['date'] == entry['date'])] + ([entry] if entry else [])
    sk = dict(manifest.get('skipped', {}))
    if entry: sk.pop(entry['date'], None)
    if skipped: sk[skipped['date']] = {k: v for k, v in skipped.items() if k != 'date'}
    return {'maps': sorted(maps, key=lambda m: m['date']), 'skipped': sk, 'updated': datetime.datetime.utcnow().isoformat() + 'Z'}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--date'); ap.add_argument('--force', action='store_true'); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--max-scenes', type=int, default=6); ap.add_argument('--days-back', type=int, default=60); ap.add_argument('--model-version'); a = ap.parse_args()
    work = tempfile.mkdtemp(prefix='unet10m_'); os.chdir(work)
    card = fetch_model(work, a.model_version); print('model version', card['version'], '| seeds', card['seeds'], '| tag', card['tag'], flush=True)
    u = load_script(work, card)
    import torch
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'; flags = u.parse_cfg(card['config']); name = u.cfg_name(flags)
    if name != card['config']: raise SystemExit(f"the model card says config {card['config']} but the script reads it as {name}")
    model = u.load_model(flags, name, dev); model.eval(); water = u.load_water()
    ee = u.ee_setup(); roi = u.roi_geom(ee); s3 = None if a.dry_run else r2_client()
    manifest = get_json(f'{PUBLIC}/{PREFIX}/manifest.json', default={'maps': [], 'skipped': {}})
    days_back = a.days_back
    if a.date: days_back = max(days_back, (datetime.datetime.utcnow() - datetime.datetime.strptime(a.date, '%Y-%m-%d')).days + 5)
    scenes = u.find_scenes(ee, roi, days_back=days_back); todo = plan(scenes, manifest, a.force, a.date, a.max_scenes)
    print(len(scenes), 'clear Landsat scenes in season;', len(todo), 'to map:', [s['date'] for s in todo], flush=True)
    lines, errors = [], 0
    for sc in todo:
        d = sc['date']
        try:
            full, unc, meta = u.scene_map(ee, roi, sc, flags, model, dev, water)
            problems = u.qc_scene(full, meta)
            if problems: raise u.SceneUnusable('qc: ' + '; '.join(problems))
            fn = f'unet10m_{d}.png'; path = os.path.join(work, fn); bounds, ok = u.write_png(full, meta['x0'], meta['y1'], meta['n'], path)
            import numpy as np
            v = full[ok & np.isfinite(full)]; p = np.percentile(v, [1, 50, 99])
            entry = {'date': d, 'url': f'{PUBLIC}/{PREFIX}/scenes/{fn}', 'bounds': bounds, 'label': 'Modelled 10 m (U-Net)', 'model_version': card['version'], 'scene_id': meta['scene_id'], 'scene_mean_c': round(meta['scene_mean_c'], 2),
                     'weather': meta['weather'], 'stats': {'min': round(float(v.min()), 1), 'max': round(float(v.max()), 1), 'p1': round(float(p[0]), 1), 'median': round(float(p[1]), 1), 'p99': round(float(p[2]), 1), 'pixel_count': int(v.size)},
                     'qc': {'tiles_done': meta['tiles_done'], 'tiles_wanted': meta['tiles_wanted'], 'landsat_valid_share': round(meta['landsat_valid_share'], 3)}, 'generated_at': datetime.datetime.utcnow().isoformat() + 'Z'}
            if not a.dry_run:
                s3.upload_file(path, BUCKET, f'{PREFIX}/scenes/{fn}', ExtraArgs={'ContentType': 'image/png', 'CacheControl': 'public, max-age=3600'})
                manifest = merge_manifest(manifest, entry=entry)
                s3.put_object(Bucket=BUCKET, Key=f'{PREFIX}/manifest.json', Body=json.dumps(manifest, indent=1).encode(), ContentType='application/json', CacheControl='no-cache')      # after every scene, so a later failure loses nothing
            lines.append(f"- {d}: published ({entry['stats']['min']} to {entry['stats']['max']} C, median {entry['stats']['median']} C)" + (' [dry run, nothing uploaded]' if a.dry_run else ''))
        except u.SceneUnusable as e:
            reason = str(e); lines.append(f'- {d}: skipped, {reason}'); print(d, 'skipped:', reason, flush=True)
            if not a.dry_run:
                manifest = merge_manifest(manifest, skipped={'date': d, 'reason': reason, 'transient': reason.startswith(TRANSIENT), 'at': datetime.datetime.utcnow().isoformat() + 'Z'})
                s3.put_object(Bucket=BUCKET, Key=f'{PREFIX}/manifest.json', Body=json.dumps(manifest, indent=1).encode(), ContentType='application/json', CacheControl='no-cache')
        except Exception:
            errors += 1; traceback.print_exc(); lines.append(f'- {d}: ERROR, see the log')
    summary = f"### 10 m pipeline, model {card['version']}\n" + ('\n'.join(lines) if lines else 'No new Landsat scenes to map.')
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f: f.write(summary + '\n')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
