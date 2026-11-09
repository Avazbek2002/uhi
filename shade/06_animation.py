"""Step 6: sun-movement animation. Two 600 m x 600 m squares side by side (a microrayon-style area and a low-rise area), shadows every 10 minutes from sunrise to sunset on one day.
usage: python3 06_animation.py buildings_h.parquet mahallas.geojson MICRO_ID LOWRISE_ID OUT.mp4 [DATE]
Frames are drawn with matplotlib and joined with ffmpeg. Modelled, not measured."""
import sys, math, datetime as dt, importlib, subprocess, os, tempfile, numpy as np, geopandas as gpd, shapely
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MP
from matplotlib.collections import PatchCollection
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); S = importlib.import_module('03_shade')
b = gpd.read_parquet(sys.argv[1]).to_crs(32642); mh = gpd.read_file(sys.argv[2]).to_crs(32642); mid, lid = int(sys.argv[3]), int(sys.argv[4]); out = sys.argv[5]
date = dt.date.fromisoformat(sys.argv[6]) if len(sys.argv) > 6 else dt.date(2024, 6, 4); HALF = 300
def crop(mid_):
    g = mh[mh.mahalla_id == mid_].geometry.iloc[0]; c = g.representative_point(); x, y = c.x, c.y
    box = shapely.box(x - HALF, y - HALF, x + HALF, y + HALF); near = b[b.intersects(box.buffer(300))]     # include buildings just outside the square: their shadows fall inside
    return box, near.reset_index(drop=True), (x, y)
P = []
for label, i in (('Microrayon-style blocks', mid), ('Low-rise private houses', lid)):
    box, near, cxy = crop(i); inbox = near[near.intersects(box)]; P.append((label, box, near, cxy, shapely.union_all(inbox.geometry.intersection(box).values)))
frames = []; t0 = dt.datetime.combine(date, dt.time(6, 0))
for k in range(0, 14 * 6 + 1): frames.append(t0 + dt.timedelta(minutes=10 * k))
tmp = tempfile.mkdtemp(); BG, ROOF, SH, GR = '#0d1620', '#e8edf2', '#38465a', '#c9a06a'
fig, axs = plt.subplots(1, 2, figsize=(16, 9), dpi=100); fig.patch.set_facecolor(BG)
for n, ts in enumerate(frames):
    el, az = S.sun(date, ts.hour, ts.minute); ok = el >= 4
    for ax, (label, box, near, cxy, roofs) in zip(axs, P):
        ax.clear(); ax.set_facecolor(GR if ok else '#2b3340'); ax.set_xlim(cxy[0] - HALF, cxy[0] + HALF); ax.set_ylim(cxy[1] - HALF, cxy[1] + HALF); ax.set_aspect('equal'); ax.set_xticks([]); ax.set_yticks([])
        for s_ in ax.spines.values(): s_.set_visible(False)
        shade_pct = 0.0
        if ok:
            sp = S.shadows(near.geometry.values, near.height_model_m.values, el, az); pl = [p for p in sp if p is not None and not p.is_empty]
            allsh = shapely.union_all(sp).intersection(box).difference(roofs) if len(sp) else shapely.Polygon(); ground = box.area - roofs.area; shade_pct = allsh.area / ground * 100
            polys = [allsh] if allsh.geom_type == 'Polygon' else list(getattr(allsh, 'geoms', []))
            ax.add_collection(PatchCollection([MP(np.array(p.exterior.coords), closed=True) for p in polys if p.geom_type == 'Polygon'], facecolor=SH, edgecolor='none', alpha=0.88))
        rp = [roofs] if roofs.geom_type == 'Polygon' else list(roofs.geoms); ax.add_collection(PatchCollection([MP(np.array(p.exterior.coords), closed=True) for p in rp if p.geom_type == 'Polygon'], facecolor=ROOF, edgecolor='none'))
        ax.set_title(label, color='#d9e4ee', fontsize=18, pad=10); ax.text(0.02, 0.03, f'Open ground in shade: {shade_pct:4.0f}%', transform=ax.transAxes, color='#ffffff', fontsize=17, weight='bold', bbox=dict(facecolor=BG, alpha=0.8, pad=6, edgecolor='none'))
    fig.suptitle(f'{date:%d %B %Y}   {ts:%H:%M}   sun {max(el,0):.0f}° above the horizon', color='#ffffff', fontsize=22, y=0.97)
    fig.text(0.5, 0.015, 'Modelled, not measured: building shadows from OpenStreetMap footprints with estimated heights; trees not included.  Local time (UTC+5).', color='#8fa3b5', ha='center', fontsize=12)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.88, bottom=0.06, wspace=0.04); fig.savefig(f'{tmp}/f{n:04d}.png', facecolor=BG)
subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-framerate', '12', '-i', f'{tmp}/f%04d.png', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast', '-crf', '20', out], check=True); print('wrote', out, len(frames), 'frames')
