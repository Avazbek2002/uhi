import { useEffect, useMemo, useRef, useState } from "react";
import type { Feature, FeatureCollection, Geometry } from "geojson";
import {
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import mapboxgl from "mapbox-gl";
import { fromUrl } from "geotiff";

// ── Types ──────────────────────────────────────────────────────────────────
type Hotspot = {
  id: string;
  mahalla_id: number;
  mahalla_name: string;
  district_name: string;
  coordinates: [number, number]; // [lng, lat]
  peak_lst: number;
  mean_lst: number;
  area_ha: number;
  severity: "Moderate" | "Severe" | "Critical";
};

type LstMapEntry = {
  date: string;
  cog_url?: string;
  tile_url?: string;
  url?: string;
  label?: string;
  bounds?: [[number, number], [number, number], [number, number], [number, number]];
  stats?: { min: number; max: number; pixel_count: number };
  mahalla_stats?: Record<string | number, { mean: number; min: number; max: number }>;
  district_stats?: Record<string | number, { mean: number; min: number; max: number }>;
  hotspots?: Hotspot[];
};

type RawDistrictProps = {
  feature_type: "district";
  full_name: string;
  centroid?: { coordinates: [number, number] };
  lst_mean?: number;
  lst_min?: number;
  lst_max?: number;
  [key: string]: unknown;
};

type RawMahallaProps = {
  feature_type: "mahalla";
  name: string;
  parent_district: string;
  lst_mean?: number;
  lst_std?: number;
  lst_min?: number;
  lst_max?: number;
  lst_count?: number;
  low_confidence?: boolean;
  [key: string]: unknown;
};

type Props = {
  mahalla_id: number;
  mahalla_name?: string;
  district_id: number;
  lst_baseline?: number;
  lst_mean?: number;
  lst_std?: number;
  lst_min?: number;
  lst_max?: number;
  lst_count?: number;
  low_confidence?: boolean;
};

type Mahalla = Feature<Geometry, Props> & { properties: Props };

type DistrictProps = {
  d_id: number;
  full_name?: string;
  lst_mean?: number;
  lst_min?: number;
  lst_max?: number;
};

type District = Feature<Geometry, DistrictProps> & {
  properties: DistrictProps;
};

type Mode = "mahalla" | "district";
type LayerMode = "mahalla" | "district" | "raster";
type RasterBoundaryMode = "none" | "mahalla" | "district";
type RasterBounds = [
  [number, number],
  [number, number],
  [number, number],
  [number, number],
];

// ── Constants & Helpers ───────────────────────────────────────────────────
const DISTRICT_NAMES: Record<number, string> = {};
const HEAT_COLORS = [
  "#fff2b2",
  "#f9d56e",
  "#f7a34b",
  "#e76536",
  "#bf3025",
  "#76181b",
];
const fmt = new Intl.NumberFormat("en-US");

const LST_RASTER_URL = import.meta.env.VITE_LST_RASTER_URL as string | undefined;
const LST_RASTER_TILE_URL = import.meta.env.VITE_LST_RASTER_TILE_URL as string | undefined;
const LST_GEOTIFF_URL = import.meta.env.VITE_LST_GEOTIFF_URL as string | undefined;

const LST_RASTER_BOUNDS: RasterBounds = [
  [69.126, 41.396],
  [69.466, 41.396],
  [69.466, 41.166],
  [69.126, 41.166],
];
const HOTSPOT_MIN_ZOOM = 10;
const DEFAULT_LST_MEAN = 55;
const LST_FILL_COLOR: mapboxgl.ExpressionSpecification = [
  "interpolate", ["linear"], ["to-number", ["get", "lst_mean"], DEFAULT_LST_MEAN],
  42, "#fff45f",
  55, "#ffd34d",
  70, "#f59e0b",
  85, "#ea580c",
];

function colorFor(value: number, min = 42, max = 85) {
  const pct = Math.max(0, Math.min(0.999, (value - min) / (max - min)));
  return HEAT_COLORS[Math.floor(pct * HEAT_COLORS.length)];
}

function normalizeHotspotCoordinates(
  coordinates: [number, number],
): [number, number] | null {
  const [first, second] = coordinates;

  // UTM easting/northing values — completely unrenderable by Mapbox
  if (Math.abs(first) > 180 || Math.abs(second) > 90) {
    return null;
  }

  // Detect swapped [lat, lng] order: first value looks like a Tashkent latitude
  const firstLooksLikeLat =
    first >= LST_RASTER_BOUNDS[2][1] && first <= LST_RASTER_BOUNDS[0][1];
  const secondLooksLikeLng =
    second >= LST_RASTER_BOUNDS[0][0] && second <= LST_RASTER_BOUNDS[1][0];

  if (firstLooksLikeLat && secondLooksLikeLng) {
    return [second, first];
  }

  return coordinates;
}

function finiteOrFallback(value: number | undefined, fallback: number) {
  return Number.isFinite(value) ? Number(value) : fallback;
}

function baselineLstFromValue(value: unknown, fallback = DEFAULT_LST_MEAN) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  const clamped = Math.max(0, Math.min(100, numeric));
  return Math.round((42 + (clamped / 100) * 43) * 10) / 10;
}

function buildDistrictFeatureCollection(
  mahallas: Mahalla[],
  districts: District[],
): FeatureCollection {
  const districtLookup = new Map<number, { mean: number; min: number; max: number }>();
  districts.forEach((d) => {
    const rows = mahallas.filter((m) => m.properties.district_id === d.properties.d_id);
    const rowMeans = rows
      .map((m) => m.properties.lst_mean)
      .filter((value): value is number => Number.isFinite(value));
    const mean = finiteOrFallback(
      d.properties.lst_mean,
      rowMeans.length
        ? Math.round((rowMeans.reduce((sum, value) => sum + value, 0) / rowMeans.length) * 10) / 10
        : DEFAULT_LST_MEAN,
    );
    const min = finiteOrFallback(
      d.properties.lst_min,
      rows.length ? Math.min(...rows.map((m) => finiteOrFallback(m.properties.lst_min, mean))) : mean - 5,
    );
    const max = finiteOrFallback(
      d.properties.lst_max,
      rows.length ? Math.max(...rows.map((m) => finiteOrFallback(m.properties.lst_max, mean))) : mean + 5,
    );
    districtLookup.set(d.properties.d_id, { mean, min, max });
  });

  return {
    type: "FeatureCollection",
    features: districts.map((d) => {
      const stats = districtLookup.get(d.properties.d_id) ?? {
        mean: DEFAULT_LST_MEAN,
        min: DEFAULT_LST_MEAN - 5,
        max: DEFAULT_LST_MEAN + 5,
      };
      return {
        ...d,
        id: d.properties.d_id,
        properties: {
          ...d.properties,
          lst_mean: stats.mean,
          lst_min: stats.min,
          lst_max: stats.max,
        },
      };
    }),
  };
}

function formatMapDate(date: string) {
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(`${date}T12:00:00Z`));
}

function isImageRasterUrl(url: string) {
  return /\.(png|jpe?g|webp)(\?.*)?$/i.test(url);
}

function isUtmBbox(bbox: number[]): boolean {
  const [minX, minY, maxX, maxY] = bbox;
  return (
    Math.abs(minX) > 180 || Math.abs(maxX) > 180 ||
    Math.abs(minY) > 90  || Math.abs(maxY) > 90
  );
}

function normalizeBoundsToWgs84(bounds: RasterBounds): RasterBounds {
  const needsConversion = bounds.some(([x, y]) => Math.abs(x) > 180 || Math.abs(y) > 90);
  if (!needsConversion) return bounds;
  return bounds.map(([x, y]) => utmToWgs84(x, y)) as RasterBounds;
}

function utmToWgs84(easting: number, northing: number): [number, number] {
  const a = 6378137.0;
  const f = 1 / 298.257223563;
  const k0 = 0.9996;
  const e2 = 2 * f - f * f;
  const e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2));
  const lon0 = 69 * (Math.PI / 180); // UTM zone 42N central meridian — covers Tashkent (~69.1–69.5°E)

  const x = easting - 500000;
  const y = northing;

  const M = y / k0;
  const mu = M / (a * (1 - e2 / 4 - 3 * e2 * e2 / 64 - 5 * e2 * e2 * e2 / 256));
  const p1 =
    mu +
    (3 * e1 / 2 - 27 * e1 * e1 * e1 / 32) * Math.sin(2 * mu) +
    (21 * e1 * e1 / 16 - 55 * e1 * e1 * e1 * e1 / 32) * Math.sin(4 * mu) +
    (151 * e1 * e1 * e1 / 96) * Math.sin(6 * mu);
  const e1sq = e2 / (1 - e2);
  const N1 = a / Math.sqrt(1 - e2 * Math.sin(p1) * Math.sin(p1));
  const T1 = Math.tan(p1) * Math.tan(p1);
  const C1 = e1sq * Math.cos(p1) * Math.cos(p1);
  const R1 = a * (1 - e2) / Math.pow(1 - e2 * Math.sin(p1) * Math.sin(p1), 1.5);
  const D = x / (N1 * k0);

  const lat =
    p1 -
    (N1 * Math.tan(p1) / R1) *
      (D * D / 2 -
        (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * e1sq) * Math.pow(D, 4) / 24 +
        (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * e1sq - 3 * C1 * C1) *
          Math.pow(D, 6) / 720);
  const lon =
    lon0 +
    (D -
      (1 + 2 * T1 + C1) * D * D * D / 6 +
      (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * e1sq + 24 * T1 * T1) *
        Math.pow(D, 5) / 120) /
      Math.cos(p1);

  return [lon * (180 / Math.PI), lat * (180 / Math.PI)];
}

async function renderGeoTiff(url: string) {
  const tiff = await fromUrl(url);
  const image = await tiff.getImage();
  const width = image.getWidth();
  const height = image.getHeight();
  const rasters = await image.readRasters({ interleave: true });
  const values = rasters as unknown as ArrayLike<number>;
  const valid = Array.from(values).filter((value) => Number.isFinite(value));
  const min = Math.min(42, Math.min(...valid));
  const max = Math.max(85, Math.max(...valid));
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas is unavailable");
  const pixels = context.createImageData(width, height);
  for (let index = 0; index < values.length; index += 1) {
    const value = Number(values[index]);
    const pixel = index * 4;
    if (!Number.isFinite(value)) {
      pixels.data[pixel + 3] = 0;
      continue;
    }
    const color =
      HEAT_COLORS[
        Math.min(
          HEAT_COLORS.length - 1,
          Math.floor(
            Math.max(0, Math.min(0.999, (value - min) / (max - min))) *
              HEAT_COLORS.length,
          ),
        )
      ];
    const hex = color.slice(1);
    pixels.data[pixel] = parseInt(hex.slice(0, 2), 16);
    pixels.data[pixel + 1] = parseInt(hex.slice(2, 4), 16);
    pixels.data[pixel + 2] = parseInt(hex.slice(4, 6), 16);
    pixels.data[pixel + 3] = 255;
  }
  context.putImageData(pixels, 0, 0);

  const rawBbox = image.getBoundingBox();
  let coordinates: RasterBounds;

  if (isUtmBbox(rawBbox)) {
    const [minX, minY, maxX, maxY] = rawBbox;
    const topLeft     = utmToWgs84(minX, maxY);
    const topRight    = utmToWgs84(maxX, maxY);
    const bottomRight = utmToWgs84(maxX, minY);
    const bottomLeft  = utmToWgs84(minX, minY);
    coordinates = [topLeft, topRight, bottomRight, bottomLeft];
  } else {
    coordinates = [
      [rawBbox[0], rawBbox[3]],
      [rawBbox[2], rawBbox[3]],
      [rawBbox[2], rawBbox[1]],
      [rawBbox[0], rawBbox[1]],
    ] as RasterBounds;
  }

  return { url: canvas.toDataURL("image/png"), coordinates };
}

function parseRenderable(
  geojson: { features: Feature<Geometry, Record<string, unknown>>[] },
): { mahallas: Mahalla[]; districts: District[] } {
  const rawDistricts = geojson.features.filter(
    (f) => f.properties?.["feature_type"] === "district",
  ) as Feature<Geometry, RawDistrictProps>[];

  // Build full_name → district_id lookup from mahalla properties.
  // Mahallas carry both district_id (numeric, e.g. 2611) and parent_district
  // (string matching district full_name, e.g. "Toshkent, Bektemir").
  // This is the only reliable way to assign the correct numeric d_id to each
  // district polygon without relying on array ordering.
  const rawMahallasAll = geojson.features.filter(
    (f) => f.properties?.["feature_type"] === "mahalla",
  );
  const fullNameToDistrictId = new Map<string, number>();
  rawMahallasAll.forEach((f) => {
    const parentName = String(f.properties?.["parent_district"] ?? "");
    const dId = Number(f.properties?.["district_id"] ?? 0);
    if (parentName && dId > 0 && !fullNameToDistrictId.has(parentName)) {
      fullNameToDistrictId.set(parentName, dId);
    }
  });

  const districtNameToId = new Map<string, number>();
  rawDistricts.forEach((f, i) => {
    const name = f.properties.full_name ?? `District ${i + 1}`;
    // Look up the real district_id from mahalla data; fall back to sequential
    const id = fullNameToDistrictId.get(name) ?? (i + 1);
    districtNameToId.set(name, id);
    DISTRICT_NAMES[id] = name;
  });

  const districts: District[] = rawDistricts.map((f) => {
    const p = f.properties;
    const name = p.full_name ?? "";
    const id = fullNameToDistrictId.get(name) ?? districtNameToId.get(name) ?? 0;
    return {
      ...f,
      properties: {
        d_id: id,
        full_name: name,
        lst_mean: undefined,
        lst_min: undefined,
        lst_max: undefined,
      },
    };
  });

  const rawMahallas = geojson.features.filter(
    (f) => f.properties?.["feature_type"] === "mahalla",
  ) as Feature<Geometry, RawMahallaProps>[];

  const mahallas: Mahalla[] = rawMahallas.map((f, index) => {
    const p = f.properties;
    const raw = p as Record<string, unknown>;

    // Use the mahalla_id already present in the GeoJSON — this is the same
    // ID the GEE pipeline uses when writing mahalla_stats to the manifest,
    // so the keys will match on stats sync.
    const mahalla_id = Number(raw["mahalla_id"] ?? raw["id"] ?? index + 1);

    // district_id comes directly from the GeoJSON property; fall back to the
    // name-based lookup only if missing.
    const district_id =
      Number(raw["district_id"]) ||
      districtNameToId.get(p.parent_district) ||
      0;

    const baseline = baselineLstFromValue(raw["value"] ?? raw["composite_value"]);

    return {
      ...f,
      properties: {
        mahalla_id,
        mahalla_name: p.name,
        district_id,
        population: Number(raw["population_count"] ?? 0),
        employment: Number(raw["employment"] ?? 0),
        lst_baseline: baseline,
        lst_mean: baseline,
        lst_std: undefined,
        lst_min: Math.round((baseline - 5) * 10) / 10,
        lst_max: Math.round((baseline + 5) * 10) / 10,
        lst_count: undefined,
        low_confidence: false,
      },
    } as Mahalla;
  });

  return { mahallas, districts };
}

function geometryRings(geometry: Geometry): number[][][] {
  if (geometry.type === "Polygon") return geometry.coordinates as number[][][];
  if (geometry.type === "MultiPolygon")
    return (geometry.coordinates as number[][][][]).flat();
  return [];
}

function projectFeatures(features: (Mahalla | District)[]) {
  const points: number[][] = [];
  features.forEach((f) =>
    geometryRings(f.geometry).forEach((r) => r.forEach((p) => points.push(p))),
  );
  const xs = points.map((p) => p[0]),
    ys = points.map((p) => p[1]);
  const minX = Math.min(...xs),
    maxX = Math.max(...xs),
    minY = Math.min(...ys),
    maxY = Math.max(...ys);
  const width = maxX - minX || 1,
    height = maxY - minY || 1;
  return (geometry: Geometry) =>
    geometryRings(geometry)
      .map((r) =>
        r
          .map(
            ([x, y]) =>
              `${((x - minX) / width) * 100},${100 - ((y - minY) / height) * 100}`,
          )
          .join(" "),
      )
      .join(" ");
}

// ── Fallback Map (SVG) ───────────────────────────────────────────────────
function FallbackMap({
  features,
  districts,
  mode,
  selected,
  onSelect,
}: {
  features: Mahalla[];
  districts: District[];
  mode: Mode;
  selected: Mahalla | null;
  onSelect: (m: Mahalla) => void;
}) {
  const project = useMemo(
    () => projectFeatures([...features, ...districts]),
    [features, districts],
  );
  const districtProject = useMemo(
    () => projectFeatures(districts),
    [districts],
  );
  return (
    <div className="fallback-map">
      <svg
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        aria-label="Tashkent heat map"
      >
        {districts.map((d) => (
          <polygon
            key={d.properties.d_id}
            points={districtProject(d.geometry)}
            fill="none"
            stroke="#778184"
            strokeWidth=".18"
          />
        ))}
        {mode === "mahalla" &&
          features.map((m) => (
            <g
              key={m.properties.mahalla_id}
              onClick={() => onSelect(m)}
              className="map-feature"
            >
              <polygon
                points={project(m.geometry)}
                fill={colorFor(m.properties.lst_mean!)}
                fillOpacity={m.properties.low_confidence ? 0.45 : 0.82}
                stroke={
                  selected?.properties.mahalla_id === m.properties.mahalla_id
                    ? "#f5efe1"
                    : "#242d2d"
                }
                strokeWidth={
                  selected?.properties.mahalla_id === m.properties.mahalla_id
                    ? 0.4
                    : 0.08
                }
              />
              {m.properties.low_confidence && (
                <polygon points={project(m.geometry)} className="hatch" />
              )}
            </g>
          ))}
        {mode === "district" &&
          districts.map((d) => {
            const rows = features.filter(
              (m) => m.properties.district_id === d.properties.d_id,
            );
            const value = d.properties.lst_mean ?? (
              rows.length
                ? rows.reduce((a, m) => a + (m.properties.lst_mean || 0), 0) / rows.length
                : 55
            );
            return (
              <polygon
                key={d.properties.d_id}
                points={districtProject(d.geometry)}
                fill={colorFor(value)}
                fillOpacity=".88"
                stroke="#26302f"
                strokeWidth=".35"
                onClick={() => rows[0] && onSelect(rows[0])}
              />
            );
          })}
      </svg>
      <span className="map-credit">STATIC CARTOGRAPHY · WGS84</span>
    </div>
  );
}

// ── Mapbox Map Component ─────────────────────────────────────────────────
function MapboxMap({
  mahallas,
  districts,
  layerMode,
  rasterBoundary,
  onSelect,
  rasterEntry,
  lstOpacity,
  satellite,
  onRasterStatus,
}: {
  mahallas: Mahalla[];
  districts: District[];
  layerMode: LayerMode;
  rasterBoundary: RasterBoundaryMode;
  onSelect: (m: Mahalla) => void;
  selectedFeatureId: number | null;
  rasterEntry?: LstMapEntry;
  lstOpacity: number;
  satellite: boolean;
  onRasterStatus: (status: "ready" | "missing" | "error") => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<mapboxgl.Map | null>(null);
  const token = import.meta.env.VITE_MAPBOX_TOKEN;

  const lstOpacityRef = useRef(lstOpacity);
  lstOpacityRef.current = lstOpacity;

  // Stable refs so click handlers inside the map init effect never go stale
  const mahallasRef  = useRef(mahallas);
  const districtsRef = useRef(districts);
  const onSelectRef  = useRef(onSelect);
  mahallasRef.current  = mahallas;
  districtsRef.current = districts;
  onSelectRef.current  = onSelect;

  // Keep hotspot markers in a ref so we can clear/re-add without remounting the map
  const htmlMarkersRef = useRef<mapboxgl.Marker[]>([]);
  const hotspotPopupRef = useRef<mapboxgl.Popup | null>(null);

  // Helper: clear all beacon markers from the map
  const clearBeacons = () => {
    htmlMarkersRef.current.forEach((m) => m.remove());
    htmlMarkersRef.current = [];
    hotspotPopupRef.current?.remove();
    hotspotPopupRef.current = null;
  };

  const updateBeaconVisibility = (instance: mapboxgl.Map) => {
    const shouldShow = instance.getZoom() >= HOTSPOT_MIN_ZOOM;
    htmlMarkersRef.current.forEach((marker) => {
      marker.getElement().classList.toggle("visible", shouldShow);
    });
    if (!shouldShow) {
      hotspotPopupRef.current?.remove();
      hotspotPopupRef.current = null;
    }
  };

  // Helper: place beacon markers for the current scene's hotspots
  const drawBeacons = (
    instance: mapboxgl.Map,
    hotspots: Hotspot[],
    mahallaList: Mahalla[],
    selectFn: (m: Mahalla) => void,
  ) => {
    clearBeacons();
    if (!hotspots.length) return;

    hotspots.forEach((spot) => {
      const coordinates = normalizeHotspotCoordinates(spot.coordinates);
      if (!coordinates) return; // UTM or otherwise invalid — skip silently

      const isCritical = spot.severity === "Critical";
      const isSevere = spot.severity === "Severe";
      const ringBg      = isCritical ? "rgba(255,17,0,0.40)"  : "rgba(255,102,0,0.40)";
      const borderColor = isCritical ? "rgba(255,68,0,0.95)"  : isSevere ? "rgba(255,136,0,0.95)" : "rgba(242,178,103,0.95)";

      const el = document.createElement("div");
      el.className = "hotspot-beacon-container";
      
      // 1. Force the root container to exactly 0x0 and allow overflow
      el.style.width = "0px";
      el.style.height = "0px";
      el.style.position = "relative";
      el.style.overflow = "visible";
      
      const ring = document.createElement("div");
      ring.className = "beacon-pulse-ring";
      ring.style.background = ringBg;
      ring.style.borderColor = borderColor;
      // 2. Pin the ring exactly to the 0,0 center
      ring.style.position = "absolute";
      ring.style.top = "50%";
      ring.style.left = "50%";
      ring.style.transform = "translate(-50%, -50%)";

      const badge = document.createElement("div");
      badge.className = "beacon-badge";
      badge.style.borderColor = borderColor;
      // 3. Anchor the badge next to the center point
      badge.style.position = "absolute";
      badge.style.top = "50%";
      badge.style.left = "16px"; // Adjust visual gap from the dot
      badge.style.transform = "translate(0, -50%)";
      badge.style.whiteSpace = "nowrap";

      const dot = document.createElement("span");
      dot.className = "beacon-dot";
      // 4. Pin the dot exactly to the 0,0 center
      dot.style.position = "absolute";
      dot.style.top = "50%";
      dot.style.left = "50%";
      dot.style.transform = "translate(-50%, -50%)";

      const temp = document.createElement("span");
      temp.className = "beacon-temp";
      temp.textContent = `${spot.peak_lst.toFixed(1)}°C`;

      badge.append(temp);
      el.append(ring, dot, badge);

      const openDetails = () => {
        hotspotPopupRef.current?.remove();

        const detail = document.createElement("div");
        detail.className = "hotspot-popup";

        const heading = document.createElement("div");
        heading.className = "hotspot-popup-heading";
        const title = document.createElement("strong");
        title.textContent = spot.mahalla_name;
        const severity = document.createElement("span");
        severity.textContent = spot.severity;
        severity.dataset.severity = spot.severity.toLowerCase();
        heading.append(title, severity);

        const districtName = document.createElement("p");
        districtName.textContent = spot.district_name;

        const metrics = document.createElement("div");
        metrics.className = "hotspot-popup-metrics";
        [
          ["Peak", `${spot.peak_lst.toFixed(1)}°C`],
          ["Mean", `${spot.mean_lst.toFixed(1)}°C`],
          ["Area", `${spot.area_ha.toFixed(1)} ha`],
        ].forEach(([label, value]) => {
          const metric = document.createElement("span");
          const metricLabel = document.createElement("small");
          metricLabel.textContent = label;
          const metricValue = document.createElement("b");
          metricValue.textContent = value;
          metric.append(metricLabel, metricValue);
          metrics.append(metric);
        });

        const meta = document.createElement("small");
        meta.className = "hotspot-popup-meta";
        meta.textContent = `${spot.id} · ${coordinates[1].toFixed(5)}, ${coordinates[0].toFixed(5)}`;

        detail.append(heading, districtName, metrics, meta);

        hotspotPopupRef.current = new mapboxgl.Popup({
          closeButton: true,
          closeOnClick: true,
          className: "hotspot-map-popup",
          offset: 24,
        })
          .setLngLat(coordinates)
          .setDOMContent(detail)
          .addTo(instance);
      };

      el.addEventListener("click", (e) => {
        e.stopPropagation();
        openDetails();
        const parent = mahallaList.find(
          (item) => item.properties.mahalla_id === Number(spot.mahalla_id),
        );
        if (parent) selectFn(parent);
      });
      el.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        openDetails();
        const parent = mahallaList.find(
          (item) => item.properties.mahalla_id === Number(spot.mahalla_id),
        );
        if (parent) selectFn(parent);
      });

      const marker = new mapboxgl.Marker({ element: el, anchor: "center" })
        .setLngLat(coordinates)
        .addTo(instance);

      htmlMarkersRef.current.push(marker);
    });
    updateBeaconVisibility(instance);
  };

  // ── UPDATE MAPBOX DATA SOURCES WHEN MAHALLAS / DISTRICTS STATS CHANGE ──
  useEffect(() => {
    if (!map.current || !map.current.isStyleLoaded()) return;
    const m = map.current;

    const mahallaSource = m.getSource("mahallas") as mapboxgl.GeoJSONSource;
    if (mahallaSource) {
      mahallaSource.setData({
        type: "FeatureCollection",
        features: mahallas,
      });
    }

    const districtSource = m.getSource("districts") as mapboxgl.GeoJSONSource;
    if (districtSource) {
      districtSource.setData(buildDistrictFeatureCollection(mahallas, districts));
    }
  }, [mahallas, districts]);

  // ── BEACON MARKERS: re-draw whenever hotspots or mahallas change ─────────
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const hotspots = rasterEntry?.hotspots ?? [];

    const render = () => drawBeacons(
      instance,
      hotspots,
      mahallasRef.current,
      onSelectRef.current,
    );

    if (instance.isStyleLoaded()) {
      render();
    } else {
      instance.once("load", render);
    }

    return clearBeacons;
  // drawBeacons / clearBeacons are defined in component scope but stable
  // across renders because they only reference refs. rasterEntry?.date is
  // a scalar that changes when the user picks a new scene.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rasterEntry?.date, rasterEntry?.hotspots]);

  // ── DYNAMIC OPACITY & BOUNDARY OVERLAY UPDATE ──────────────────────────
  useEffect(() => {
    if (!map.current || !map.current.isStyleLoaded()) return;
    const m = map.current;

    const isMahalla = layerMode === "mahalla";
    const isDistrict = layerMode === "district";
    const isRaster = layerMode === "raster";

    if (m.getLayer("lst-raster") && m.getLayer("mahalla-fill")) {
      m.moveLayer("lst-raster", "mahalla-fill");
    }

    if (m.getLayer("lst-raster")) {
      m.setPaintProperty("lst-raster", "raster-opacity", lstOpacity);
      m.setLayoutProperty("lst-raster", "visibility", isRaster ? "visible" : "none");
    }

    if (m.getLayer("mahalla-fill")) {
      const showFill = isMahalla || (isRaster && rasterBoundary === "mahalla");
      m.setLayoutProperty("mahalla-fill", "visibility", showFill ? "visible" : "none");
      m.setPaintProperty(
        "mahalla-fill",
        "fill-opacity",
        isRaster ? 0.001 : ["case", ["boolean", ["feature-state", "selected"], false], 0.97, 0.82]
      );
    }
    if (m.getLayer("mahalla-outline")) {
      const showOutline = isMahalla || (isRaster && rasterBoundary === "mahalla");
      m.setLayoutProperty("mahalla-outline", "visibility", showOutline ? "visible" : "none");
      m.setPaintProperty("mahalla-outline", "line-color", isRaster ? "#ffffff" : "#1e2b2b");
      m.setPaintProperty("mahalla-outline", "line-width", isRaster ? 1.2 : 0.6);
      m.setPaintProperty("mahalla-outline", "line-opacity", isRaster ? 0.9 : 0.7);
    }

    if (m.getLayer("district-fill")) {
      const showFill = isDistrict || (isRaster && rasterBoundary === "district");
      m.setLayoutProperty("district-fill", "visibility", showFill ? "visible" : "none");
      m.setPaintProperty(
        "district-fill",
        "fill-opacity",
        isRaster ? 0.001 : ["case", ["boolean", ["feature-state", "selected"], false], 0.95, 0.82]
      );
    }
    if (m.getLayer("district-outline")) {
      const showOutline = isDistrict || (isRaster && rasterBoundary === "district");
      m.setLayoutProperty("district-outline", "visibility", showOutline ? "visible" : "none");
      m.setPaintProperty("district-outline", "line-color", isRaster ? "#ffffff" : "#2a3635");
      m.setPaintProperty("district-outline", "line-width", isRaster ? 2.2 : 1.5);
      m.setPaintProperty("district-outline", "line-opacity", isRaster ? 0.95 : 1.0);
    }
  }, [layerMode, rasterBoundary, lstOpacity]);

  // ── MAIN MAP INITIALIZATION ─────────────────────────────────────────────
  useEffect(() => {
    if (!container.current || !token) return;
    mapboxgl.accessToken = token;
    const instance = new mapboxgl.Map({
      container: container.current,
      style: satellite
        ? "mapbox://styles/mapbox/satellite-streets-v12"
        : "mapbox://styles/mapbox/dark-v11",
      center: [69.24, 41.3],
      zoom: 10.2,
    });
    map.current = instance;

    instance.on("load", async () => {
      // ── 1. MAHALLA SOURCE & LAYERS ────────────────────────────────────────
      instance.addSource("mahallas", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: mahallas,
        } as FeatureCollection,
        promoteId: "mahalla_id",
      });
      instance.addLayer({
        id: "mahalla-fill",
        type: "fill",
        source: "mahallas",
        layout: { visibility: (layerMode === "mahalla" || (layerMode === "raster" && rasterBoundary === "mahalla")) ? "visible" : "none" },
        paint: {
          "fill-color": LST_FILL_COLOR,
          "fill-opacity": layerMode === "raster" ? 0.001 : [
            "case",
            ["boolean", ["feature-state", "selected"], false], 0.97,
            0.82,
          ],
        },
      });
      instance.addLayer({
        id: "mahalla-outline",
        type: "line",
        source: "mahallas",
        layout: { visibility: (layerMode === "mahalla" || (layerMode === "raster" && rasterBoundary === "mahalla")) ? "visible" : "none" },
        paint: {
          "line-color": layerMode === "raster" ? "#ffffff" : "#1e2b2b",
          "line-width": layerMode === "raster" ? 1.2 : 0.6,
          "line-opacity": layerMode === "raster" ? 0.9 : 0.7,
        },
      });
      instance.addLayer({
        id: "mahalla-selected",
        type: "line",
        source: "mahallas",
        layout: { visibility: "visible" },
        paint: {
          "line-color": "#fff8e8",
          "line-width": [
            "case",
            ["boolean", ["feature-state", "selected"], false], 2.5,
            0,
          ],
          "line-offset": -1,
        },
      });

      // ── 2. DISTRICT SOURCE & LAYERS ───────────────────────────────────────
      const districtFeatures = buildDistrictFeatureCollection(mahallas, districts);
      instance.addSource("districts", {
        type: "geojson",
        data: districtFeatures,
        promoteId: "d_id",
      });
      instance.addLayer({
        id: "district-fill",
        type: "fill",
        source: "districts",
        layout: { visibility: (layerMode === "district" || (layerMode === "raster" && rasterBoundary === "district")) ? "visible" : "none" },
        paint: {
          "fill-color": LST_FILL_COLOR,
          "fill-opacity": layerMode === "raster" ? 0.001 : [
            "case",
            ["boolean", ["feature-state", "selected"], false], 0.95,
            0.82,
          ],
        },
      });
      instance.addLayer({
        id: "district-outline",
        type: "line",
        source: "districts",
        layout: { visibility: (layerMode === "district" || (layerMode === "raster" && rasterBoundary === "district")) ? "visible" : "none" },
        paint: {
          "line-color": layerMode === "raster" ? "#ffffff" : "#2a3635",
          "line-width": layerMode === "raster" ? 2.2 : 1.5,
          "line-opacity": layerMode === "raster" ? 0.95 : 1.0,
        },
      });
      instance.addLayer({
        id: "district-selected",
        type: "line",
        source: "districts",
        layout: { visibility: "visible" },
        paint: {
          "line-color": "#fff8e8",
          "line-width": [
            "case",
            ["boolean", ["feature-state", "selected"], false], 3.5,
            0,
          ],
          "line-offset": -1.5,
        },
      });

      // ── 3. CLICK HANDLERS (use refs to avoid stale closures) ─────────────
      let lastSelectedMahalla: number | null = null;
      instance.on("click", "mahalla-fill", (e) => {
        const id = e.features?.[0]?.properties?.mahalla_id;
        const item = mahallasRef.current.find((f) => f.properties.mahalla_id === Number(id));
        if (!item) return;
        if (lastSelectedMahalla !== null) {
          instance.setFeatureState({ source: "mahallas", id: lastSelectedMahalla }, { selected: false });
        }
        lastSelectedMahalla = Number(id);
        instance.setFeatureState({ source: "mahallas", id: lastSelectedMahalla }, { selected: true });
        onSelectRef.current(item);
      });

      let lastSelectedDistrict: number | null = null;
      instance.on("click", "district-fill", (e) => {
        const did = e.features?.[0]?.properties?.d_id;
        const rows = mahallasRef.current.filter((m) => m.properties.district_id === Number(did));
        if (!rows.length) return;
        if (lastSelectedDistrict !== null) {
          instance.setFeatureState({ source: "districts", id: lastSelectedDistrict }, { selected: false });
        }
        lastSelectedDistrict = Number(did);
        instance.setFeatureState({ source: "districts", id: lastSelectedDistrict }, { selected: true });
        onSelectRef.current(rows[0]);
      });

      instance.on("mouseenter", "mahalla-fill", () => { instance.getCanvas().style.cursor = "pointer"; });
      instance.on("mouseleave", "mahalla-fill", () => { instance.getCanvas().style.cursor = ""; });
      instance.on("mouseenter", "district-fill", () => { instance.getCanvas().style.cursor = "pointer"; });
      instance.on("mouseleave", "district-fill", () => { instance.getCanvas().style.cursor = ""; });
      instance.on("zoom", () => updateBeaconVisibility(instance));
      instance.on("moveend", () => updateBeaconVisibility(instance));

      instance.addControl(new mapboxgl.NavigationControl(), "bottom-right");
    });

    return () => instance.remove();
  }, [
    token,
    satellite,
  ]);

  // ── RASTER LAYER: update when selected date / rasterEntry changes ─────────
  // Separate from map init so the map doesn't remount on every date selection.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !instance.isStyleLoaded()) return;

    // Remove the old raster source/layer if present
    if (instance.getLayer("lst-raster")) instance.removeLayer("lst-raster");
    if (instance.getSource("lst-raster")) instance.removeSource("lst-raster");

    const tileUrl    = rasterEntry?.tile_url ?? LST_RASTER_TILE_URL;
    const rawImageUrl = rasterEntry?.url;
    const cogUrl     = rasterEntry?.cog_url ?? LST_GEOTIFF_URL;
    const bounds     = normalizeBoundsToWgs84(rasterEntry?.bounds ?? LST_RASTER_BOUNDS);
    const beforeLayer = instance.getLayer("mahalla-fill") ? "mahalla-fill" : undefined;
    const visible = layerMode === "raster" ? "visible" : "none";

    const addLayer = (sourceId: string) => {
      instance.addLayer(
        {
          id: "lst-raster",
          type: "raster",
          source: sourceId,
          layout: { visibility: visible },
          paint: { "raster-opacity": lstOpacityRef.current, "raster-resampling": "linear" },
        },
        beforeLayer,
      );
    };

    if (tileUrl) {
      instance.addSource("lst-raster", {
        type: "raster",
        tiles: [tileUrl],
        tileSize: 256,
        bounds: [69.126, 41.166, 69.466, 41.396],
      });
      addLayer("lst-raster");
      instance.once("idle", () => { if (layerMode === "raster") onRasterStatus("ready"); });

    } else if (rawImageUrl && isImageRasterUrl(rawImageUrl)) {
      const probe = new Image();
      probe.crossOrigin = "anonymous";
      probe.onload = () => {
        if (!instance.getSource("lst-raster")) {
          instance.addSource("lst-raster", { type: "image", url: rawImageUrl, coordinates: bounds });
          addLayer("lst-raster");
        }
        if (layerMode === "raster") onRasterStatus("ready");
      };
      probe.onerror = () => { if (layerMode === "raster") onRasterStatus("error"); };
      probe.src = rawImageUrl;

    } else if (cogUrl) {
      renderGeoTiff(cogUrl).then((rendered) => {
        if (!instance.getSource("lst-raster")) {
          instance.addSource("lst-raster", {
            type: "image",
            url: rendered.url,
            coordinates: rendered.coordinates,
          });
          addLayer("lst-raster");
        }
        if (layerMode === "raster") onRasterStatus("ready");
      }).catch(() => { if (layerMode === "raster") onRasterStatus("error"); });

    } else if (LST_RASTER_URL) {
      instance.addSource("lst-raster", { type: "image", url: LST_RASTER_URL, coordinates: LST_RASTER_BOUNDS });
      addLayer("lst-raster");
      const img = new Image();
      img.onload = () => { if (layerMode === "raster") onRasterStatus("ready"); };
      img.onerror = () => { if (layerMode === "raster") onRasterStatus("error"); };
      img.src = LST_RASTER_URL;

    } else {
      if (layerMode === "raster") onRasterStatus("missing");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rasterEntry, layerMode, onRasterStatus]);

  return <div ref={container} className="mapbox-map" />;
}

// ── Calendar Component ────────────────────────────────────────────────────
function LstCalendar({
  maps,
  selectedDate,
  onSelect,
}: {
  maps: LstMapEntry[];
  selectedDate: string;
  onSelect: (date: string) => void;
}) {
  const selectedMonth = selectedDate
    ? new Date(`${selectedDate}T12:00:00Z`)
    : new Date();
  const [month, setMonth] = useState(
    new Date(
      Date.UTC(selectedMonth.getUTCFullYear(), selectedMonth.getUTCMonth(), 1),
    ),
  );
  useEffect(() => {
    const nextMonth = new Date(
      Date.UTC(selectedMonth.getUTCFullYear(), selectedMonth.getUTCMonth(), 1),
    );
    setMonth(nextMonth);
  }, [selectedDate]);
  const available = new Set(maps.map((map) => map.date));
  const firstWeekday = new Date(
    Date.UTC(month.getUTCFullYear(), month.getUTCMonth(), 1),
  ).getUTCDay();
  const daysInMonth = new Date(
    Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 0),
  ).getUTCDate();
  const cells = Array.from(
    { length: firstWeekday + daysInMonth },
    (_, index) => {
      if (index < firstWeekday) return null;
      const day = index - firstWeekday + 1;
      return `${month.getUTCFullYear()}-${String(month.getUTCMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    },
  );
  return (
    <div className="lst-calendar">
      <div className="calendar-header">
        <strong>
          {new Intl.DateTimeFormat("en", {
            month: "long",
            year: "numeric",
          }).format(month)}
        </strong>
        <div>
          <button
            aria-label="Previous month"
            onClick={() =>
              setMonth(
                new Date(
                  Date.UTC(month.getUTCFullYear(), month.getUTCMonth() - 1, 1),
                ),
              )
            }
          >
            ‹
          </button>
          <button
            aria-label="Next month"
            onClick={() =>
              setMonth(
                new Date(
                  Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 1),
                ),
              )
            }
          >
            ›
          </button>
        </div>
      </div>
      <div className="calendar-weekdays">
        {["S", "M", "T", "W", "T", "F", "S"].map((day, index) => (
          <span key={`${day}-${index}`}>{day}</span>
        ))}
      </div>
      <div className="calendar-grid">
        {cells.map((date, index) =>
          date ? (
            <button
              key={date}
              className={`${available.has(date) ? "available" : ""} ${date === selectedDate ? "selected" : ""}`}
              disabled={!available.has(date)}
              onClick={() => onSelect(date)}
            >
              {Number(date.slice(-2))}
            </button>
          ) : (
            <span key={`empty-${index}`} />
          ),
        )}
      </div>
      <div className="calendar-key">
        <span className="calendar-dot" /> {maps.length} available scene
        {maps.length === 1 ? "" : "s"}
      </div>
    </div>
  );
}

// ── Main App Component ───────────────────────────────────────────────────
export default function App() {
  const [mahallas, setMahallas] = useState<Mahalla[]>([]);
  const [districts, setDistricts] = useState<District[]>([]);
  const [mode] = useState<Mode>("mahalla");
  const [layerMode, setLayerMode] = useState<LayerMode>("mahalla");
  const [rasterBoundary, setRasterBoundary] = useState<RasterBoundaryMode>("mahalla");
  const [district, setDistrict] = useState("all");
  const [sort, setSort] = useState<"lst_mean" | "lst_min" | "lst_max">("lst_mean");
  const [selected, setSelected] = useState<Mahalla | null>(null);
  const [showMethod, setShowMethod] = useState(false);
  const [query, setQuery] = useState("");
  const [lstOpacity, setLstOpacity] = useState(0.9);
  const [satellite, setSatellite] = useState(true);
  const [lstMaps, setLstMaps] = useState<LstMapEntry[]>([]);
  const [selectedDate, setSelectedDate] = useState("");
  const [rasterStatus, setRasterStatus] = useState<"ready" | "missing" | "error">("missing");

  // Load Manifest
  // Tries the R2 URL first (now that CORS is configured on the bucket),
  // falls back to the local bundled copy which the backend keeps in sync.
  useEffect(() => {
    const urls = [
      "https://pub-ec33c8a1d13f40f4935fa09d39bb00ce.r2.dev/landsat-lst-scenes/maps_manifest.json",
      "/data/lst-manifest.json",
    ];

    async function loadManifest() {
      for (const url of urls) {
        try {
          const response = await fetch(url);
          if (!response.ok) continue;

          const contentType = response.headers.get("content-type") ?? "";
          if (contentType.includes("text/html")) continue;

          const payload = (await response.json()) as
            | LstMapEntry[]
            | { maps?: LstMapEntry[]; scenes?: LstMapEntry[] };

          const rawEntries = Array.isArray(payload)
            ? payload
            : (payload.scenes ?? payload.maps ?? []);

          const validEntries = rawEntries
            .filter(
              (entry) =>
                /^\d{4}-\d{2}-\d{2}$/.test(entry.date) &&
                Boolean(entry.tile_url ?? entry.cog_url ?? entry.url),
            )
            .sort((a, b) => a.date.localeCompare(b.date));

          if (validEntries.length > 0) {
            setLstMaps(validEntries);
            setSelectedDate(validEntries.at(-1)?.date || "");
            return;
          }
        } catch {
          continue;
        }
      }

      // Final fallback: single GeoTIFF from env var
      if (LST_GEOTIFF_URL) {
        const date =
          LST_GEOTIFF_URL.match(/\d{4}-\d{2}-\d{2}/)?.[0] || "2024-08-15";
        setLstMaps([{ date, cog_url: LST_GEOTIFF_URL }]);
        setSelectedDate(date);
      }
    }

    loadManifest();
  }, []);

  // Load Base GeoJSON Boundaries
  useEffect(() => {
    fetch("/data/tashkent_city_renderable.geojson")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((geojson) => {
        const { mahallas, districts } = parseRenderable(geojson);
        setMahallas(mahallas);
        setDistricts(districts);
      })
      .catch(() => {
        const fallbackDistrictNames = ["Демо-север", "Демо-центр", "Демо-юг"];
        fallbackDistrictNames.forEach((name, i) => {
          DISTRICT_NAMES[i + 1] = name;
        });
        const fallbackDistricts: District[] = fallbackDistrictNames.map(
          (_, i) => ({
            type: "Feature",
            properties: {
              d_id: i + 1,
              lst_mean: Math.round((48 + i * 3) * 10) / 10,
              lst_min: Math.round((40 + i * 2) * 10) / 10,
              lst_max: Math.round((58 + i * 4) * 10) / 10,
            },
            geometry: {
              type: "Polygon",
              coordinates: [
                [
                  [69.15 + i * 0.08, 41.2],
                  [69.22 + i * 0.08, 41.2],
                  [69.22 + i * 0.08, 41.3],
                  [69.15 + i * 0.08, 41.3],
                  [69.15 + i * 0.08, 41.2],
                ],
              ],
            },
          }),
        );
        const fallbackMahallas: Mahalla[] = fallbackDistricts.flatMap(
          (d, di) =>
            Array.from({ length: 12 }, (_, mi) => {
              const mean = Math.round((46 + di * 4 + mi * 0.5) * 10) / 10;
              return {
                type: "Feature",
                properties: {
                  mahalla_id: d.properties.d_id * 1000 + mi,
                  mahalla_name: `Demo mahalla ${mi + 1}`,
                  district_id: d.properties.d_id,
                  lst_baseline: mean,
                  lst_mean: mean,
                  lst_std: 1.8,
                  lst_min: Math.round((mean - 5) * 10) / 10,
                  lst_max: Math.round((mean + 5) * 10) / 10,
                  lst_count: 200,
                  low_confidence: mi % 13 === 0,
                },
                geometry: {
                  type: "Polygon",
                  coordinates: [
                    [
                      [69.155 + di * 0.08 + (mi % 4) * 0.016, 41.205 + Math.floor(mi / 4) * 0.03],
                      [69.168 + di * 0.08 + (mi % 4) * 0.016, 41.205 + Math.floor(mi / 4) * 0.03],
                      [69.168 + di * 0.08 + (mi % 4) * 0.016, 41.23 + Math.floor(mi / 4) * 0.03],
                      [69.155 + di * 0.08 + (mi % 4) * 0.016, 41.23 + Math.floor(mi / 4) * 0.03],
                      [69.155 + di * 0.08 + (mi % 4) * 0.016, 41.205 + Math.floor(mi / 4) * 0.03],
                    ],
                  ],
                },
              };
            }),
        );
        setMahallas(fallbackMahallas);
        setDistricts(fallbackDistricts);
      });
  }, []);

  const selectedLstMap = useMemo(
    () => lstMaps.find((map) => map.date === selectedDate),
    [lstMaps, selectedDate]
  );

  // ── DYNAMIC STATS SYNC: MERGE MANIFEST STATS INTO MAHALLAS & DISTRICTS ──
  useEffect(() => {
    // Reset to baseline values before applying selected-date stats.
    setMahallas((prev) =>
      prev.map((m) => {
        const baseline = finiteOrFallback(m.properties.lst_baseline, DEFAULT_LST_MEAN);
        return {
          ...m,
          properties: {
            ...m.properties,
            lst_mean: baseline,
            lst_std: undefined,
            lst_min: Math.round((baseline - 5) * 10) / 10,
            lst_max: Math.round((baseline + 5) * 10) / 10,
            lst_count: undefined,
            low_confidence: false,
          },
        };
      })
    );
    setDistricts((prev) =>
      prev.map((d) => ({
        ...d,
        properties: { ...d.properties, lst_mean: undefined, lst_min: undefined, lst_max: undefined },
      }))
    );

    if (!selectedLstMap) return;

    const mStats = selectedLstMap.mahalla_stats;
    const dStats = selectedLstMap.district_stats;

    if (mStats) {
      setMahallas((prev) =>
        prev.map((m) => {
          const mId = m.properties.mahalla_id;
          // GEE writes keys as string(mahalla_id) — try both numeric and string
          const stat = mStats[mId] ?? mStats[String(mId)];
          if (!stat) return m;
          const count = (stat as { count?: number }).count;
          return {
            ...m,
            properties: {
              ...m.properties,
              lst_mean: stat.mean,
              lst_min: stat.min,
              lst_max: stat.max,
              lst_count: count ?? undefined,
              low_confidence: count != null && count < 50,
            },
          };
        })
      );
    }

    if (dStats) {
      setDistricts((prev) =>
        prev.map((d) => {
          const dId = d.properties.d_id;
          const dName = d.properties.full_name ?? DISTRICT_NAMES[dId];
          // Try every key format the GEE script might write:
          // numeric d_id (2611), string d_id ("2611"), full_name ("Toshkent, Bektemir")
          const stat =
            dStats[dId] ??
            dStats[String(dId)] ??
            (dName ? dStats[dName] : undefined);
          if (!stat) return d;
          return {
            ...d,
            properties: {
              ...d.properties,
              lst_mean: stat.mean,
              lst_min: stat.min,
              lst_max: stat.max,
            },
          };
        })
      );
    }
  }, [selectedLstMap]);

  const visible = useMemo(
    () =>
      mahallas.filter(
        (m) =>
          (district === "all" ||
            m.properties.district_id === Number(district)) &&
          (query === "" ||
            (m.properties.mahalla_name || "")
              .toLowerCase()
              .includes(query.toLowerCase())),
      ),
    [mahallas, district, query],
  );

  const ranked = useMemo(
    () =>
      [...visible]
        .sort(
          (a, b) =>
            Number(b.properties[sort] ?? 0) - Number(a.properties[sort] ?? 0),
        )
        .slice(0, 8),
    [visible, sort],
  );

  const districtRows = useMemo(
    () =>
      districts
        .map((d) => {
          const rows = mahallas.filter(
            (m) => m.properties.district_id === d.properties.d_id,
          );
          const mean = d.properties.lst_mean ?? (
            rows.length
              ? rows.reduce((a, m) => a + Number(m.properties.lst_mean || 0), 0) / rows.length
              : 0
          );
          return {
            name: DISTRICT_NAMES[d.properties.d_id] ?? d.properties.full_name ?? `District ${d.properties.d_id}`,
            mean,
          };
        })
        .sort((a, b) => b.mean - a.mean)
        .slice(0, 5),
    [districts, mahallas],
  );

  const selectedDistrictObj = useMemo(
    () => districts.find((d) => d.properties.d_id === Number(district)),
    [districts, district]
  );

  const statsSummary = useMemo(() => {
    if (district !== "all" && selectedDistrictObj) {
      const dProps = selectedDistrictObj.properties;
      const rows = mahallas.filter((m) => m.properties.district_id === selectedDistrictObj.properties.d_id);

      const mean = dProps.lst_mean ?? (
        rows.length ? rows.reduce((a, m) => a + Number(m.properties.lst_mean || 0), 0) / rows.length : 0
      );
      const min = dProps.lst_min ?? (
        rows.length ? Math.min(...rows.map((m) => m.properties.lst_min ?? mean)) : 0
      );
      const max = dProps.lst_max ?? (
        rows.length ? Math.max(...rows.map((m) => m.properties.lst_max ?? mean)) : 0
      );

      return { mean, min, max };
    }

    if (!visible.length) return { mean: 0, min: 0, max: 0 };

    const mean = visible.reduce((a, m) => a + Number(m.properties.lst_mean || 0), 0) / visible.length;
    const min = Math.min(...visible.map((m) => Number(m.properties.lst_min ?? m.properties.lst_mean ?? 0)));
    const max = Math.max(...visible.map((m) => Number(m.properties.lst_max ?? m.properties.lst_mean ?? 0)));

    return { mean, min, max };
  }, [district, selectedDistrictObj, visible, mahallas]);

  const mapDistricts =
    district === "all"
      ? districts
      : districts.filter((d) => d.properties.d_id === Number(district));

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">◒</span>
          <div>
            <span className="eyebrow">CLIMATE INTELLIGENCE / 01</span>
            <h1>
              Heat Atlas <em>/ Tashkent</em>
            </h1>
          </div>
        </div>
        <div className="header-actions">
          <span className="live-dot" />
          <span className="header-status">LAND SURFACE TEMPERATURE · 30M</span>
          <button className="method-link" onClick={() => setShowMethod(true)}>
            Methodology <span>↗</span>
          </button>
        </div>
      </header>
      <main className="workspace">
        <section className="map-column">
          <div className="map-header">
            <div>
              <p className="section-kicker">CITYWIDE EXPOSURE</p>
              <h2>Thermal surface reading</h2>
            </div>
            <div className="controls">
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
              <select
                value={district}
                onChange={(e) => setDistrict(e.target.value)}
              >
                <option value="all">All districts</option>
                {districts.map((d) => (
                  <option key={d.properties.d_id} value={d.properties.d_id}>
                    {DISTRICT_NAMES[d.properties.d_id] ?? `District ${d.properties.d_id}`}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="map-frame">
            {mahallas.length ? (
              import.meta.env.VITE_MAPBOX_TOKEN ? (
                <MapboxMap
                  mahallas={mahallas}
                  districts={districts}
                  layerMode={layerMode}
                  rasterBoundary={rasterBoundary}
                  onSelect={setSelected}
                  selectedFeatureId={selected?.properties.mahalla_id ?? null}
                  rasterEntry={selectedLstMap}
                  lstOpacity={lstOpacity}
                  satellite={satellite}
                  onRasterStatus={setRasterStatus}
                />
              ) : (
                <FallbackMap
                  features={visible}
                  districts={mapDistricts}
                  mode={mode}
                  selected={selected}
                  onSelect={setSelected}
                />
              )
            ) : (
              <div className="loading">
                Loading city surface <span>...</span>
              </div>
            )}
            <div className="map-legend">
              {layerMode === "raster" ? (
                <>
                  <div className="legend-boundary-selector" style={{ marginBottom: 12, width: "100%" }}>
                    <span
                      style={{
                        display: "block",
                        fontSize: "9px",
                        fontWeight: 700,
                        color: "#8d9999",
                        marginBottom: "6px",
                        letterSpacing: "0.06em",
                        textTransform: "uppercase",
                      }}
                    >
                      OVERLAY BOUNDARY
                    </span>
                    <div
                      style={{
                        display: "flex",
                        width: "100%",
                        background: "rgba(0, 0, 0, 0.3)",
                        borderRadius: "4px",
                        padding: "2px",
                        boxSizing: "border-box",
                        border: "1px solid rgba(255, 255, 255, 0.1)",
                      }}
                    >
                      <button
                        style={{
                          flex: 1,
                          padding: "4px 0",
                          fontSize: "10px",
                          fontWeight: 600,
                          textAlign: "center",
                          border: "none",
                          borderRadius: "3px",
                          cursor: "pointer",
                          transition: "all 0.15s ease",
                          background: rasterBoundary === "none" ? "#344240" : "transparent",
                          color: rasterBoundary === "none" ? "#ffffff" : "#8d9999",
                        }}
                        onClick={() => setRasterBoundary("none")}
                      >
                        None
                      </button>
                      <button
                        style={{
                          flex: 1,
                          padding: "4px 0",
                          fontSize: "10px",
                          fontWeight: 600,
                          textAlign: "center",
                          border: "none",
                          borderRadius: "3px",
                          cursor: "pointer",
                          transition: "all 0.15s ease",
                          background: rasterBoundary === "mahalla" ? "#344240" : "transparent",
                          color: rasterBoundary === "mahalla" ? "#ffffff" : "#8d9999",
                        }}
                        onClick={() => setRasterBoundary("mahalla")}
                      >
                        Mahalla
                      </button>
                      <button
                        style={{
                          flex: 1,
                          padding: "4px 0",
                          fontSize: "10px",
                          fontWeight: 600,
                          textAlign: "center",
                          border: "none",
                          borderRadius: "3px",
                          cursor: "pointer",
                          transition: "all 0.15s ease",
                          background: rasterBoundary === "district" ? "#344240" : "transparent",
                          color: rasterBoundary === "district" ? "#ffffff" : "#8d9999",
                        }}
                        onClick={() => setRasterBoundary("district")}
                      >
                        District
                      </button>
                    </div>
                  </div>

                  <div className="legend-title">
                    <span>°C</span> LST THERMAL
                  </div>
                  <div className="legend-gradient inferno" />
                  <div className="legend-labels">
                    <span>20°</span>
                    <span>35°</span>
                    <span>50°</span>
                    <span>65°</span>
                  </div>
                  <div className="legend-swatches">
                    <span className="swatch" style={{ background: "#000004" }} />
                    <span className="swatch-label">Cool</span>
                    <span className="swatch" style={{ background: "#8c2981" }} />
                    <span className="swatch-label">Moderate</span>
                    <span className="swatch" style={{ background: "#fe9f6d" }} />
                    <span className="swatch-label">Hot</span>
                    <span className="swatch" style={{ background: "#fcfdbf" }} />
                    <span className="swatch-label">Extreme</span>
                  </div>
                  <div
                    className="legend-note"
                    style={{ borderTop: 0, paddingTop: 0, marginTop: 8 }}
                  >
                    Landsat 8/9 surface temperature
                  </div>
                </>
              ) : (
                <>
                  <div className="legend-title">
                    <span>°C</span> MEAN LST
                  </div>
                  <div className="legend-gradient" />
                  <div className="legend-labels">
                    <span>42°</span>
                    <span>55°</span>
                    <span>70°</span>
                    <span>85°</span>
                  </div>
                  <div className="legend-note">
                    <span className="hatch-swatch" /> Low confidence{" "}
                    <span className="info">i</span>
                  </div>
                </>
              )}
            </div>
            <div className="map-annotation">
              <strong>
                {layerMode === "mahalla"
                  ? "MAHALLA VIEW"
                  : layerMode === "district"
                    ? "DISTRICT VIEW"
                    : "LST RASTER"}
              </strong>
              <span>
                {selectedDate
                  ? formatMapDate(selectedDate)
                  : "Choose an available date"}
              </span>
            </div>
            <div className="map-controls" aria-label="Map display controls">
              <button
                className={satellite ? "active" : ""}
                onClick={() => setSatellite((value) => !value)}
              >
                {satellite ? "Satellite" : "Basemap"}
              </button>
              <label>
                <span>LST opacity</span>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={lstOpacity}
                  onChange={(event) =>
                    setLstOpacity(Number(event.target.value))
                  }
                />
                <strong>{Math.round(lstOpacity * 100)}%</strong>
              </label>
            </div>
            {rasterStatus !== "ready" && (
              <div className="raster-alert">
                <strong>LST raster not loaded</strong>
                <span>
                  {rasterStatus === "error"
                    ? "Image failed to load — check that the R2 bucket has public access and CORS enabled."
                    : lstMaps.length
                      ? "Select a date from the calendar to display the LST overlay."
                      : "No scenes available. Run the upload script to populate the R2 manifest."}
                </span>
              </div>
            )}
          </div>
          <LstCalendar
            maps={lstMaps}
            selectedDate={selectedDate}
            onSelect={(date) => {
              setSelectedDate(date);
              setRasterStatus("missing");
            }}
          />
          <div className="map-foot">
            <span>Source: downscaled Landsat thermal product</span>
            <span>Click a polygon to inspect</span>
          </div>
        </section>
        <aside className="sidebar">
          <div className="sidebar-head">
            <div>
              <p className="section-kicker">PRIORITY SIGNALS</p>
              <h2>Heat watchlist</h2>
            </div>
            <span className="count">{visible.length} / 585</span>
          </div>
          <div className="metric-strip">
            <div>
              <span>MEAN LST</span>
              <strong>
                {statsSummary.mean.toFixed(1)}
                <small>°C</small>
              </strong>
            </div>
            <div>
              <span>MIN LST</span>
              <strong>
                {statsSummary.min.toFixed(1)}
                <small>°C</small>
              </strong>
            </div>
            <div>
              <span>MAX LST</span>
              <strong>
                {statsSummary.max.toFixed(1)}
                <small>°C</small>
              </strong>
            </div>
          </div>
          <div className="search-box">
            <span>⌕</span>
            <input
              placeholder="Search mahalla..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="table-toolbar">
            <span>TOP 8 BY</span>
            <div className="sort-buttons">
              <button
                className={sort === "lst_mean" ? "active" : ""}
                onClick={() => setSort("lst_mean")}
              >
                Mean
              </button>
              <button
                className={sort === "lst_min" ? "active" : ""}
                onClick={() => setSort("lst_min")}
              >
                Min
              </button>
              <button
                className={sort === "lst_max" ? "active" : ""}
                onClick={() => setSort("lst_max")}
              >
                Max
              </button>
            </div>
          </div>
          <div className="ranking">
            {ranked.map((m, i) => (
              <button
                className={`rank-row ${selected?.properties.mahalla_id === m.properties.mahalla_id ? "selected" : ""}`}
                key={m.properties.mahalla_id}
                onClick={() => setSelected(m)}
              >
                <span className="rank-num">0{i + 1}</span>
                <span className="rank-name">
                  <strong>{m.properties.mahalla_name}</strong>
                  <small>
                    {DISTRICT_NAMES[m.properties.district_id] ??
                      `District ${m.properties.district_id}`}
                  </small>
                </span>
                <span className="rank-value">
                  <b>{m.properties[sort]?.toFixed(1)}°</b>
                  <small>Min: {m.properties.lst_min?.toFixed(1)}° | Max: {m.properties.lst_max?.toFixed(1)}°</small>
                </span>
              </button>
            ))}
          </div>

          <div className="district-chart" style={{ marginTop: 20 }}>
            <div className="chart-title">
              <span>District temperature</span>
              <small>Spatial pixel stats</small>
            </div>
            <ResponsiveContainer width="100%" height={130}>
              <BarChart
                data={districtRows}
                layout="vertical"
                margin={{ left: 0, right: 18, top: 2, bottom: 0 }}
              >
                <XAxis type="number" domain={[40, 85]} hide />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={92}
                  tick={{ fill: "#8d9999", fontSize: 9 }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  cursor={{ fill: "#26302f" }}
                  contentStyle={{
                    background: "#1c2424",
                    border: "1px solid #40504d",
                    fontSize: 11,
                  }}
                  formatter={(v) => [`${Number(v).toFixed(1)}°C`, "LST"]}
                />
                <Bar
                  dataKey="mean"
                  fill="#e76536"
                  barSize={8}
                  radius={[0, 2, 2, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="sidebar-foot">
            Metrics update as a district filter is applied.
          </p>
        </aside>
      </main>
      {selected && (
        <div className="detail-panel">
          <button className="close-detail" onClick={() => setSelected(null)}>
            ×
          </button>
          <p className="section-kicker">SELECTED MAHALLA</p>
          <h2>{selected.properties.mahalla_name}</h2>
          <span className="detail-district">
            {DISTRICT_NAMES[selected.properties.district_id] ??
              selected.properties.district_id}
          </span>
          {selected.properties.low_confidence && (
            <span className="confidence-badge">LOW CONFIDENCE</span>
          )}
          <div className="detail-temp">
            <strong>{selected.properties.lst_mean?.toFixed(1)}°</strong>
            <span>mean land surface temperature</span>
          </div>
          <div className="detail-grid">
            <div>
              <span>MEAN</span>
              <b>{selected.properties.lst_mean?.toFixed(1)}°</b>
            </div>
            <div>
              <span>MINIMUM</span>
              <b>{selected.properties.lst_min?.toFixed(1)}°</b>
            </div>
            <div>
              <span>MAXIMUM</span>
              <b>{selected.properties.lst_max?.toFixed(1)}°</b>
            </div>
            <div>
              <span>PIXELS</span>
              <b>{fmt.format(selected.properties.lst_count || 0)}</b>
            </div>
          </div>
        </div>
      )}
      {showMethod && (
        <div className="modal-backdrop" onClick={() => setShowMethod(false)}>
          <article
            className="method-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="close-detail"
              onClick={() => setShowMethod(false)}
            >
              ×
            </button>
            <p className="section-kicker">FIELD NOTES / 01</p>
            <h2>Reading the urban surface</h2>
            <p className="modal-lede">
              Heat Atlas turns satellite observations into a neighborhood-scale
              signal for the people shaping Tashkent.
            </p>
            <div className="method-grid">
              <div>
                <b>01 / Thermal sources</b>
                <p>
                  Land Surface Temperature is derived from Landsat thermal
                  imagery, with MODIS used to support temporal consistency
                  across the summer composite.
                </p>
              </div>
              <div>
                <b>02 / Spatial detail</b>
                <p>
                  The source signal is downscaled to a 30m grid. Each polygon
                  reports the distribution of valid interior pixels that fall
                  inside its boundary.
                </p>
              </div>
              <div>
                <b>03 / Confidence</b>
                <p>
                  Where fewer than 50 interior pixels are available, we mark the
                  reading as low confidence. It remains visible, but should be
                  used as a directional signal.
                </p>
              </div>
              <div>
                <b>04 / District rollup</b>
                <p>
                  District readings are pre-computed directly from Landsat 30m
                  raster imagery in Google Earth Engine (or evaluated as pure spatial
                  averages across mahallas). No population weighting is applied.
                </p>
              </div>
            </div>
            <div className="method-source">
              Data literacy note <span>·</span> LST is a surface measurement,
              not air temperature.
            </div>
          </article>
        </div>
      )}
    </div>
  );
}
