"""Step 4: does modelled shade line up with satellite surface temperature, mahalla by mahalla?
usage: python3 04_compare.py shade.csv ../public/data/lst-manifest.json OUT_PREFIX
LST per mahalla = mean over all Landsat scenes in the manifest (and the single 2024-06-04 scene the shadows were computed for)."""
import sys, json, numpy as np, pandas as pd
from scipy.stats import spearmanr
d = pd.read_csv(sys.argv[1]); mp = json.load(open(sys.argv[2]))['maps']
rows = {}
for sc in mp:
    for k, v in (sc.get('mahalla_stats') or {}).items(): rows.setdefault(int(k), {})[sc['date']] = v['mean']
L = pd.DataFrame(rows).T; d['lst_all_mean'] = d.mahalla_id.map(L.mean(axis=1)); d['lst_20240604'] = d.mahalla_id.map(L['2024-06-04']); d['n_scenes'] = d.mahalla_id.map(L.notna().sum(axis=1))
d = d[(d.area_ha >= 15) & d.lst_all_mean.notna() & d.mean_building_height_m.notna()].copy(); print(len(d), 'mahallas with >= 15 ha and LST')
d['microrayon_like'] = (d.tall_share_of_footprint >= 0.5) & (d.building_cover >= 0.12); d['lowrise'] = d.tall_share_of_footprint < 0.05
cols = ['shade_1055', 'shade_mean_day', 'building_cover', 'mean_building_height_m', 'tall_share_of_footprint']
out = {}
for tgt in ['lst_20240604', 'lst_all_mean']:
    out[tgt] = {c: round(float(spearmanr(d[c], d[tgt])[0]), 2) for c in cols}; print(tgt, 'Spearman correlation with', out[tgt])
# does shade add anything once building cover is known?  (ordinary least squares on standardised variables)
def ols(y, Xc):
    X = np.c_[np.ones(len(d)), *[(d[c] - d[c].mean()) / d[c].std() for c in Xc]]; yv = (d[y] - d[y].mean()) / d[y].std(); b = np.linalg.lstsq(X, yv, rcond=None)[0]; r2 = 1 - ((yv - X @ b) ** 2).sum() / (yv ** 2).sum(); return dict(zip(['const'] + Xc, np.round(b, 2))), round(float(r2), 3)
for Xc in (['building_cover'], ['shade_1055'], ['building_cover', 'shade_1055'], ['building_cover', 'shade_1055', 'mean_building_height_m']):
    b, r2 = ols('lst_all_mean', Xc); print('LST_all ~', Xc, '-> R2', r2, b); out['ols_' + '+'.join(Xc)] = dict(r2=r2, coef=b)
g = d.groupby(np.where(d.microrayon_like, 'microrayon-like (tall blocks)', np.where(d.lowrise, 'low-rise (private houses)', 'mixed'))).agg(n=('mahalla_id', 'size'), lst_all=('lst_all_mean', 'mean'), lst_0604=('lst_20240604', 'mean'), shade_1055=('shade_1055', 'mean'), shade_day=('shade_mean_day', 'mean'), cover=('building_cover', 'mean'), height=('mean_building_height_m', 'mean')).round(3); print(g.to_string()); g.to_csv(sys.argv[3] + '_groups.csv')
d.to_csv(sys.argv[3] + '_mahalla.csv', index=False); json.dump(out, open(sys.argv[3] + '_stats.json', 'w'), indent=1)
