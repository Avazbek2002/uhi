"""Step 2: estimate heights for buildings that have no height/levels in OSM, using the ~8% that do.
Features: footprint size and shape, building type, and how big/dense the neighbouring buildings are (a block of large slabs looks different from a street of small houses).
usage: python3 02_heights.py IN.parquet OUT.parquet  -> also prints hold-out accuracy (areas held out in 2 km blocks so neighbours are not leaked)."""
import sys, numpy as np, geopandas as gpd, json
from scipy.spatial import cKDTree
from sklearn.ensemble import HistGradientBoostingRegressor
g = gpd.read_parquet(sys.argv[1]).to_crs(32642).reset_index(drop=True)
g['area'] = g.area; c = g.geometry.centroid; xy = np.c_[c.x, c.y]
mrr = g.geometry.minimum_rotated_rectangle()
def aspect(p):
    x, y = p.exterior.coords.xy; a = np.hypot(x[1]-x[0], y[1]-y[0]); b = np.hypot(x[2]-x[1], y[2]-y[1]); return max(a, b) / max(min(a, b), 1e-6)
asp = np.array([aspect(p) if p.geom_type == 'Polygon' else 1 for p in mrr])
tree = cKDTree(xy); la = np.log1p(g.area.values)
def nb(r):
    idx = tree.query_ball_point(xy, r); n = np.array([len(i) for i in idx]); m = np.array([la[i].mean() for i in idx]); mx = np.array([la[i].max() for i in idx]); return n, m, mx
n1, m1, x1 = nb(60); n2, m2, x2 = nb(200)
types = ['yes', 'house', 'apartments', 'detached', 'industrial', 'school', 'retail', 'roof', 'commercial', 'garage', 'garages', 'kindergarten', 'hospital', 'office', 'university', 'dormitory', 'warehouse']
X = np.c_[la, asp, g.geometry.length / np.sqrt(g.area), n1, m1, x1, n2, m2, x2, np.array([(g.btype == t).values for t in types]).T]
known = (g.height_src != 'type_default').values; y = np.log(g.height_m.values)
block = (xy[:, 0] // 2000).astype(int) * 1000 + (xy[:, 1] // 2000).astype(int); ub = np.unique(block[known]); rng = np.random.default_rng(0); hold = set(rng.choice(ub, size=len(ub)//5, replace=False))
te = known & np.isin(block, list(hold)); tr = known & ~te
m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.06, max_depth=6, random_state=0).fit(X[tr], y[tr])
p = np.exp(m.predict(X[te])); t = np.exp(y[te]); lvl_p = p / 3; lvl_t = t / 3
print(f'hold-out ({te.sum()} buildings in unseen 2 km blocks): median abs error {np.median(np.abs(p-t)):.1f} m, within 1 storey {np.mean(np.abs(lvl_p-lvl_t)<=1):.0%}, within 2 storeys {np.mean(np.abs(lvl_p-lvl_t)<=2):.0%}; baseline (constant) median abs error {np.median(np.abs(np.median(t)-t)):.1f} m')
m2_ = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.06, max_depth=6, random_state=0).fit(X[known], y[known])
pred = np.clip(np.exp(m2_.predict(X)), 3.0, 60.0); pred = np.where(g.area.values < 30, np.minimum(pred, 3.5), pred)
g['height_model_m'] = np.round(np.where(known, g.height_m, pred), 1)
g['height_src'] = np.where(known, g.height_src, 'model')
out = g.drop(columns=['area']).to_crs(4326); out.to_parquet(sys.argv[2])
q = g.height_model_m.values; print('final heights: share >=15 m (5+ storeys):', round(float((q >= 15).mean()), 3), ' share >=24 m:', round(float((q >= 24).mean()), 3), ' median', float(np.median(q)))
print('mean predicted height of the', int((~known).sum()), 'unlabeled buildings:', round(float(pred[~known].mean()), 1), 'm vs labeled mean', round(float(np.exp(y[known]).mean()), 1), 'm')
json.dump(dict(holdout_n=int(te.sum()), holdout_mae_m=float(np.median(np.abs(p-t))), within1=float(np.mean(np.abs(lvl_p-lvl_t) <= 1)), within2=float(np.mean(np.abs(lvl_p-lvl_t) <= 2))), open('data_height_model_accuracy.json', 'w'), indent=1)
