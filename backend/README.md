# LST COG Processing Pipeline

## Overview

This backend pipeline processes Landsat Land Surface Temperature (LST) imagery into Cloud Optimized GeoTIFFs (COGs) and serves them as 256×256 pixel tiles via HTTP Range Requests.

**Key improvements over the old PNG approach:**
- ✅ Tile-by-tile loading (only visible tiles are fetched)
- ✅ Sharp rendering at all zoom levels
- ✅ Raw float32 temperature values preserved
- ✅ Dynamic styling via TiTiler (colormap, rescale) without re-processing
- ✅ HTTP Range Request support for efficient COG reading
- ✅ No client-side PNG rendering overhead

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Google Earth Engine                                                │
│  ├─ Landsat 8/9 Collection 2 Level-2                               │
│  ├─ ST_B10 band (thermal, Kelvin)                                  │
│  └─ Filter: <30% cloud, ±1 day from target date                    │
└──────────────┬──────────────────────────────────────────────────────┘
               │ Download GeoTIFF (30m, WGS84)
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Local Processing (process_lst_cog.py)                             │
│  ├─ Clip to tashkent_city.geojson boundary                         │
│  ├─ Reproject to EPSG:4326 if needed                               │
│  ├─ Convert to COG:                                                 │
│  │  ├─ Internal 256×256 tiles                                      │
│  │  ├─ Overview pyramid (5 levels: 2, 4, 8, 16, 32)               │
│  │  ├─ DEFLATE compression                                         │
│  │  └─ Nodata = -9999 for transparency                             │
│  └─ Validate COG (rio cogeo validate)                              │
└──────────────┬──────────────────────────────────────────────────────┘
               │ Upload COG
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Cloudflare R2 Storage                                              │
│  ├─ Bucket: landsat-lst-scenes                                     │
│  ├─ Prefix: lst-cog/                                                │
│  ├─ Public access enabled                                           │
│  ├─ CORS: Accept-Ranges header for HTTP Range Requests             │
│  └─ Manifest: lst-cog/manifest.json                                 │
└──────────────┬──────────────────────────────────────────────────────┘
               │ HTTP Range Request
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TiTiler Tile Server (https://titiler.xyz or self-hosted)          │
│  ├─ Reads COG header + requested tile extent via byte-range        │
│  ├─ Applies colormap (e.g., inferno) + rescale (20–65°C)           │
│  ├─ Returns 256×256 PNG tile                                        │
│  └─ Endpoint: /cog/tiles/{z}/{x}/{y}.png?url=<COG_URL>&...         │
└──────────────┬──────────────────────────────────────────────────────┘
               │ XYZ tile URL template
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Frontend (Mapbox GL JS)                                            │
│  ├─ Fetches only tiles visible in viewport                         │
│  ├─ Progressive loading as user pans/zooms                          │
│  └─ User-controlled opacity slider                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Setup

### 1. Install Dependencies

```bash
cd backend
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

**Required:**
- `R2_ACCOUNT_ID` — Cloudflare account ID
- `R2_ACCESS_KEY_ID` — R2 access key
- `R2_SECRET_ACCESS_KEY` — R2 secret key
- `R2_PUBLIC_DOMAIN` — R2 public domain (e.g., `pub-abc123.r2.dev`)
- `GEE_PROJECT` — Google Earth Engine project ID

**Optional:**
- `R2_BUCKET_NAME` — defaults to `landsat-lst-scenes`
- `TITILER_HOST` — TiTiler base URL (defaults to public `https://titiler.xyz`)

### 3. Enable R2 Public Access

1. Go to Cloudflare Dashboard → R2
2. Select your bucket (`landsat-lst-scenes`)
3. Settings → Public Access → Enable
4. Copy the public domain (e.g., `pub-abc123.r2.dev`)
5. Add to `.env` as `R2_PUBLIC_DOMAIN`

### 4. Configure R2 CORS (for HTTP Range Requests)

Create a CORS policy JSON file:

```json
[
  {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["Accept-Ranges", "Content-Range", "Content-Length"],
    "MaxAgeSeconds": 3600
  }
]
```

Apply via `wrangler` CLI:

```bash
wrangler r2 bucket cors put landsat-lst-scenes --cors-config=cors.json
```

Or via AWS CLI (boto3 compatible):

```bash
aws s3api put-bucket-cors \
  --endpoint-url https://<ACCOUNT_ID>.r2.cloudflarestorage.com \
  --bucket landsat-lst-scenes \
  --cors-configuration file://cors.json
```

### 5. Authenticate with Google Earth Engine

```bash
python -c "import ee; ee.Authenticate()"
```

Follow the browser prompt to authorize your GEE account.

---

## Usage

### Backfill every available Landsat scene

`backfill_lst.py` first deletes every object under the configured LST prefix,
then uploads one PNG, one GeoTIFF, and one metadata JSON per unique Landsat
8/9 scene for summer days from 2024 through 2026. The JSON preserves all
Earth Engine image properties and includes the legacy `mahalla_stats`,
`district_stats`, and UTFVI `hotspots` fields, plus public asset URLs, map
bounds, and LST conversion parameters.

```bash
python backfill_lst.py --max-cloud 30
```

The query covers June 1 through August 31 in each year. The script replaces
every object under `R2_LST_PREFIX` (default:
`landsat-lst-scenes`) before uploading the new dataset. The aggregate
discovery file is uploaded as `backfill-manifest.json` in that prefix.

### Process a Single Date

```bash
python process_lst_cog.py --date 2024-06-04
```

### Process a Date Range

Processes every 16 days (Landsat revisit cycle) between start and end dates:

```bash
python process_lst_cog.py --start 2024-05-01 --end 2024-09-30
```

### Force Reprocess

By default, dates already in the manifest are skipped. Use `--force` to reprocess:

```bash
python process_lst_cog.py --date 2024-06-04 --force
```

### Skip Bucket Purge

By default, the script purges existing files in `lst-cog/` before upload. Use `--no-purge` to keep existing files:

```bash
python process_lst_cog.py --date 2024-06-04 --no-purge
```

### Test with Local File (Skip GEE)

Useful for testing COG generation without GEE access:

```bash
python process_lst_cog.py --date 2024-06-04 --use-local ~/path/to/raw_lst.tif
```

---

## Output

### 1. COG File on R2

**URL Format:**
```
https://<R2_PUBLIC_DOMAIN>/lst-cog/tashkent_lst_<DATE>.tif
```

**Example:**
```
https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev/lst-cog/tashkent_lst_2024-06-04.tif
```

**Properties:**
- Format: GeoTIFF (COG)
- CRS: EPSG:4326 (WGS84)
- Data type: float32
- Nodata: -9999
- Unit: °C
- Compression: DEFLATE (lossless)
- Tile size: 256×256
- Overview levels: 2, 4, 8, 16, 32

### 2. Manifest JSON

**URL:**
```
https://<R2_PUBLIC_DOMAIN>/lst-cog/manifest.json
```

**Format:**
```json
{
  "updated": "2024-06-04T12:00:00Z",
  "scenes": [
    {
      "date": "2024-06-04",
      "cog_url": "https://pub-abc.r2.dev/lst-cog/tashkent_lst_2024-06-04.tif",
      "tile_url": "https://titiler.xyz/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=...&colormap_name=inferno&rescale=20,65&nodata=-9999",
      "stats": {
        "min": 24.12,
        "max": 58.34,
        "pixel_count": 182400
      }
    }
  ]
}
```

**Fields:**
- `date` — Acquisition date (YYYY-MM-DD)
- `cog_url` — Direct link to the COG file on R2
- `tile_url` — TiTiler XYZ tile template with pre-configured colormap and rescale
- `stats` — Min/max LST values and pixel count for the scene

---

## TiTiler Integration

### Self-Hosted TiTiler (Recommended for Production)

Deploy TiTiler via Docker:

```bash
docker run -p 8000:8000 ghcr.io/developmentseed/titiler:latest
```

Set `TITILER_HOST=http://localhost:8000` in `.env`.

**Deploy to cloud:**
- AWS Lambda: https://github.com/developmentseed/titiler-pgstac
- Google Cloud Run: https://cloud.google.com/run/docs
- DigitalOcean App Platform: https://docs.digitalocean.com/products/app-platform/

### Public TiTiler (Demo/Testing Only)

The script defaults to `https://titiler.xyz` — a public demo server. **Not for production use** (rate-limited, no SLA).

### Tile URL Anatomy

```
https://titiler.xyz/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png
  ?url=<URL_ENCODED_COG_URL>
  &colormap_name=inferno
  &rescale=20,65
  &nodata=-9999
```

**Parameters:**
- `url` — URL-encoded COG file URL
- `colormap_name` — Colormap (e.g., `inferno`, `viridis`, `hot`, `plasma`)
- `rescale` — Min,max temperature range for color mapping
- `nodata` — Value to treat as transparent

**Supported colormaps:**
https://github.com/cogeotiff/rio-tiler/blob/main/docs/src/colormap.md

---

## COG Validation

Validate that a COG file meets specifications:

```bash
rio cogeo validate tashkent_lst_2024-06-04_cog.tif
```

**Expected output:**
```
tashkent_lst_2024-06-04_cog.tif is a valid cloud optimized GeoTIFF
```

**Validation checks:**
- Internal tiling (256×256 or 512×512)
- Overview pyramid exists
- IFD (Image File Directory) before image data
- Overviews use same compression as main image

---

## Frontend Integration

The frontend (`src/App.tsx`) automatically detects the new COG manifest format.

**Manifest loading priority:**
1. `VITE_LST_MANIFEST_URL` (env var)
2. `/data/lst-manifest.json` (local fallback)

**Supported manifest formats:**
- **New COG format:** `{ scenes: [...] }`
- **Old PNG format:** `{ maps: [...] }` (backward compatible)
- **Array format:** `[...]`

**Raster source priority:**
1. `tile_url` from manifest → **XYZ tiled loading** (best performance)
2. `url` from manifest → single PNG (backward compat)
3. `cog_url` from manifest → client-side GeoTIFF rendering (fallback)
4. `VITE_LST_RASTER_TILE_URL` → env-var tile URL
5. `VITE_LST_RASTER_URL` → env-var static PNG

---

## Troubleshooting

### "COG validation failed"

**Cause:** Output file doesn't meet COG specifications.

**Fix:**
- Ensure rasterio >= 1.3.0
- Check that rio-cogeo is installed
- Verify overview levels are generated (check file size — should be ~1.3× original)

### "HTTP 403 Forbidden" when accessing COG

**Cause:** R2 bucket is not public or CORS not configured.

**Fix:**
1. Enable public access on the bucket
2. Apply CORS policy (see Setup step 4)
3. Test with `curl -I <COG_URL>` — should see `Accept-Ranges: bytes`

### "No Landsat scenes found"

**Cause:** No valid scene within ±1 day of the requested date with <30% cloud cover.

**Fix:**
- Try adjacent dates (Landsat revisit: 16 days)
- Increase cloud cover threshold in the script (`CLOUD_COVER < 30` → `< 50`)
- Check GEE availability: https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC09_C02_T1_L2

### "GEE authentication failed"

**Cause:** Earth Engine not authenticated.

**Fix:**
```bash
python -c "import ee; ee.Authenticate()"
```

### TiTiler returns 500 error

**Cause:** COG URL is not accessible or malformed.

**Fix:**
- Test COG URL directly: `curl -I <COG_URL>`
- Ensure URL is properly encoded in tile_url
- Check TiTiler logs (if self-hosted)

---

## Performance

### COG File Sizes

| Resolution | Dimensions | Float32 | COG (DEFLATE) | Reduction |
|------------|------------|---------|---------------|-----------|
| 30m @ Tashkent | ~1200×1000 | 4.8 MB | 1.2–1.8 MB | 60–75% |
| With overviews | +pyramid | +1.6 MB | +0.4 MB | 75% |

### Tile Loading

**Single PNG (old):**
- 1 request for ~3 MB file
- All-or-nothing loading
- Blurry at high zoom

**Tiled COG (new):**
- ~12 requests for initial viewport (256 KB total at zoom 11)
- Progressive loading as user pans/zooms
- Sharp at all zoom levels
- Only visible tiles fetched

### HTTP Range Requests

TiTiler reads only the requested tile data from the COG using HTTP Range headers:

```
GET /lst-cog/tashkent_lst_2024-06-04.tif
Range: bytes=12345-67890
```

R2 returns only the requested byte range, not the entire file.

---

## Migration from PNG Pipeline

### Old Workflow
```python
# Download LST GeoTIFF from GEE
lst = download_from_gee(date)

# Render to PNG with matplotlib
fig, ax = plt.subplots()
ax.imshow(lst, cmap='inferno', vmin=20, vmax=65)
plt.savefig('tashkent_lst.png', transparent=True)

# Upload PNG to R2
upload_to_r2('tashkent_lst.png')

# Generate manifest with PNG URLs
manifest = {"maps": [{"date": date, "url": png_url}]}
```

### New Workflow
```python
# Download LST GeoTIFF from GEE
lst_raw = download_from_gee(date)

# Clip to boundary
lst_clipped = clip_to_boundary(lst_raw)

# Convert to COG (no PNG rendering!)
convert_to_cog(lst_clipped, 'tashkent_lst_cog.tif')

# Upload COG to R2
upload_to_r2('tashkent_lst_cog.tif')

# Generate manifest with tile URLs
tile_url = build_tile_url(cog_url, min_temp, max_temp)
manifest = {"scenes": [{
    "date": date,
    "cog_url": cog_url,
    "tile_url": tile_url
}]}
```

**Key differences:**
- ❌ No `matplotlib` rendering
- ❌ No static PNG files
- ✅ Styling via TiTiler query params (change colormap without reprocessing)
- ✅ Tile-by-tile loading in browser
- ✅ Raw float32 values preserved for analysis

---

## References

- **Cloud Optimized GeoTIFF:** https://www.cogeo.org/
- **TiTiler Documentation:** https://developmentseed.org/titiler/
- **rio-cogeo:** https://cogeotiff.github.io/rio-cogeo/
- **Cloudflare R2:** https://developers.cloudflare.com/r2/
- **Google Earth Engine:** https://developers.google.com/earth-engine
- **Landsat Collection 2:** https://www.usgs.gov/landsat-missions/landsat-collection-2

---

## License

MIT License — see repository root LICENSE file.
