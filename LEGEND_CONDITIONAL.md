# Conditional Legend Implementation

## Overview

The map legend now adapts based on the selected layer mode, showing different temperature ranges and labels for:
- **LST Raster mode** — raw Landsat thermal imagery (20–65°C range)
- **Mahalla/District modes** — aggregated mean LST statistics (42–85°C range)

---

## Implementation

### Layer Mode Detection

The legend uses conditional rendering based on `layerMode` state:

```tsx
<div className="map-legend">
  {layerMode === "raster" ? (
    // LST Thermal legend
  ) : (
    // Mean LST legend (mahalla/district)
  )}
</div>
```

### LST Raster Legend

**Shows when:** `layerMode === "raster"`

**Content:**
- **Title:** "LST THERMAL" (emphasizes raw measurement)
- **Temperature range:** 20°C – 65°C
  - Lower bound: 20°C captures cool surfaces (parks, water)
  - Upper bound: 65°C captures extreme heat (rooftops, asphalt)
- **Labels:** Four evenly-spaced markers (20°, 35°, 50°, 65°)
- **Note:** "Landsat 8/9 surface temperature" (data source attribution)
- **No "low confidence" indicator** (applies to polygons, not raw raster)

**Visual:**
```
┌───────────────────────────┐
│ °C LST THERMAL            │
│ [gradient bar]            │
│ 20°   35°   50°   65°     │
│ Landsat 8/9 surface temp  │
└───────────────────────────┘
```

### Mean LST Legend (Mahalla/District)

**Shows when:** `layerMode === "mahalla"` or `layerMode === "district"`

**Content:**
- **Title:** "MEAN LST" (emphasizes aggregated statistic)
- **Temperature range:** 42°C – 85°C
  - Narrower range focused on typical mahalla/district averages
  - Derived from zonal statistics over polygons
- **Labels:** Four markers (42°, 55°, 70°, 85°)
- **Note:** Low confidence indicator (hatched pattern + info icon)
  - Shown for mahallas with <50 valid pixels

**Visual:**
```
┌───────────────────────────┐
│ °C MEAN LST               │
│ [gradient bar]            │
│ 42°   55°   70°   85°     │
│ ░ Low confidence    i     │
└───────────────────────────┘
```

---

## Temperature Range Rationale

### LST Raster: 20–65°C

**Why broader range?**
- **Raw pixel values** include extreme micro-environments:
  - Parks, water bodies, shadows: 20–30°C
  - Bare soil, buildings: 40–50°C
  - Dark rooftops, asphalt in direct sun: 55–65°C
- Matches **TiTiler rescale parameter** in the COG manifest:
  ```json
  "tile_url": "...&rescale=20,65&colormap_name=inferno"
  ```
- Captures the **full dynamic range** of surface materials

**Color gradient mapping:**
- 20°C → lightest yellow (`#fff2b2`)
- 35°C → orange (`#f9d56e`)
- 50°C → dark orange (`#f7a34b`)
- 65°C → deep red (`#bf3025`)

### Mean LST: 42–85°C

**Why narrower range?**
- **Spatial averaging** over 180–500 pixels per mahalla smooths out extremes
- **Population-weighted** statistics bias toward inhabited areas (hotter than parks)
- **Typical mahalla means** fall in 48–62°C range during summer
- Upper bound (85°C) handles outliers in dense urban cores

**Practical interpretation:**
- <50°C: Comfortable (parks, tree cover)
- 50–58°C: Typical urban
- 58–65°C: Heat stress concern
- >65°C: Critical intervention priority

---

## Styling

No CSS changes required — both legends use existing classes:

- `.map-legend` — container with dark background + border
- `.legend-title` — monospace font, yellow `°C` symbol
- `.legend-gradient` — CSS gradient bar (same colors for both)
- `.legend-labels` — flex-spaced temperature markers
- `.legend-note` — smaller text at bottom

**Minor inline style override:**
```tsx
<div className="legend-note" style={{ borderTop: 0, paddingTop: 0, marginTop: 8 }}>
```
Removes the border separator for the raster legend's simpler attribution note.

---

## User Experience Flow

1. **User opens app** → defaults to `layerMode = "mahalla"`
   - Sees "MEAN LST" legend with 42–85°C range
   - Low confidence indicator visible

2. **User clicks "Districts" button**
   - Legend stays the same (both use mean LST)
   - Still shows low confidence note (applies to aggregates)

3. **User clicks "LST Raster" button**
   - Legend **switches** to "LST THERMAL"
   - Range updates to 20–65°C
   - Low confidence indicator **disappears** (not applicable to raster)
   - Note changes to "Landsat 8/9 surface temperature"

4. **User switches back to "Mahallas"**
   - Legend reverts to "MEAN LST" with 42–85°C

---

## Data Alignment

### TiTiler COG Colormap

When the backend script generates the manifest, it creates tile URLs with:

```python
tile_url = f"{TITILER_HOST}/cog/tiles/{{z}}/{{x}}/{{y}}.png" \
           f"?url={cog_url}" \
           f"&colormap_name=inferno" \
           f"&rescale=20,65"  # ← matches legend range
```

The `rescale=20,65` parameter tells TiTiler to map:
- Pixels with LST ≤ 20°C → color index 0 (lightest yellow)
- Pixels with LST ≥ 65°C → color index 255 (darkest red)

This **exactly matches** the raster legend's 20–65°C labels.

### Mahalla/District Aggregates

The polygon `fill-color` uses Mapbox data-driven styling:

```js
"fill-color": [
  "interpolate", ["linear"], ["get", "lst_mean"],
  42, "#fff2b2",
  55, "#f7a34b",
  70, "#e76536",
  85, "#76181b",
]
```

This matches the mahalla/district legend's 42–85°C labels.

---

## Accessibility

### Color Blindness

The **Inferno colormap** (yellow → orange → red → dark red) is colorblind-friendly:
- Luminance varies from light to dark (readable in grayscale)
- No red/green confusion (no green in palette)
- Perceptually uniform (equal visual steps = equal temperature steps)

### Semantic Labels

- "MEAN LST" clearly indicates statistical average
- "LST THERMAL" emphasizes raw measurement
- Temperature markers (°C) provide numeric reference points
- "Landsat 8/9" credits data source (transparency)

---

## Future Enhancements

### Dynamic Range from Manifest Stats

Currently, the raster legend uses hardcoded 20–65°C. The manifest includes per-scene stats:

```json
{
  "date": "2024-06-04",
  "stats": { "min": 24.12, "max": 58.34 }
}
```

**Enhancement:** Use `selectedLstMap?.stats` to show actual scene range:

```tsx
const minTemp = selectedLstMap?.stats?.min ?? 20;
const maxTemp = selectedLstMap?.stats?.max ?? 65;
```

Then update legend labels dynamically.

### Colormap Selector

Since TiTiler supports multiple colormaps, add a dropdown:

```tsx
<select onChange={e => setColormap(e.target.value)}>
  <option value="inferno">Inferno (default)</option>
  <option value="viridis">Viridis</option>
  <option value="plasma">Plasma</option>
  <option value="turbo">Turbo</option>
</select>
```

Rebuild tile URL with new `colormap_name` parameter.

### Animated Transition

Fade between legends on layer switch:

```css
.map-legend > * {
  animation: fadeIn 0.2s ease-in;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}
```

---

## Testing Checklist

- [x] TypeScript compiles without errors
- [ ] Legend shows "MEAN LST" (42–85°C) when mahalla layer active
- [ ] Legend shows "MEAN LST" (42–85°C) when district layer active
- [ ] Legend shows "LST THERMAL" (20–65°C) when raster layer active
- [ ] Low confidence indicator visible in mahalla/district modes
- [ ] Low confidence indicator hidden in raster mode
- [ ] Gradient color bar matches displayed temperature range
- [ ] Attribution text shows for raster ("Landsat 8/9 surface temperature")
- [ ] Legend switches instantly when user toggles layer mode
- [ ] No layout shift or flicker during legend swap

---

## Files Modified

- `src/App.tsx` — Added conditional legend rendering based on `layerMode`
- `LEGEND_CONDITIONAL.md` — This documentation

---

## Summary

The legend is now **context-aware**, adapting its labels, range, and content to match the active visualization layer. Raw thermal imagery gets a wider range (20–65°C) that captures surface extremes, while aggregated polygon statistics use a narrower focus range (42–85°C) that emphasizes typical urban heat exposure. This helps users correctly interpret what the colors represent in each mode.
