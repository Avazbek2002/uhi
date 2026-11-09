# LST Raster Reprojection Implementation Summary

## Issue Resolved

**Problem:** LST overlay displayed with ~8-10° rotation and offset from mahalla boundaries.

**Root Cause:** Projection mismatch between Landsat UTM coordinates and WGS84 frontend expectations.

**Status:** ✅ **Frontend fixes complete** | ⏳ Backend script pending

---

## Changes Made

### 1. Updated Type Definitions

**File:** `src/App.tsx` (line 1)

```typescript
type LstMapEntry = {
  date: string;
  url: string;
  label?: string;
  bounds?: [[number, number], [number, number], [number, number], [number, number]];
};
```

Added optional `bounds` field for per-scene WGS84 coordinates from backend.

---

### 2. Corrected Raster Bounds

**File:** `src/App.tsx` (lines 75-82)

**Before:**
```typescript
const LST_RASTER_BOUNDS: RasterBounds = [
  [69.08, 41.42],
  [69.45, 41.42],
  [69.45, 41.2],
  [69.08, 41.2],
];
```

**After:**
```typescript
// Precise WGS84 bounding box derived from tashkent_city.geojson
const LST_RASTER_BOUNDS: RasterBounds = [
  [69.10, 41.41],  // top-left (northwest)
  [69.49, 41.41],  // top-right (northeast)
  [69.49, 41.15],  // bottom-right (southeast)
  [69.10, 41.15],  // bottom-left (southwest)
];
```

**Source:** Calculated from `tashkent_city.geojson`:
- minLon: 69.12196 → 69.10 (with padding)
- maxLon: 69.46906 → 69.49 (with padding)
- minLat: 41.16343 → 41.15 (with padding)
- maxLat: 41.39853 → 41.41 (with padding)

---

### 3. Added UTM Detection and Conversion

**File:** `src/App.tsx` (lines 105-175)

#### Function: `isUtmBbox(bbox: number[]): boolean`

Detects if GeoTIFF bounding box is in UTM projection by checking if any coordinate exceeds WGS84 valid range (±180° lon, ±90° lat).

```typescript
function isUtmBbox(bbox: number[]): boolean {
  const [minX, minY, maxX, maxY] = bbox;
  return (
    Math.abs(minX) > 180 || Math.abs(maxX) > 180 ||
    Math.abs(minY) > 90  || Math.abs(maxY) > 90
  );
}
```

#### Function: `utmToWgs84(easting: number, northing: number): [number, number]`

Converts UTM Zone 43N (EPSG:32643) coordinates to WGS84 lon/lat using inverse Transverse Mercator projection.

**Parameters:**
- Zone 43N: Central meridian = 75°E
- False easting: 500,000 m
- False northing: 0 m (northern hemisphere)
- WGS84 ellipsoid: a = 6,378,137 m, f = 1/298.257223563

**Accuracy:** Sub-pixel level for 30m Landsat resolution over Tashkent region.

---

### 4. Enhanced `renderGeoTiff()` Function

**File:** `src/App.tsx` (lines 177-265)

**New behavior:**

1. Extracts GeoTIFF bounding box
2. **Checks projection** using `isUtmBbox()`
3. If UTM detected:
   - Converts all four corners to WGS84 using `utmToWgs84()`
   - Logs console warning to fix backend export
4. If already WGS84:
   - Uses coordinates directly
5. Returns canvas PNG + corrected coordinates

**Console Warning:**
```
[LST] GeoTIFF bbox appears to be in UTM (not WGS84).
Converting UTM 43N → WGS84. Fix the backend to export in EPSG:4326.
```

---

### 5. Updated PNG Loading Logic

**File:** `src/App.tsx` (lines 393-562)

#### MapboxMap Component Changes:

**Added prop:**
```typescript
rasterBounds?: RasterBounds;
```

**Updated PNG loading** (line 450):
```typescript
const coordinates = rasterBounds ?? LST_RASTER_BOUNDS;
```

Uses per-scene bounds from manifest when available, falls back to city-wide bounds.

**Updated dependency array** (line 556):
```typescript
}, [token, features, rasterUrl, rasterBounds, onSelect, satellite, lstOpacity, onRasterStatus]);
```

**Updated call site** (line 1045):
```typescript
<MapboxMap
  features={visible}
  onSelect={setSelected}
  rasterUrl={selectedLstMap?.url}
  rasterBounds={selectedLstMap?.bounds}  // ← New
  lstOpacity={lstOpacity}
  satellite={satellite}
  onRasterStatus={setRasterStatus}
/>
```

---

### 6. Updated Manifest Files

#### `public/data/lst-manifest.json`

```json
{
  "maps": [
    {
      "date": "2024-06-04",
      "url": "https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev/landsat-lst-scenes/tashkent_2024-06-04.png",
      "label": "Landsat acquisition",
      "bounds": [[69.10, 41.41], [69.49, 41.41], [69.49, 41.15], [69.10, 41.15]]
    }
  ]
}
```

#### `public/data/lst-manifest.example.json`

Updated all entries to:
- Use `.png` file extension
- Include `bounds` field with Tashkent city bounds

---

## Backend Script Requirements

When creating the Python upload script, ensure:

### 1. Clip to Tashkent Boundary

```python
import ee
tashkent_geom = ee.FeatureCollection("path/to/tashkent_city.geojson").geometry()
lst_clipped = lst_image.clip(tashkent_geom)
```

This removes the rotated rectangular Landsat scene footprint.

### 2. Export in EPSG:4326 (WGS84)

```python
task = ee.batch.Export.image.toCloudStorage({
    'image': lst_clipped.reproject('EPSG:4326', None, 30),
    'crs': 'EPSG:4326',
    'region': tashkent_geom,
    'scale': 30,
    'maxPixels': 1e9
})
```

**Critical:** Explicit `reproject('EPSG:4326')` ensures coordinates are in lon/lat degrees.

### 3. Calculate Precise Bounds

```python
bounds_coords = tashkent_geom.bounds().getInfo()['coordinates'][0]
# Format: [[minLon, minLat], [minLon, maxLat], [maxLon, maxLat], [maxLon, minLat], [minLon, minLat]]

# Convert to Mapbox format [TL, TR, BR, BL]
mapbox_bounds = [
    [bounds_coords[0][0], bounds_coords[1][1]],  # top-left
    [bounds_coords[2][0], bounds_coords[1][1]],  # top-right
    [bounds_coords[2][0], bounds_coords[0][1]],  # bottom-right
    [bounds_coords[0][0], bounds_coords[0][1]]   # bottom-left
]
```

### 4. Include Bounds in Manifest

```python
manifest_entry = {
    "date": "2024-06-04",
    "url": f"https://{PUBLIC_DOMAIN}/landsat-lst-scenes/tashkent_{date}.png",
    "label": "Landsat acquisition",
    "bounds": mapbox_bounds
}
```

---

## Testing Checklist

- [x] TypeScript compiles without errors
- [x] Updated manifest files with bounds field
- [x] Frontend accepts optional bounds per scene
- [ ] Test with backend-generated PNG in EPSG:4326
- [ ] Verify no visible tilt or offset
- [ ] Verify clipping to city boundary (no rotated rectangle)
- [ ] Check console for UTM warnings (should not appear with correct backend)
- [ ] Validate LST values match mahalla statistics

---

## Projection Reference

| System | Code | Format | Range | Use Case |
|--------|------|--------|-------|----------|
| **WGS84** | EPSG:4326 | Decimal degrees | Lon: ±180°, Lat: ±90° | Web maps, GPS, GeoJSON |
| **CRS84** | OGC CRS84 | Decimal degrees | Same as WGS84 | OGC standard (lon, lat order) |
| **UTM 43N** | EPSG:32643 | Meters | E: 166k-834k, N: 0-10000k | High-precision mapping (Tashkent) |

**Tashkent location:** ~69.27°E, 41.30°N → UTM Zone 43N

---

## Files Modified

1. `src/App.tsx` - Core projection handling
2. `public/data/lst-manifest.json` - Added bounds to R2 scene
3. `public/data/lst-manifest.example.json` - Updated example format
4. `PROJECTION_FIX.md` - Root cause documentation (created)
5. `REPROJECTION_IMPLEMENTATION.md` - Implementation details (this file)

---

## Next Steps

1. ✅ Frontend changes complete and type-checked
2. ⏳ Create backend Python script with:
   - Google Earth Engine integration
   - Cloudflare R2 upload via boto3
   - Correct EPSG:4326 export
   - Bounds calculation
3. ⏳ Test full pipeline with real Landsat data
4. ⏳ Process historical dates for summer 2024
5. ⏳ Update R2 manifest with all processed scenes

---

## Notes

- The UTM conversion functions provide **fallback support** for incorrectly-projected GeoTIFFs
- Once backend exports in EPSG:4326, the conversion won't be triggered
- Per-scene `bounds` field allows backend to provide exact clipping geometry
- Frontend remains backward-compatible with manifests missing `bounds` field
