# 3D sun and shade video

`07_scene_data.py` (in `shade/`) writes `scene3d.json`: two 600 m squares (microrayon-style and low-rise), buildings in local metres with estimated heights, and the sun position and shaded share of open ground for every frame.
`index.html` draws it with three.js (extruded buildings, a directional sun with a real shadow map). `shot.py` loads it in headless Chromium and saves one PNG per frame; join them with ffmpeg:

```bash
npm install three@0.160.0                 # next to index.html
python3 -m http.server 8765 &             # serve this folder
python3 shot.py frames all                # 360 frames, about 2.5 minutes
ffmpeg -framerate 24 -i frames/f%04d.png -c:v libx264 -pix_fmt yuv420p -crf 18 out.mp4
```
Modelled, not measured. Building heights are mostly estimated; trees are not included.
