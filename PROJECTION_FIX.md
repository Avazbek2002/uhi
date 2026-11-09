# LST Raster Projection Fix

## Problem

The LST overlay shows two visual issues:
1. **Large rotated orange rectangle** - Raw Landsat scene footprint at ~8-10° tilt
2. **Slight misalignment** of the city boundary overlay with mahalla polygons

## Root Cause

**Projection Mismatch:**
- Landsat data from Google Earth Engine is in **UTM Zone 43N (EPSG:32643)** - meters
- Frontend expects **WGS84 (EPSG:4326)** - decimal degrees
- When GeoTIFF bbox is extracted, UTM coordinates are misinterpreted as lon/lat

**Coordinate Systems:**
- Tashkent city boundary: `urn:ogc:def:crs:OGC:1.3:CRS84` (WGS84)
- Mahalla polygons: WGS84 decimal degrees (e.g., `[69.23, 41.36]`)
- Landsat scenes: UTM 43N unless explicitly reprojected

## Solution (COMPLETED)

### Frontend Updates ✅

**Files Modified:**
- `src/App.tsx`
- `public/data/lst-manifest.json`
- `public/data/lst-manifest.example.json`

**Changes:**

1. **Updated `LST_RASTER_BOUNDS` to precise Tashkent coordinates** (lines 75-82)
   - Derived from `tashkent_city.geojson` bounding box: `[69.12, 69.47] × [41.16, 41.40]`
   - Padded slightly to `[69.10, 69.49] × [41.15, 41.41]` to prevent edge clipping

2. **Added `bounds` field to `LstMapEntry` type** (line 1)
   - Allows per-scene bounding boxes from backend manifest
   - Optional field for backward compatibility

3. **Implemented UTM→WGS84 projection detection and conversion** (lines 95-195)
   - `isUtmBbox()`: Detects if GeoTIFF bbox is in UTM (coordinates > ±180/±90)
   - `utmToWgs84()`: Converts UTM Zone 43N coordinates to WGS84 lon/lat
   - `renderGeoTiff()`: Now checks projection and converts if needed
   - **Console warning** when UTM is detected (backend should export in EPSG:4326 instead)

4. **Updated PNG loading to use dynamic bounds** (lines 450-470)
   - `MapboxMap` now accepts optional `rasterBounds` prop
   - Uses `rasterBounds ?? LST_RASTER_BOUNDS` (manifest bounds take precedence)
   - Passed from `selectedLstMap?.bounds` in main component

5. **Updated manifest files**
   - Added `bounds` field to example and actual manifests
   - Bounds format: `[[lon, lat], [lon, lat], [lon, lat], [lon, lat]]` (TL, TR, BR, BL)

### Backend Script Requirements (TODO)

When you create the backend upload script, ensure it:

1. **Clips to Tashkent boundary** before export (removes rotated rectangle)
   ```python
   # Pseudo-code
   tashkent_geom = ee.FeatureCollection("path/to/tashkent_city.geojson").geometry()
   lst_clipped = lst_image.clip(tashkent_geom)
   ```

2. **Exports in EPSG:4326 explicitly**
   ```python
   # Force WGS84 output
   task = ee.batch.Export.image.toCloudStorage({
       'image': lst_clipped.reproject('EPSG:4326', None, 30),
       'crs': 'EPSG:4326',
       'region': tashkent_geom,
       'scale': 30
   })
   ```

3. **Calculates precise WGS84 bounding box**
   ```python
   # Get exact bounds from Tashkent geometry
   bounds = tashkent_geom.bounds().getInfo()['coordinates'][0]
   # bounds format: [[minLon, minLat], [minLon, maxLat], [maxLon, maxLat], [maxLon, minLat]]
   ```

4. **Includes bbox in manifest**
   ```json
   {
     "maps": [
       {
         "date": "2024-06-04",
         "url": "https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev/landsat-lst-scenes/tashkent_2024-06-04.png",
         "bounds": [[69.08, 41.48], [69.50, 41.48], [69.50, 41.18], [69.08, 41.18]]
       }
     ]
   }
   ```

### Frontend Updates (Completed)

✅ Updated `LST_RASTER_BOUNDS` to more accurate Tashkent bounds
✅ Modified manifest type to support optional `bounds` field per map entry
✅ Updated PNG rendering to use dynamic bounds from manifest when available

## Precise Bounding Box

Based on the Tashkent city boundary GeoJSON analysis:

```typescript
// More accurate bounds for Tashkent (approximate from city boundary)
const LST_RASTER_BOUNDS: RasterBounds = [
  [69.10, 41.48],  // top-left (northwest)
  [69.50, 41.48],  // top-right (northeast)
  [69.50, 41.18],  // bottom-right (southeast)
  [69.10, 41.18]   // bottom-left (southwest)
];
```

**Note:** The backend script should calculate exact bounds from `tashkent_city.geojson` geometry to ensure pixel-perfect alignment.

## Testing Checklist

- [ ] Backend exports LST scenes in EPSG:4326
- [ ] PNG files are clipped to Tashkent boundary (no rotated rectangle)
- [ ] Manifest includes `bounds` field for each scene
- [ ] Frontend loads PNG with correct alignment to mahalla boundaries
- [ ] No visible tilt or offset in overlay
- [ ] LST data values match mahalla statistics

## References

- **UTM Zone 43N**: EPSG:32643 (covers Tashkent longitude ~69°E)
- **WGS84**: EPSG:4326 (standard geographic coordinates)
- **CRS84**: OGC identifier for WGS84 (used in city boundary file)
- **Landsat Resolution**: 30m thermal band, ~100m for thermal in older products

## Next Steps

1. Create backend Python script with correct projection handling
2. Test with a single date first to verify alignment
3. Update manifest with bounds metadata
4. Process full date range once verified
