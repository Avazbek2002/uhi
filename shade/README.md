# Shade analysis: do microrayon-style neighbourhoods shade themselves, and does it show up in satellite heat?

Modelled, not measured. Everything here is computed from OpenStreetMap buildings and the Landsat surface-temperature (LST) scenes already in this repo.

## Run order
```bash
pip install geopandas rasterio pyarrow scikit-learn osmium shapely
python3 01_buildings.py uzbekistan.osm.pbf buildings.parquet                 # footprints + heights from tags
python3 02_heights.py buildings.parquet buildings_h.parquet                  # estimate missing heights
python3 03_shade.py buildings_h.parquet ../public/data/tashkent_mahallas_master.geojson shade.csv 2024-06-04
python3 05_green.py uzbekistan.osm.pbf ../public/data/tashkent_mahallas_master.geojson green.csv
python3 04_compare.py shade.csv ../public/data/lst-manifest.json out/cmp
python3 06_animation.py buildings_h.parquet ../public/data/tashkent_mahallas_master.geojson 2607038 2601038 sun_day.mp4
```
Outputs used in the write-up are in `results/`.

## Method
1. **Buildings:** 319,517 footprints in Tashkent from OSM. Only about 8% carry a height or floor count.
2. **Heights:** the rest are estimated by a small model trained on the labelled 8% (footprint size and shape, building type, size and density of neighbours). On city blocks it never saw: median error 2.3 m (a constant guess: 4.0 m), 57% within one storey, 76% within two. Labelled buildings are probably larger than average, so real errors are likely higher for ordinary houses.
3. **Sun and shadows:** standard solar-position formulas for Tashkent. A building's shadow is its footprint swept along the shadow direction, length = height / tan(sun elevation), on a 5 m grid. Day: 4 June 2024 (a Landsat scene date). The 10:55 slice matches the satellite pass.
4. **Per mahalla:** share of open ground (not roofs) in shade, building cover, mean building height, share of footprint in buildings of 15 m or more ("tall blocks").
5. **Comparison with satellite:** mean Landsat LST per mahalla over all 40 scenes in `lst-manifest.json`, and the 2024-06-04 scene alone. 555 mahallas of 15 ha or more.
6. **Tree proxy:** share of area covered by OSM vegetation polygons and mapped trees per km2. OSM tree coverage is patchy: this is a rough stand-in, not NDVI.

## Findings (555 mahallas)
- Microrayon-like mahallas (tall blocks >= 50% of footprint, building cover >= 12%, n = 44) average **42.6 C** against **45.8 C** for low-rise private-house mahallas (n = 243). That is 3.2 C cooler, and it holds in every distance-from-centre band (1.6 to 4.7 C), so it is not just a city-centre effect.
- Mean building height has the strongest link with LST (rank correlation -0.69). Building cover is positive (+0.46: more buildings, hotter). Modelled shade at the satellite hour is weak on its own (-0.16).
- **Shade by buildings alone does not explain the cooling.** Only about 10% of open ground is shaded at 10:55 in June, and within low-rise mahallas more modelled shade goes with hotter, not cooler, surfaces (+0.23), because shade there just reflects dense building. In the animation example the low-rise area even has more building shade than the microrayon late in the day.
- The mapped-tree proxy points the other way for microrayons: 53 mapped trees per km2 against 4.6 in low-rise areas, and OSM green share correlates with cooler LST (-0.34). That fits the idea that yard trees and open green space do part of the work, but OSM mapping effort is uneven, so this is a hint to test with satellite vegetation data, not a result.
- All models together explain about 42% of the variation between mahallas, so most of it is still unexplained.

## Limits
- Heights are estimated for 92% of buildings. Trees, awnings, walls' fine detail, terrain and clouds are not modelled. Convex-hull shadows slightly over-shade courtyards.
- Landsat passes at about 10:55 local time, so satellite heat shows the morning surface, not the afternoon peak.
- "Microrayon-like" is inferred from building height and footprint, not from an official list.
- Mahalla boundaries are large, so averages blur courtyards and streets.
- This is correlation across neighbourhoods, not proof that a layout causes the cooling.

## Next
Add satellite vegetation (NDVI) so trees can be tested properly; add a road-surface mask to measure shade on asphalt specifically; compare against field infrared-thermometer readings of sunlit and shaded surfaces.
