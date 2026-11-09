# Layer Selector Implementation

## Overview

Replaced the combined mahalla/district + LST raster overlay with **exclusive layer selection**. Users can now toggle between three visualization modes:

1. **Mahalla View** - Neighborhood-level LST polygons
2. **District View** - District-level aggregated LST polygons  
3. **LST Raster** - Continuous thermal imagery only

## Key Changes

### 1. New Layer Mode Type

```typescript
type LayerMode = "mahalla" | "district" | "raster";
```

Separate from the existing `Mode` type (used by fallback map).

### 2. Three-Way Segmented Control

**Location:** `map-header` in `App.tsx`

```tsx
<div className="segmented">
  <button
    className={layerMode === "mahalla" ? "active" : ""}
    onClick={() => setLayerMode("mahalla")}
  >
    Mahallas
  </button>
  <button
    className={layerMode === "district" ? "active" : ""}
    onClick={() => setLayerMode("district")}
  >
    Districts
  </button>
  <button
    className={layerMode === "raster" ? "active" : ""}
    onClick={() => setLayerMode("raster")}
  >
    LST Raster
  </button>
</div>
```

### 3. MapboxMap Component Refactor

**Props Changed:**

- ~~`features: Mahalla[]`~~ → `mahallas: Mahalla[]` + `districts: District[]`
- Added `layerMode: LayerMode`
- Added `selectedFeatureId: number | null` (for selection tracking)

**Layer Visibility Logic:**

All layers are added to the map, but visibility is controlled via `layout: { visibility }`:

```typescript
layout: { visibility: layerMode === "mahalla" ? "visible" : "none" }
```

### 4. Polygon Selection with Highlight Effect

**Feature State Approach:**

Using Mapbox `feature-state` for efficient selection without re-rendering:

```typescript
// On click
instance.setFeatureState(
  { source: "mahallas", id: mahalla_id }, 
  { selected: true }
);
```

**Visual Highlight:**

Two-part effect:
1. **Increased opacity** on the fill layer when selected (82% → 95%)
2. **Bright outline** via dedicated `line` layer:

```typescript
instance.addLayer({
  id: "mahalla-selected",
  type: "line",
  source: "mahallas",
  paint: {
    "line-color": "#fff8e8",
    "line-width": [
      "case",
      ["boolean", ["feature-state", "selected"], false], 3,
      0,
    ],
    "line-offset": -1,
  },
});
```

**Result:** Selected polygon appears **slightly larger** due to the thick outline extending beyond the fill.

### 5. District Aggregation

Districts show **population-weighted LST averages**:

```typescript
const districtLookup = new Map<number, number>();
districts.forEach((d) => {
  const rows = mahallas.filter(
    (m) => m.properties.district_id === d.properties.d_id,
  );
  const pop = rows.reduce((a, m) => a + Number(m.properties.population || 1), 0);
  const mean = rows.reduce(
    (a, m) => a + Number(m.properties.lst_mean || 0) * Number(m.properties.population || 1),
    0,
  ) / Math.max(pop, 1);
  districtLookup.set(d.properties.d_id, mean);
});
```

District geometries are enhanced with `lst_mean` property for color interpolation.

### 6. Layer Rendering Order

**Z-index stacking (bottom to top):**

```
0. Basemap (satellite or dark)
1. LST Raster (when layerMode === "raster")
2. District Fill (when layerMode === "district")
3. District Outline (borders)
4. District Selected Highlight
5. Mahalla Fill (when layerMode === "mahalla")
6. Mahalla Selected Highlight
7. Navigation Controls (UI)
```

## Visual Behavior

### Mahalla Mode
- Shows ~200 neighborhood polygons
- Filled with LST gradient
- Click → thick white outline appears
- Opacity increases slightly on selection
- Hover → cursor changes to pointer

### District Mode
- Shows 12 district polygons
- Filled with population-weighted LST
- Dark outline (#2a3635) always visible
- Click → thick white outline appears
- Selects first mahalla in district for sidebar detail

### LST Raster Mode
- Only the continuous thermal image is visible
- No polygons
- User-controlled opacity slider (0-100%)
- Date selector calendar remains active
- No clickable features

## State Management

### New State Variables

```typescript
const [layerMode, setLayerMode] = useState<LayerMode>("mahalla");
```

Existing `mode` state kept for fallback SVG map compatibility.

### Raster Status

`onRasterStatus()` now only fires when `layerMode === "raster"`, preventing false "missing" alerts when polygons are shown.

## Click Handling

**Mahalla Clicks:**

```typescript
instance.on("click", "mahalla-fill", (e) => {
  const id = e.features?.[0]?.properties?.mahalla_id;
  const item = mahallas.find((f) => f.properties.mahalla_id === Number(id));
  if (!item) return;
  
  // Clear previous selection
  if (lastSelectedMahalla !== null) {
    instance.setFeatureState(
      { source: "mahallas", id: lastSelectedMahalla }, 
      { selected: false }
    );
  }
  
  // Set new selection
  lastSelectedMahalla = Number(id);
  instance.setFeatureState(
    { source: "mahallas", id: lastSelectedMahalla }, 
    { selected: true }
  );
  
  onSelect(item);
});
```

**District Clicks:**

Similar pattern, but selects first mahalla in district for detail panel display.

## Styling

No CSS changes required — existing `.segmented button` styles work with 3 buttons.

## Backward Compatibility

- **Fallback Map:** Still uses old `mode` state (mahalla/district toggle)
- **Data Loading:** Unchanged
- **Detail Panel:** Unchanged
- **Sidebar:** Unchanged

## Performance

**Optimizations:**

1. **Feature State** instead of layer filtering → no re-render on selection
2. **PromoteId** on sources → efficient feature state lookups
3. **Layer visibility toggle** → instant switching, no re-add
4. **Aggregation on load** → districts computed once, not per frame

## Testing Checklist

- [x] TypeScript compiles without errors
- [ ] Three-way selector toggles layers correctly
- [ ] Only one layer visible at a time
- [ ] Mahalla selection shows white outline
- [ ] District selection shows white outline
- [ ] Selected polygon appears slightly larger (outline effect)
- [ ] Hover cursor changes to pointer
- [ ] Click deselects previous, selects new
- [ ] Detail panel shows correct data for selection
- [ ] LST raster mode hides polygons
- [ ] Opacity slider works in all modes
- [ ] Calendar date change refreshes raster
- [ ] Satellite/dark basemap toggle works

## Known Limitations

1. **Zoom-independent outline width** - Thick lines remain same pixel width at all zoom levels
2. **No smooth transitions** - Layer switches are instant (can add CSS transitions later)
3. **District detail** - Clicking district shows first mahalla's detail, not aggregated district stats

## Future Enhancements

- [ ] Add district-level detail panel variant
- [ ] Smooth fade transitions between layer modes
- [ ] Scale-dependent line widths (zoom-based)
- [ ] Touch/mobile gesture optimization
- [ ] Keyboard shortcuts (M/D/R keys)
- [ ] Layer legend updates per mode

---

## Files Modified

- `src/App.tsx` - Core implementation
- `LAYER_SELECTOR_IMPLEMENTATION.md` - This document

## Git Commit Message

```
feat: Add exclusive layer selector for mahalla/district/raster views

- Replace combined overlay with 3-way selector
- Add polygon highlight on click using feature-state
- Implement bright outline for "bigger" selection effect
- Calculate district LST as population-weighted averages
- Use layout visibility for instant layer switching
- Maintain backward compatibility with fallback map
```
