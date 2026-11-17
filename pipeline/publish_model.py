#!/usr/bin/env python3
"""Publish a trained model (the seed ensemble plus the OpenStreetMap water file it needs) to Cloudflare R2 as one versioned bundle.

Run it where the trained files are (the training server), with the R2 credentials in the environment:
  export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
  python publish_model.py --version 2026-11-v1 --dir /home/jovyan/work --tag apr_oct_e35 --seeds 7,8,9,10 --water osm_water.json \
      --metrics "120 m RMSE new places 1.68, strictest 1.66; degrade test 0.49 / 0.78; canals cooler in 93% of pixels"

It uploads  unet10m/models/<version>/...  and then points  unet10m/models/latest.json  at it. The pipeline reads latest.json, so publishing a new version
changes what the next run uses; old versions stay in the bucket, and every published map records the version it came from.
Roll back by re-running with --only-pointer --version <older version>."""
import argparse, hashlib, json, os, sys, datetime
import boto3
from botocore.config import Config

CONFIG = 'derived+gain+phys+psf+sign+t4+wphys'      # all+wphys, the switches sorted as cfg_name() does
BUCKET = os.environ.get('R2_BUCKET_NAME', 'tashkent-uhi'); PREFIX = 'unet10m'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--version', required=True); ap.add_argument('--dir', default='.'); ap.add_argument('--tag', required=True)
    ap.add_argument('--seeds', default='7,8,9,10'); ap.add_argument('--water', default='osm_water.json'); ap.add_argument('--metrics', default=''); ap.add_argument('--config', default=CONFIG)
    ap.add_argument('--only-pointer', action='store_true', help='do not upload, only point latest.json at an existing version')
    a = ap.parse_args()
    s3 = boto3.client('s3', endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                      aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], config=Config(signature_version='s3v4', region_name='auto'))
    base = f'{PREFIX}/models/{a.version}'
    if not a.only_pointer:
        seeds = [int(v) for v in a.seeds.split(',')]; files = {}
        for sd in seeds:      # same naming as the training script: seed 7 has no suffix
            local = f'unet_lst_v3_{a.config}' + ('' if sd == 7 else f'_s{sd}') + f'_{a.tag}.pt'; p = os.path.join(a.dir, local)
            if not os.path.exists(p): sys.exit(f'missing {p}')
            files[local] = {'sha256': sha256(p), 'bytes': os.path.getsize(p)}
        wp = os.path.join(a.dir, a.water)
        if not os.path.exists(wp): sys.exit(f'missing {wp}')
        files['osm_water.json'] = {'sha256': sha256(wp), 'bytes': os.path.getsize(wp)}; local_of = {k: (os.path.join(a.dir, k) if k != 'osm_water.json' else wp) for k in files}
        card = {'version': a.version, 'created': datetime.datetime.utcnow().isoformat() + 'Z', 'config': a.config, 'tag': a.tag, 'seeds': seeds, 'files': files, 'metrics': a.metrics,
                'note': 'Modelled, not measured. Metrics are 120 m block errors against Landsat on held-out dates and places.'}
        for k in files:
            print('uploading', k, round(files[k]['bytes'] / 1e6, 1), 'MB'); s3.upload_file(local_of[k], BUCKET, f'{base}/{k}')
        s3.put_object(Bucket=BUCKET, Key=f'{base}/model.json', Body=json.dumps(card, indent=1).encode(), ContentType='application/json')
    s3.put_object(Bucket=BUCKET, Key=f'{PREFIX}/models/latest.json', Body=json.dumps({'version': a.version}).encode(), ContentType='application/json', CacheControl='no-cache')
    print('latest ->', a.version)


if __name__ == '__main__':
    main()
