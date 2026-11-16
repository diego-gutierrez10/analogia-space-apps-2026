#!/usr/bin/env python3
"""
mars_analog_data_pipeline.py
============================
Builds the dataset of Earth candidate sites for analogs of Mars sites
(NASA Space Apps Challenge 2026 · "Identify Earth Locations that Analog the
Permanent Moon Base Locations and Mars").

Reproduces, end to end, how all the data were obtained and cleaned:

  1. relief     0.5° patch grid per continent over the Copernicus DEM GLO-30.
                The DEM is averaged to 463 m (the MOLA scale on Mars) before slope and
                roughness are computed, with the same function that is applied to Mars.
  2. cleaning   Coarse filter per patch with ESA WorldCover 2021 (land cover) and
                GHS-POP R2023A (population): arid areas with no cities, cropland,
                forest or water.
  3. fine       ~5 x 5 km subpatches (near equal area) inside the surviving patches:
                relief, land cover and population per subpatch, plus fine-scale rules
                (including detection of lakes flattened by the DSM).
  4. integrate  Merges all continents into one world table without duplicates.
  5. albedo     Temporal summary (2019–2021) of MODIS MCD43C3 v061 albedo:
                median, percentiles, variability and snow fraction.
  6. lst        Temporal summary (2016–2020) of MODIS MYD11C3 v061 land surface
                temperature (day, night and amplitude), in °C and as percentiles.
  7. dataset    Final dataset: subpatches with complete data, ordered columns, and
                Gower distance + rank for every Mars reference site.

Usage
-----
    python mars_analog_data_pipeline.py                          # everything
    python mars_analog_data_pipeline.py --steps albedo lst dataset
    python mars_analog_data_pipeline.py --steps relief cleaning fine --continents africa asia

Every step is resumable: each one keeps a cache (DEM tiles, WorldCover windows,
subpatches, MODIS dates) and a re-run only processes what is still pending.

Input files (in the working folder, ANALOGS_ROOT or the current directory)
-------------------------------------------------------------------------
    mars_reference_sites.csv                    Mars site signatures (step "dataset")
    world/teammate_coarse_patches_clean.csv     optional: pre-filtered coarse grid for
                                                North America (used in the published run)
Everything else (DEM, WorldCover, GHS-POP, MODIS) is downloaded automatically.

Requirements
------------
    pip install numpy pandas scipy rasterio requests tqdm matplotlib "joblib>=1.4"
    pip install earthaccess pyhdf          # albedo and lst steps (+ a free Earthdata Login account)

Sources: Copernicus DEM GLO-30 (ESA/Airbus, AWS Open Data), ESA WorldCover 2021,
GHS-POP R2023A (JRC, European Commission), NASA MODIS MCD43C3 and MYD11C3 (LP DAAC).
"""
import argparse
import hashlib
import logging
import math
import os
import re
import shutil
import tempfile
import time
import warnings
import zipfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import requests
from joblib import Parallel, delayed
from rasterio.enums import Resampling
from rasterio.windows import Window, from_bounds
from scipy.ndimage import (binary_erosion, distance_transform_edt, maximum_filter,
                           minimum_filter, uniform_filter)
from tqdm.auto import tqdm

try:                                   # only needed for the MODIS steps
    import earthaccess
except ImportError:
    earthaccess = None

# Working folder: per-continent outputs, the integrated world table and the final dataset go here.
ROOT = os.path.abspath(os.environ.get("ANALOGS_ROOT", os.getcwd()))


# =============================================================================
# 1. GENERAL CONFIGURATION AND TERRAIN FUNCTIONS (shared by Earth and Mars)
# =============================================================================
# ============================== PARALELISMO ===================================
# Each tile is downloaded and processed in a separate PROCESS (joblib/loky; also works in Jupyter on macOS).
# The bottleneck is the network, so it pays to run more processes than CPU cores.
# Each process uses ~0.5 GB of RAM: 16 workers are fine with 16 GB; use 8 with 8 GB.
N_WORKERS = min(16, 2 * (os.cpu_count() or 4))

# ============================== RUTAS =========================================
# Per-continent paths are set in configure_continent() (section 7).
BASE_DIR = os.path.join(ROOT, "continent_patches")

COARSE_CSV = os.path.join(BASE_DIR, "patches_continent.csv")      # stage 1 output (coarse relief)
TILES_DIR = os.path.join(BASE_DIR, "tiles_v2")                     # DEM tile cache
CLEANING_DIR = os.path.join(BASE_DIR, "cleaning")                  # WorldCover / GHS-POP results and cache
DATA_DIR = os.path.join(ROOT, "shared_data")   # global GHS-POP raster, downloaded once
FINE_DIR = os.path.join(BASE_DIR, "fine")                          # stage 2 output (~5 km subpatches)

# ============================== ESCALA ========================================
# MOLA (Mars) has ~463 m pixels. Slope and roughness DEPEND on scale: at 30 m they come out
# much higher than at 463 m. To compare with Mars, the Earth DEM is averaged to this scale
# BEFORE slope and roughness are computed (with the same function used for MOLA).
MARS_SCALE_M = 463.0
MIN_VALID_BLOCK = 0.5      # a downsampled pixel is NaN if more than half of its 30 m pixels are NaN
EROSION_PX = 2                # pixels (at downsampled scale) dropped next to data gaps / coastlines

# Lakes: Copernicus is a DSM and FLATTENS water bodies to a constant height, creating
# "perfectly flat" areas that are not terrain. A pixel whose 5x5 neighbourhood at 30 m has a
# range of EXACTLY 0 is treated as flattened water and removed before downsampling.
MASK_LAKES = True
LAKE_WINDOW = 5


# ======================= TERRAIN FUNCTIONS ====================================
def downsample(a, fy, fx, min_valid=MIN_VALID_BLOCK):
    """Block mean over fy x fx blocks ignoring NaN. A block is NaN if it has fewer than
    min_valid (fraction) valid pixels. The leftover edge is trimmed."""
    H, W = a.shape
    ny, nx = H // fy, W // fx
    b = a[: ny * fy, : nx * fx].reshape(ny, fy, nx, fx)
    val = np.isfinite(b)
    n = val.sum(axis=(1, 3))
    s = np.where(val, b, 0).sum(axis=(1, 3), dtype=np.float64)
    out = np.full((ny, nx), np.nan)
    ok = (n > 0) & (n >= min_valid * fy * fx)
    out[ok] = s[ok] / n[ok]
    return out


def terrain_variables(dem, dx_m, dy_m, erosion=EROSION_PX):
    """Slope (degrees) and roughness (m) of a DEM ALREADY at the comparison scale.
    Works the same for Earth (downsampled to ~463 m) and for MOLA.

    - Gaps (ocean, no data, lakes) are filled with the nearest valid value ONLY so the filters
      stay finite; pixels within `erosion` pixels of a gap are masked afterwards. This removes the
      fake "cliffs" that appear when gaps are filled with the tile mean (walls of hundreds of
      metres along every coastline).
    - Detrended roughness: r = dem - mean_3x3 ; rough = sqrt(mean_3x3(r^2)).
      A steep but smooth slope gives roughness ~0 (a plain 3x3 std would report it as rough).
    """
    dem = np.asarray(dem, dtype=np.float64)
    valid = np.isfinite(dem)
    if not valid.any():
        nan = np.full(dem.shape, np.nan)
        return nan, nan.copy()
    if valid.all():
        f = dem.copy()
    else:
        idx = distance_transform_edt(~valid, return_distances=False, return_indices=True)
        f = dem[tuple(idx)]
    f -= f.mean()                                         # centrar: evita perder precision
    gy, gx = np.gradient(f, dy_m, dx_m)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    r = f - uniform_filter(f, 3, mode="nearest")
    rough = np.sqrt(np.maximum(uniform_filter(r ** 2, 3, mode="nearest"), 0))
    interior = binary_erosion(valid, iterations=erosion, border_value=1) if erosion else valid
    slope[~interior] = np.nan
    rough[~interior] = np.nan
    return slope, rough


TERRAIN_FEATURES = ["elev_mean_m", "elev_std_m", "elev_min_m", "elev_max_m", "relief_m",
                    "slope_mean", "slope_p50", "slope_p95",
                    "rough_mean", "rough_p50", "rough_p95", "n_pix"]


def _summary_vec(e, s, r):
    """Features of a set of pixels (1D arrays at downsampled scale)."""
    e, s, r = e[np.isfinite(e)], s[np.isfinite(s)], r[np.isfinite(r)]
    out = dict.fromkeys(TERRAIN_FEATURES, np.nan)
    out["n_pix"] = int(s.size)
    if e.size:
        p5, p95 = np.percentile(e, [5, 95])
        out.update(elev_mean_m=e.mean(), elev_std_m=e.std(), elev_min_m=e.min(), elev_max_m=e.max(),
                   relief_m=p95 - p5)
    if s.size:
        out.update(slope_mean=s.mean(), slope_p50=np.median(s), slope_p95=np.percentile(s, 95))
    if r.size:
        out.update(rough_mean=r.mean(), rough_p50=np.median(r), rough_p95=np.percentile(r, 95))
    return out


def summarize_groups(ids, elev, slope, rough):
    """Features per group (patch or subpatch). ids: integer per pixel, -1 = outside.
    Returns a DataFrame indexed by id."""
    ids = np.asarray(ids).ravel()
    elev, slope, rough = (np.asarray(x).ravel() for x in (elev, slope, rough))
    m = ids >= 0
    ids, elev, slope, rough = ids[m], elev[m], slope[m], rough[m]
    order = np.argsort(ids, kind="stable")
    ids, elev, slope, rough = ids[order], elev[order], slope[order], rough[order]
    uniq, cuts = np.unique(ids, return_index=True)
    rows = {}
    for k, a, b in zip(uniq, cuts, list(cuts[1:]) + [len(ids)]):
        rows[int(k)] = _summary_vec(elev[a:b], slope[a:b], rough[a:b])
    return pd.DataFrame.from_dict(rows, orient="index", columns=TERRAIN_FEATURES)


def terrain_window(dem, dx_m, dy_m):
    """Features of ONE DEM window already at the comparison scale. Typical use: MOLA around
    Jezero/Gale. Note: in simple-cylindrical MOLA, dx_m = 463 * cos(lat) and dy_m = 463."""
    slope, rough = terrain_variables(dem, dx_m, dy_m)
    return _summary_vec(np.asarray(dem, float).ravel(), slope.ravel(), rough.ravel())


def flat_mask(dem, win_size=LAKE_WINDOW):
    """True where the win_size x win_size neighbourhood has a range of EXACTLY 0 (flattened DSM water).
    NaN is treated as 'not flat' (filled with a value that breaks the tie)."""
    valid = np.isfinite(dem)
    d = np.where(valid, dem, -1e6).astype(np.float32)
    flat = (maximum_filter(d, win_size) - minimum_filter(d, win_size)) == 0
    # dilate: the ring of win_size//2 pixels along the lake shore is also flattened water
    flat = maximum_filter(flat, win_size)
    return flat & valid


def downsampling_factors(rx, ry, lat_c, escala=MARS_SCALE_M):
    """Native pixels per downsampled pixel (fy, fx) and real size of a native pixel (dx_m, dy_m)."""
    dx_m = rx * 111320 * math.cos(math.radians(lat_c))
    dy_m = ry * 110574
    return max(1, round(escala / dy_m)), max(1, round(escala / dx_m)), dx_m, dy_m


def dem_maps(dem, transform, lat_c):
    """Everything derived from one DEM tile, at downsampled scale.
    Returns a dict with: elev, slope, rough, frac_ocean, frac_flat (2D) and lat, lon (1D centres)."""
    rx, ry = transform.a, -transform.e
    fy, fx, dx_m, dy_m = downsampling_factors(rx, ry, lat_c)
    nan_orig = ~np.isfinite(dem)                                   # ocean / no data
    flat = flat_mask(dem)
    dem_t = dem.copy()
    if MASK_LAKES:
        dem_t[flat] = np.nan
    elev = downsample(dem_t, fy, fx)
    frac_oc = downsample(nan_orig.astype(np.float32), fy, fx, min_valid=0)
    pl = np.where(nan_orig, np.nan, flat.astype(np.float32))      # flat fraction over land
    frac_pl = downsample(pl, fy, fx, min_valid=1e-9)
    slope, rough = terrain_variables(elev, dx_m * fx, dy_m * fy)
    ny, nx = elev.shape
    lat = transform.f - (np.arange(ny) * fy + fy / 2) * ry
    lon = transform.c + (np.arange(nx) * fx + fx / 2) * rx
    return dict(elev=elev, slope=slope, rough=rough, frac_ocean=frac_oc, frac_flat=frac_pl,
                lat=lat, lon=lon, pix_m=round((dx_m * fx + dy_m * fy) / 2, 1))


# =============================================================================
# 2. STAGE 1 · COARSE RELIEF (0.5° patches, Copernicus DEM GLO-30)
# =============================================================================
REGIONS = {}     # set per continent in configure_continent()
ACTIVE_REGIONS = []

PATCH_LAT = 0.5
LAT_SWITCH = 52.0
OCEAN_AS_NAN = True
# A stricter value (0.5) loses coastal deserts (Sechura, coastal Atacama, La Guajira). Coarse
# patches are let through and stage 2 drops the SUBPATCHES that are mostly sea.
MAX_OCEAN_FRAC = 0.90

DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
# GDAL: do not list the bucket, merge contiguous HTTP ranges (fewer round trips per tile)
DEM_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
               GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_HTTP_MULTIPLEX="YES",
               GDAL_HTTP_VERSION="2", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2")
COARSE_COLS = (["lat", "lon", "patch_lat_deg", "patch_lon_deg", "frac_ocean", "frac_exact_flat"]
               + TERRAIN_FEATURES + ["pix_m"])


def lon_width(la):
    return 0.5 if abs(la + 0.5) < LAT_SWITCH else 1.0


def tile_url(la, lo):
    ns = "N" if la >= 0 else "S"
    ew = "E" if lo >= 0 else "W"
    name = f"Copernicus_DSM_COG_10_{ns}{abs(la):02d}_00_{ew}{abs(lo):03d}_00_DEM"
    return f"{DEM_BASE}/{name}/{name}.tif"


def read_tile(la, lo, attempts=3):
    """(status, data). status: 'ok' | 'ocean' (tile does not exist) | 'error' (network).
    data = (float32 DEM with NaN, transform)."""
    url = tile_url(la, lo)
    try:
        r = requests.head(url, timeout=20)
    except requests.RequestException:
        return "error", None
    if r.status_code in (403, 404):
        return "ocean", None
    if r.status_code != 200:
        return "error", None
    for _ in range(attempts):
        try:
            with rasterio.Env(**DEM_ENV), rasterio.open(url) as src:
                dem = src.read(1).astype(np.float32)
                nd, transform = src.nodata, src.transform
            if nd is not None:
                dem[dem == nd] = np.nan
            dem[dem < -1000] = np.nan
            if OCEAN_AS_NAN:
                dem[dem == 0] = np.nan
            return "ok", (dem, transform)
        except rasterio.errors.RasterioIOError:
            continue
    return "error", None


def process_tile(la, lo):
    status, data = read_tile(la, lo)
    if status == "error":
        return None
    if status == "ocean" or np.isnan(data[0]).all():
        return pd.DataFrame(columns=COARSE_COLS)
    dem, transform = data
    M = dem_maps(dem, transform, la + 0.5)
    dlat, dlon = PATCH_LAT, lon_width(la)
    LAT, LON = np.meshgrid(M["lat"], M["lon"], indexing="ij")
    pi = np.floor((la + 1 - LAT) / dlat).astype(int)
    pj = np.floor((LON - lo) / dlon).astype(int)
    n_i, n_j = round(1 / dlat), round(1 / dlon)
    inside = (pi >= 0) & (pi < n_i) & (pj >= 0) & (pj < n_j)
    ids = np.where(inside, pi * n_j + pj, -1)

    feats = summarize_groups(ids, M["elev"], M["slope"], M["rough"])
    s = pd.DataFrame({"id": ids.ravel(), "oc": M["frac_ocean"].ravel(), "pl": M["frac_flat"].ravel()})
    s = s[s["id"] >= 0].groupby("id").mean()
    feats = feats.join(s, how="right")
    rows = []
    for k, f in feats.iterrows():
        if f["oc"] > MAX_OCEAN_FRAC or not np.isfinite(f["slope_p50"]):
            continue
        i, j = divmod(int(k), n_j)
        rows.append({"lat": round(la + 1 - (i + 0.5) * dlat, 4), "lon": round(lo + (j + 0.5) * dlon, 4),
                      "patch_lat_deg": dlat, "patch_lon_deg": dlon,
                      "frac_ocean": f["oc"], "frac_exact_flat": f["pl"],
                      **{c: f[c] for c in TERRAIN_FEATURES}, "pix_m": M["pix_m"]})
    return pd.DataFrame(rows, columns=COARSE_COLS)


def tiles_for(regions):
    tiles = set()
    for name in regions:
        lat_min, lat_max, lon_min, lon_max = REGIONS[name]
        for la in range(math.floor(lat_min), math.ceil(lat_max)):
            for lo in range(math.floor(lon_min), math.ceil(lon_max)):
                tiles.add((la, lo))
    return sorted(tiles)


def _tile_worker(la, lo):
    """Runs in a separate process. Never raises: one odd tile must not kill the run."""
    try:
        return la, lo, process_tile(la, lo)
    except Exception as e:
        return la, lo, f"exception: {e!r}"


def extract(regions=None, n_workers=None):
    regions = regions or ACTIVE_REGIONS
    n_workers = n_workers or N_WORKERS
    os.makedirs(TILES_DIR, exist_ok=True)
    tiles = tiles_for(regions)
    f_empty = os.path.join(TILES_DIR, "tiles_no_data.txt")
    empty_tiles = set()
    if os.path.exists(f_empty):
        with open(f_empty) as fh:
            empty_tiles = {l.strip() for l in fh if l.strip()}
    tag = f"p{PATCH_LAT}_c{LAT_SWITCH}_e{int(MARS_SCALE_M)}_oc{MAX_OCEAN_FRAC}_lg{int(MASK_LAKES)}"
    file_path = lambda la, lo: os.path.join(TILES_DIR, f"tile_{la}_{lo}_{tag}.csv")

    parts, failures = [], []
    pend = []
    for la, lo in tiles:
        if f"{la},{lo}" in empty_tiles:
            continue
        if os.path.exists(file_path(la, lo)):
            parts.append(pd.read_csv(file_path(la, lo)))
        else:
            pend.append((la, lo))
    print(f"Regions: {list(regions)} -> {len(tiles)} tiles | {len(parts)} cached | "
          f"{len(empty_tiles)} ocean | {len(pend)} to process with {n_workers} processes")

    n = sum(len(p) for p in parts)
    bar = tqdm(total=len(pend))
    jobs = Parallel(n_jobs=n_workers, backend="loky", return_as="generator_unordered")(
        delayed(_tile_worker)(la, lo) for la, lo in pend)
    with open(f_empty, "a") as fv:
        for la, lo, df_t in jobs:                # only the main process writes
            bar.update(1)
            if df_t is None or isinstance(df_t, str):
                failures.append((la, lo, df_t or "network"))
            elif len(df_t) == 0:
                fv.write(f"{la},{lo}\n"); fv.flush()
            else:
                df_t.to_csv(file_path(la, lo), index=False)
                parts.append(df_t)
                n += len(df_t)
            bar.set_postfix(patches=n, failures=len(failures))
    bar.close()
    if failures:
        print(f"{len(failures)} tiles failed (network); run again to retry: {failures[:10]}")
    if not parts:
        raise RuntimeError("0 patches. Check the internet connection or try a small region first.")
    df = pd.concat(parts, ignore_index=True)
    df.to_csv(COARSE_CSV, index=False)
    print(f"{len(df)} patches saved to {COARSE_CSV}")
    return df


def plot_map(df, col="slope_p50", folder=None):
    folder = folder or BASE_DIR
    fig, ax = plt.subplots(figsize=(10, 7))
    sc = ax.scatter(df["lon"], df["lat"], c=df[col], s=6, cmap="magma", vmax=df[col].quantile(0.98))
    plt.colorbar(sc, label=col)
    ax.set_xlabel("lon"); ax.set_ylabel("lat"); ax.set_title(f"Patches: {col}")
    ax.set_aspect("equal")
    plt.tight_layout()
    plt.savefig(os.path.join(folder, f"mapa_{col}.png"), dpi=150)


# =============================================================================
# 3. STAGE 1 · LAND COVER, POPULATION AND COARSE FILTER (ESA WorldCover, GHS-POP)
# =============================================================================
# ============================== CONFIG =======================================
CSV_IN = COARSE_CSV
OUT_DIR = CLEANING_DIR

# --- WorldCover ---
WC_VERSION, WC_YEAR = "v200", 2021
WC_BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
DECIM = 3                                            # 1 = native 10 m; 3 = 30 m (mode)
STRIP_ROWS = 1800                                    # rows per read (multiple of DECIM)

# --- Main population source: GHS-POP R2023A, 30 arcsec WGS84 (zip ~400 MB, downloaded once) ---
# Global coverage. If the download fails, fetch it manually and set GHS_POP_TIF to the .tif path.
GHS_POP_TIF = os.path.join(DATA_DIR, "GHS_POP_E2020_GLOBE_R2023A_4326_30ss_V1_0.tif")
GHS_POP_URL = ("https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/"
               "GHS_POP_E2020_GLOBE_R2023A_4326_30ss/V1-0/GHS_POP_E2020_GLOBE_R2023A_4326_30ss_V1_0.zip")

# If GHS-POP has no data (NaN) in empty areas (desert, high plateau, ice), the rule
# 'no_population' drops the patch. Set True to treat NaN as 0 inhabitants.
POP_NAN_AS_ZERO = False

# --- Optional alternative population: WorldPop 1 km. It is distributed PER COUNTRY and this script
# reads a single raster, so it is disabled. The rules do NOT use this column. ---
USE_ALT_POPULATION = False
WORLDPOP_TIF = os.path.join(DATA_DIR, "col_ppp_2020_1km_Aggregated.tif")      # ejemplo: Colombia
WORLDPOP_URL = "https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/COL/col_ppp_2020_1km_Aggregated.tif"

# --- VIIRS night lights (optional): manual download; set the path of an annual radiance composite ---
USE_VIIRS = False
VIIRS_TIF = None                                     # e.g. "/path/to/VNL_v2_2023_global.tif"

# --- Rules: a patch is KEPT only if it meets ALL of them ---
THRESHOLDS = {
    "frac_urban_max": 0.01,     # < 1 %
    "frac_cropland_max": 0.05,    # < 5 %
    "pop_density_max": 5.0,     # < 5 inhabitants/km2
    "arid_min": 0.60,           # frac_bare + frac_shrub > 60 %
    "frac_trees_max": 0.15,    # < 15 %
    "frac_water_max": 0.50,       # drops water-dominated patches
}
SCENARIOS = {
    "strict": {"frac_urban_max": 0.005, "frac_cropland_max": 0.02, "pop_density_max": 2.0,
                 "arid_min": 0.80, "frac_trees_max": 0.05, "frac_water_max": 0.30},
    "medium": THRESHOLDS,
    "lax": {"frac_urban_max": 0.03, "frac_cropland_max": 0.15, "pop_density_max": 20.0,
             "arid_min": 0.40, "frac_trees_max": 0.30, "frac_water_max": 0.70},
}

# --- Validation sites: (lat, lon, should_stay). Cities and rainforest should GO; known Mars
# analogs (e.g. the Atacama desert) should STAY. ---
SITES = {}       # validation sites, set per continent

GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2", VSI_CACHE="TRUE")
# =============================================================================

EARTH_RADIUS_KM = 6371.0088
COLS_LC = ["frac_urban", "frac_cropland", "frac_trees", "frac_shrub", "frac_grass",
           "frac_bare", "frac_wetland", "frac_other", "frac_water"]
VALID_CLASSES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
CLASS_GROUPS = {"frac_trees": [10], "frac_shrub": [20], "frac_grass": [30], "frac_cropland": [40],
          "frac_urban": [50], "frac_bare": [60], "frac_wetland": [90, 95], "frac_other": [70, 100]}


# ----------------------------- utilidades ------------------------------------
def save_csv(df, file_path):
    """Escritura atomica; nunca crea CSV vacios."""
    if len(df) == 0:
        print(f"  (no rows: {os.path.basename(file_path)} not written)")
        return
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp = file_path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, file_path)


def patch_area_km2(lat_c, dlat, dlon):
    """True spherical area of a lat/lon cell of height dlat and width dlon (degrees) centred at lat_c."""
    s, n = math.radians(lat_c - dlat / 2), math.radians(lat_c + dlat / 2)
    return EARTH_RADIUS_KM ** 2 * math.radians(dlon) * (math.sin(n) - math.sin(s))


def _key(lat, lon):
    return np.round(np.asarray(lat) * 1e4).astype(np.int64), np.round(np.asarray(lon) * 1e4).astype(np.int64)


def download_file(url, dest, desc):
    """Downloads with a progress bar to a .part file, then os.replace. Raises RuntimeError with instructions on failure."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        r = requests.get(url, stream=True, timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not connect to download {desc}: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"{desc}: HTTP {r.status_code} at {url}. Download it manually and set its path "
                           f"in the configuration (GHS_POP_TIF / WORLDPOP_TIF).")
    total = int(r.headers.get("content-length", 0))
    tmp = dest + ".part"
    with open(tmp, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=desc) as b:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            b.update(len(chunk))
    os.replace(tmp, dest)


def ensure_raster(tif, url, desc):
    """Returns the local .tif path; downloads it (and unzips it) if missing."""
    if tif and os.path.exists(tif):
        return tif
    if url is None:
        raise FileNotFoundError(f"Falta {desc}: {tif}")
    if url.lower().endswith(".zip"):
        zpath = os.path.join(DATA_DIR, os.path.basename(url))
        if not os.path.exists(zpath):
            download_file(url, zpath, desc + " (zip)")
        with zipfile.ZipFile(zpath) as z:
            member = [m for m in z.namelist() if m.lower().endswith(".tif")][0]
            tmp_dir = os.path.join(DATA_DIR, "_unzip")
            z.extract(member, tmp_dir)
        os.replace(os.path.join(tmp_dir, member), tif)
    else:
        download_file(url, tif, desc)
    return tif


# ----------------------------- WorldCover ------------------------------------
def wc_tile(lat, lon):
    la0, lo0 = int(math.floor(lat / 3.0) * 3), int(math.floor(lon / 3.0) * 3)
    return f"{'N' if la0 >= 0 else 'S'}{abs(la0):02d}{'E' if lo0 >= 0 else 'W'}{abs(lo0):03d}"


def wc_url(tile):
    return f"{WC_BASE}/{WC_VERSION}/{WC_YEAR}/map/ESA_WorldCover_10m_{WC_YEAR}_{WC_VERSION}_{tile}_Map.tif"


def wc_existe(url):
    """'ok' | 'ocean' (tile does not exist) | 'error' (network)."""
    try:
        r = requests.head(url, timeout=30, allow_redirects=True)
    except requests.RequestException:
        return "error"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (403, 404):
        return "ocean"
    return "error"


def _window(src, w, s, e, n):
    """Integer window (c0, r0, c1, r1) clipped to the raster; None if they do not intersect."""
    win = from_bounds(w, s, e, n, src.transform)
    c0, r0 = int(round(win.col_off)), int(round(win.row_off))
    c1, r1 = c0 + max(1, int(round(win.width))), r0 + max(1, int(round(win.height)))
    c0, r0, c1, r1 = max(c0, 0), max(r0, 0), min(c1, src.width), min(r1, src.height)
    if c1 <= c0 or r1 <= r0:
        return None
    return c0, r0, c1, r1


def count_classes(src, lat, lon, dlat, dlon):
    """Class histogram (256 bins) of the patch, read in strips, decimated by DECIM (mode)."""
    hy, hx = dlat / 2, dlon / 2
    v = _window(src, lon - hx, lat - hy, lon + hx, lat + hy)
    counts = np.zeros(256, dtype=np.int64)
    if v is None:
        return counts
    c0, r0, c1, r1 = v
    width = c1 - c0
    for rs in range(r0, r1, STRIP_ROWS):
        height = min(STRIP_ROWS, r1 - rs)
        win = Window(c0, rs, width, height)
        if DECIM > 1:
            arr = src.read(1, window=win, out_shape=(max(1, height // DECIM), max(1, width // DECIM)),
                           resampling=Resampling.mode)
        else:
            arr = src.read(1, window=win)
        counts += np.bincount(arr.ravel(), minlength=256)[:256]
    return counts


def fractions(counts):
    """Land-cover fractions (denominator = land pixels) and frac_water (over all valid pixels)."""
    observed = int(counts[VALID_CLASSES].sum())
    out = {c: np.nan for c in COLS_LC}
    if observed == 0:
        return out
    water = int(counts[80])
    land = observed - water
    out["frac_water"] = water / observed
    if land > 0:
        for col, classes in CLASS_GROUPS.items():
            out[col] = float(sum(counts[c] for c in classes)) / land
    return out


def process_tile_lc(tile, patches, bar):
    """Devuelve (estado, DataFrame|None). estado: ok | oceano | error."""
    url = wc_url(tile)
    status = wc_existe(url)
    if status != "ok":
        return status, None
    rows = []
    try:
        with rasterio.Env(**GDAL_ENV), rasterio.open(url) as src:
            for lat, lon, dlat, dlon in zip(patches["lat"], patches["lon"],
                                            patches["patch_lat_deg"], patches["patch_lon_deg"]):
                rows.append({"lat": lat, "lon": lon, **fractions(count_classes(src, lat, lon, dlat, dlon))})
                bar.update(1)
    except rasterio.errors.RasterioIOError:
        return "error", None
    return "ok", pd.DataFrame(rows)


class _NullBar:
    def update(self, *a):
        pass


def _lc_worker(tile, g, f):
    """One WorldCover tile in a separate process."""
    try:
        status, res = process_tile_lc(tile, g, _NullBar())
    except Exception:
        status, res = "error", None
    return tile, f, status, res


def enrich_landcover(df):
    cache = os.path.join(OUT_DIR, "cache_lc")
    os.makedirs(cache, exist_ok=True)
    f_empty = os.path.join(OUT_DIR, "wc_tiles_no_data.txt")
    empty_tiles = set()
    if os.path.exists(f_empty):
        with open(f_empty) as fh:
            empty_tiles = {l.strip() for l in fh if l.strip()}

    df = df.copy()
    df["_tile"] = [wc_tile(a, b) for a, b in zip(df["lat"], df["lon"])]
    parts, failures, pend = [], [], []
    for tile, g in df.groupby("_tile"):
        # the coordinate hash prevents reusing a cache built for a different set of patches
        h = hashlib.md5(",".join(f"{a:.4f}_{b:.4f}_{c}" for a, b, c in
                                 zip(g["lat"], g["lon"], g["patch_lon_deg"])).encode()).hexdigest()[:8]
        f = os.path.join(cache, f"lc_{tile}_d{DECIM}_{WC_VERSION}_{h}.csv")
        if tile in empty_tiles:
            continue
        if os.path.exists(f):
            parts.append(pd.read_csv(f))
        else:
            pend.append((tile, g[["lat", "lon", "patch_lat_deg", "patch_lon_deg"]], f))
    print(f"WorldCover: {len(parts)} tiles cached, {len(pend)} to process with {N_WORKERS} processes")

    bar = tqdm(total=len(pend), desc="WorldCover (tiles)")
    jobs = Parallel(n_jobs=N_WORKERS, backend="loky", return_as="generator_unordered")(
        delayed(_lc_worker)(tile, g, f) for tile, g, f in pend)
    for tile, f, status, res in jobs:
        bar.update(1)
        if status == "error":
            failures.append(tile)
        elif status == "ocean":
            empty_tiles.add(tile)
            with open(f_empty, "a") as fh:
                fh.write(tile + "\n")
        else:
            save_csv(res, f)
            parts.append(res)
        bar.set_postfix(failures=len(failures))
    bar.close()

    if failures:
        print(f"\n!! {len(failures)} WorldCover tiles failed (NETWORK, not cached): {failures}\n"
              f"   Run again to retry; until then those patches have no land cover.")
    df["_k1"], df["_k2"] = _key(df["lat"], df["lon"])
    if parts:
        lc = pd.concat(parts, ignore_index=True)
        lc["_k1"], lc["_k2"] = _key(lc["lat"], lc["lon"])
        df = df.merge(lc[["_k1", "_k2"] + COLS_LC], on=["_k1", "_k2"], how="left")
    else:
        for c in COLS_LC:
            df[c] = np.nan
    # Missing WorldCover tile: land cover is NaN, water is NOT assumed.
    df["lc_no_tile"] = df["_tile"].isin(empty_tiles)
    df["lc_network_error"] = df["_tile"].isin(failures)
    return df.drop(columns=["_k1", "_k2"])


# ------------------------------ Poblacion -----------------------------------
def aggregate_window(src, lat, lon, dlat, dlon, mode="sum", neg_invalid=True):
    """Sum (or mean) of valid values inside the patch; NaN if there is no data."""
    hy, hx = dlat / 2, dlon / 2
    v = _window(src, lon - hx, lat - hy, lon + hx, lat + hy)
    if v is None:
        return np.nan
    c0, r0, c1, r1 = v
    arr = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0)).astype(np.float64)
    bad = ~np.isfinite(arr)
    if src.nodata is not None:
        bad |= arr == src.nodata
    if neg_invalid:
        bad |= arr < 0
    if bad.all():
        return np.nan
    arr = arr[~bad]
    return float(arr.sum() if mode == "sum" else arr.mean())


def _per_patch(df, src, desc, mode="sum", neg_invalid=True):
    return [aggregate_window(src, a, b, c, d, mode, neg_invalid) for a, b, c, d in
            tqdm(zip(df["lat"], df["lon"], df["patch_lat_deg"], df["patch_lon_deg"]), total=len(df), desc=desc)]


def enrich_population(df):
    df = df.copy()
    df["area_km2"] = [patch_area_km2(a, c, d) for a, c, d in
                      zip(df["lat"], df["patch_lat_deg"], df["patch_lon_deg"])]

    ghs = ensure_raster(GHS_POP_TIF, GHS_POP_URL, "GHS-POP 2020 (30 arcsec)")
    with rasterio.open(ghs) as src:
        df["pop_total"] = _per_patch(df, src, "GHS-POP")
    if POP_NAN_AS_ZERO:
        df["pop_total"] = df["pop_total"].fillna(0.0)
    df["pop_density"] = df["pop_total"] / df["area_km2"]

    if USE_ALT_POPULATION:
        wp = ensure_raster(WORLDPOP_TIF, WORLDPOP_URL, "WorldPop 1 km")
        with rasterio.open(wp) as src:
            df["pop_total_alt"] = _per_patch(df, src, "WorldPop")
        df["pop_density_alt"] = df["pop_total_alt"] / df["area_km2"]   # NaN outside the raster's country

    if USE_VIIRS:
        if not (VIIRS_TIF and os.path.exists(VIIRS_TIF)):
            print("!! USE_VIIRS=True but VIIRS_TIF does not exist: light_mean skipped.")
        else:
            with rasterio.open(VIIRS_TIF) as src:
                df["light_mean"] = _per_patch(df, src, "VIIRS", "mean", False)
    return df


# ------------------------------- Rules --------------------------------------
def evaluate(df, u=None):
    """Adds 'reason' (all failed rules, separated by ;) and 'keep'."""
    u = THRESHOLDS if u is None else u
    df = df.copy()
    v = lambda c: df[c].to_numpy(dtype=float)
    no_lc = np.isnan(v("frac_urban"))
    has_lc = ~no_lc
    pop = v("pop_density")
    with np.errstate(invalid="ignore"):
        rules = {
            "no_land_or_cover": no_lc,
            "urban": has_lc & ~(v("frac_urban") < u["frac_urban_max"]),
            "cropland": has_lc & ~(v("frac_cropland") < u["frac_cropland_max"]),
            "not_arid": has_lc & ~((v("frac_bare") + v("frac_shrub")) > u["arid_min"]),
            "trees": has_lc & ~(v("frac_trees") < u["frac_trees_max"]),
            "population": ~np.isnan(pop) & ~(pop < u["pop_density_max"]),
            "no_population": np.isnan(pop),
        }
        if u.get("frac_water_max") is not None:
            rules["water"] = has_lc & ~(v("frac_water") <= u["frac_water_max"])
        if u.get("frac_flat_max") is not None and "frac_exact_flat" in df:
            # lakes flattened by the DSM (used in stage 2; diagnostic only at the coarse stage)
            rules["flat_lake"] = ~(v("frac_exact_flat") <= u["frac_flat_max"])
    names = list(rules)
    mat = np.column_stack([rules[k] for k in names])
    df["reason"] = [";".join(n for n, m in zip(names, grid_row) if m) for grid_row in mat]
    df["keep"] = df["reason"] == ""
    return df


def sensitivity(df):
    rows = []
    for name, u in SCENARIOS.items():
        e = evaluate(df, u)
        rows.append({"scenario": name, "patches_total": len(e), "kept": int(e["keep"].sum()),
                      "pct_kept": round(100 * e["keep"].mean(), 2),
                      "thresholds": "; ".join(f"{k}={val}" for k, val in u.items() if val is not None)})
    return pd.DataFrame(rows)


# ----------------------------- Validation -----------------------------------
def patch_containing(df, lat, lon):
    """Patch containing the point (uses each patch's height and width); None if none."""
    hy = df["patch_lat_deg"] / 2 + 1e-9
    hx = df["patch_lon_deg"] / 2 + 1e-9
    m = ((df["lat"] - lat).abs() <= hy) & ((df["lon"] - lon).abs() <= hx)
    if not m.any():
        return None
    sub = df[m]
    return sub.iloc[int(np.argmin(((sub["lat"] - lat) ** 2 + (sub["lon"] - lon) ** 2).to_numpy()))]


def validate(df_eval, sites=None):
    sites = SITES if sites is None else sites
    print("\n=========== VALIDATION WITH KNOWN SITES ===========")
    rows, n_failures = [], 0
    for name, (lat, lon, should_stay) in sites.items():
        p = patch_containing(df_eval, lat, lon)
        if p is None:
            res = "not in the CSV (ocean, coast or region not processed)"
            ok = None
            reason = ""
        else:
            ok = bool(p["keep"]) == should_stay
            res = "STAYS" if p["keep"] else "GOES"
            reason = p["reason"]
        expected = "should stay" if should_stay else "should go"
        mark = "OK     " if ok else ("CHECK" if ok is False else "?      ")
        n_failures += ok is False
        print(f"[{mark}] {name:42s} ({expected}) -> {res}")
        if p is not None:
            print(f"           patch ({p['lat']}, {p['lon']}) | urban {p['frac_urban']:.3f} | cropland "
                  f"{p['frac_cropland']:.3f} | trees {p['frac_trees']:.3f} | arid (bare+shrub) "
                  f"{p['frac_bare'] + p['frac_shrub']:.3f} | ice/lichen {p['frac_other']:.3f} | "
                  f"pop {p['pop_density']:.2f} inh/km2" + (f" | reason: {reason}" if reason else ""))
        rows.append({"site": name, "expected": expected, "result": res, "ok": ok, "reason": reason})
    if n_failures:
        print(f"\n{n_failures} site(s) do NOT match the expectation. No threshold was changed. "
              f"Check each detail line (which fraction or threshold caused it) and decide whether to "
              f"change the thresholds or the patch size.")
    return pd.DataFrame(rows)


def check_sums(df):
    cols = [c for c in COLS_LC if c not in ("frac_water",)]
    s = df[cols].sum(axis=1, min_count=len(cols))
    ok = s.dropna()
    bad_mask = ((ok - 1).abs() > 0.01).sum()
    print(f"Check: land fractions sum to ~1 in {len(ok) - bad_mask}/{len(ok)} patches with land"
          + (f"  (!! {bad_mask} do not sum to 1)" if bad_mask else ""))


# -------------------------------- main --------------------------------------
OLD_RELIEF_COLS = ["elev_mean_m", "elev_std_m", "elev_min_m", "elev_max_m", "relief_m",
                       "slope_mean", "slope_p50", "slope_p95", "slope_max",
                       "rough_mean", "rough_p50", "rough_p95", "pix_m"]


def run_cleaning():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = pd.read_csv(CSV_IN)
    missing_cols = {"patch_lat_deg", "patch_lon_deg"} - set(df.columns)
    if missing_cols:
        raise ValueError(f"The CSV is missing {missing_cols}: create it with extract()")
    if "slope_max" in df.columns:
        print("Legacy CSV detected: only the grid and frac_ocean are used; its relief columns are dropped "
              "(they had the edge artifact). Corrected relief is computed in stage 2.")
        df = df.drop(columns=[c for c in OLD_RELIEF_COLS if c in df.columns])
    print(f"{len(df)} patches in {CSV_IN}")

    df = enrich_landcover(df)
    check_sums(df)
    df = enrich_population(df)
    df = df.drop(columns=["_tile"])

    ev = evaluate(df)
    save_csv(ev, os.path.join(OUT_DIR, f"patches_{CONTINENT}_enriched.csv"))
    save_csv(ev[ev["keep"]].drop(columns=["reason", "keep"]),
                os.path.join(OUT_DIR, f"patches_{CONTINENT}_clean.csv"))
    save_csv(ev[~ev["keep"]].drop(columns=["keep"]),
                os.path.join(OUT_DIR, "discarded_patches.csv"))

    sens = sensitivity(df)
    save_csv(sens, os.path.join(OUT_DIR, "sensitivity.csv"))
    print("\n=========== SENSITIVITY ===========")
    print(sens.to_string(index=False))
    print(f"\nKept (THRESHOLDS): {int(ev['keep'].sum())} of {len(ev)}")
    print("Most frequent reasons among discarded rows:")
    print(ev.loc[~ev["keep"], "reason"].str.split(";").explode().value_counts().to_string())

    validate(ev)
    if ev["lc_network_error"].any():
        print("\n!! Some patches have no land cover because of network errors: run again to complete them.")
    return ev

# =============================================================================
# 4. STAGE 2 · ~5 KM SUBPATCHES
# =============================================================================
PATCH_KM = 5.0                    # subpatch side. With 463 m pixels: ~110 pixels per subpatch
MAX_OCEAN_FRAC_FINE = 0.50
MIN_PIX_FINE = 25                 # minimum valid pixels (at 463 m) to trust the percentiles
SURVIVORS_CSV = None   # set per continent
THRESHOLDS_FINE = {**THRESHOLDS,
                 "frac_water_max": 0.30,      # sea along the coast counts as water: keep this at ~0.3 or higher
                 "frac_flat_max": 0.05}     # >5 % of pixels with an exactly-zero 5x5 range = flattened lake


# ------------------------------ geometria -----------------------------------
def subdivisions(lat_c, dlat, dlon, km=PATCH_KM):
    n_lat = max(1, round(dlat * 110.574 / km))
    n_lon = max(1, round(dlon * 111.32 * math.cos(math.radians(lat_c)) / km))
    return n_lat, n_lon


def sub_index(lat, lon, lat_c, lon_c, dlat, dlon, n_lat, n_lon):
    """Subpatch id (0..n_lat*n_lon-1) for each point; -1 if it falls outside the coarse patch."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    i = np.floor((lat_c + dlat / 2 - lat) / (dlat / n_lat)).astype(int)
    j = np.floor((lon - (lon_c - dlon / 2)) / (dlon / n_lon)).astype(int)
    ok = (i >= 0) & (i < n_lat) & (j >= 0) & (j < n_lon)
    return np.where(ok, i * n_lon + j, -1)


def sub_centers(lat_c, lon_c, dlat, dlon, n_lat, n_lon):
    k = np.arange(n_lat * n_lon)
    i, j = k // n_lon, k % n_lon
    sl, so = dlat / n_lat, dlon / n_lon
    return lat_c + dlat / 2 - (i + 0.5) * sl, lon_c - dlon / 2 + (j + 0.5) * so, sl, so


def window_centers(src, c0, r0, height, width, out_h, out_w):
    """Lat/lon centres of a window read with out_shape (out_h, out_w)."""
    t = src.transform
    sx = width / out_w * t.a
    sy = height / out_h * (-t.e)
    x0 = t.c + c0 * t.a
    y0 = t.f + r0 * t.e
    lon = x0 + (np.arange(out_w) + 0.5) * sx
    lat = y0 - (np.arange(out_h) + 0.5) * sy
    return np.meshgrid(lat, lon, indexing="ij")


# ------------------------------ lectores ------------------------------------
def open_wc(tile):
    """('ok', src) | ('ocean', None) | ('error', None). Kept separate so it can be tested offline."""
    url = wc_url(tile)
    status = wc_existe(url)
    if status != "ok":
        return status, None
    try:
        return "ok", rasterio.open(url)
    except rasterio.errors.RasterioIOError:
        return "error", None


def count_classes_sub(src, lat_c, lon_c, dlat, dlon, n_lat, n_lon):
    """WorldCover class histogram (n_sub, 256) per subpatch, with ONE read of the patch."""
    nsub = n_lat * n_lon
    counts = np.zeros((nsub, 256), dtype=np.int64)
    v = _window(src, lon_c - dlon / 2, lat_c - dlat / 2, lon_c + dlon / 2, lat_c + dlat / 2)
    if v is None:
        return counts
    c0, r0, c1, r1 = v
    width, height = c1 - c0, r1 - r0
    out_h, out_w = max(1, height // DECIM), max(1, width // DECIM)
    arr = src.read(1, window=Window(c0, r0, width, height), out_shape=(out_h, out_w),
                   resampling=Resampling.mode if DECIM > 1 else Resampling.nearest)
    LAT, LON = window_centers(src, c0, r0, height, width, out_h, out_w)
    sub = sub_index(LAT, LON, lat_c, lon_c, dlat, dlon, n_lat, n_lon).ravel()
    m = sub >= 0
    flat = np.bincount(sub[m] * 256 + arr.ravel()[m].astype(np.int64), minlength=nsub * 256)
    return flat[: nsub * 256].reshape(nsub, 256)


def sum_by_sub(src, lat_c, lon_c, dlat, dlon, n_lat, n_lon):
    """Sum of valid values (population) per subpatch; NaN if the subpatch has no data."""
    nsub = n_lat * n_lon
    out = np.full(nsub, np.nan)
    v = _window(src, lon_c - dlon / 2, lat_c - dlat / 2, lon_c + dlon / 2, lat_c + dlat / 2)
    if v is None:
        return out
    c0, r0, c1, r1 = v
    arr = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0)).astype(np.float64)
    LAT, LON = window_centers(src, c0, r0, r1 - r0, c1 - c0, r1 - r0, c1 - c0)
    bad = ~np.isfinite(arr) | (arr < 0)
    if src.nodata is not None:
        bad |= arr == src.nodata
    sub = sub_index(LAT, LON, lat_c, lon_c, dlat, dlon, n_lat, n_lon).ravel()
    m = (sub >= 0) & ~bad.ravel()
    s = np.bincount(sub[m], weights=arr.ravel()[m], minlength=nsub)[:nsub]
    n = np.bincount(sub[m], minlength=nsub)[:nsub]
    out[n > 0] = s[n > 0]
    return out


# ------------------------------ per patch ----------------------------------
def process_parent(p, M, wc_src, pop_src):
    """DataFrame of the subpatches of coarse patch p (row with lat, lon, patch_lat_deg, patch_lon_deg).
    M = dem_maps of the tile that contains it. wc_src None = missing WorldCover tile."""
    lat_c, lon_c, dlat, dlon = p["lat"], p["lon"], p["patch_lat_deg"], p["patch_lon_deg"]
    n_lat, n_lon = subdivisions(lat_c, dlat, dlon)
    nsub = n_lat * n_lon
    la_s, lo_s, sl, so = sub_centers(lat_c, lon_c, dlat, dlon, n_lat, n_lon)

    LAT, LON = np.meshgrid(M["lat"], M["lon"], indexing="ij")
    ids = sub_index(LAT, LON, lat_c, lon_c, dlat, dlon, n_lat, n_lon)
    feats = summarize_groups(ids, M["elev"], M["slope"], M["rough"]).reindex(range(nsub))
    aux = pd.DataFrame({"id": ids.ravel(), "oc": M["frac_ocean"].ravel(), "pl": M["frac_flat"].ravel()})
    aux = aux[aux["id"] >= 0].groupby("id").mean().reindex(range(nsub))

    df = pd.DataFrame({"lat": np.round(la_s, 5), "lon": np.round(lo_s, 5),
                       "patch_lat_deg": sl, "patch_lon_deg": so,
                       "parent_lat": lat_c, "parent_lon": lon_c,
                       "frac_ocean": aux["oc"].to_numpy(), "frac_exact_flat": aux["pl"].to_numpy()})
    for c in TERRAIN_FEATURES:
        df[c] = feats[c].to_numpy()
    df["n_pix"] = df["n_pix"].fillna(0).astype(int)
    df["pix_m"] = M["pix_m"]

    if wc_src is not None:
        cnt = count_classes_sub(wc_src, lat_c, lon_c, dlat, dlon, n_lat, n_lon)
        lc = pd.DataFrame([fractions(c) for c in cnt])
    else:
        lc = pd.DataFrame(np.nan, index=range(nsub), columns=COLS_LC)
    for c in COLS_LC:
        df[c] = lc[c].to_numpy()
    df["lc_no_tile"] = wc_src is None

    df["area_km2"] = [patch_area_km2(a, sl, so) for a in df["lat"]]
    df["pop_total"] = sum_by_sub(pop_src, lat_c, lon_c, dlat, dlon, n_lat, n_lon)
    if POP_NAN_AS_ZERO:
        df["pop_total"] = df["pop_total"].fillna(0.0)
    df["pop_density"] = df["pop_total"] / df["area_km2"]
    return df


def fine_cache_path(p):
    tag = f"k{PATCH_KM}_e{int(MARS_SCALE_M)}_d{DECIM}_lg{int(MASK_LAKES)}"
    return os.path.join(FINE_DIR, "cache", f"sub_{p['lat']:.4f}_{p['lon']:.4f}_{tag}.csv")


def _fine_worker(la, lo, g, ghs):
    """One DEM tile with all its surviving patches, in a separate process.
    Each patch writes ITS OWN cache CSV (distinct names: no clashes between processes)."""
    failures = []
    try:
        status, data = read_tile(la, lo)
        if status != "ok":
            return [(la, lo, status)]
        M = dem_maps(data[0], data[1], la + 0.5)
        open_wc_tiles = {}
        with rasterio.Env(**GDAL_ENV), rasterio.open(ghs) as pop_src:
            for _, p in g.iterrows():
                t = wc_tile(p["lat"], p["lon"])
                if t not in open_wc_tiles:
                    open_wc_tiles[t] = open_wc(t)
                wc_status, wc_src = open_wc_tiles[t]
                if wc_status == "error":
                    failures.append((la, lo, f"worldcover {t}"))
                    continue
                try:
                    save_csv(process_parent(p, M, wc_src, pop_src), fine_cache_path(p))
                except rasterio.errors.RasterioIOError:
                    failures.append((la, lo, f"read {t}"))
            for est, src in open_wc_tiles.values():
                if src is not None:
                    src.close()
    except Exception as e:
        failures.append((la, lo, f"exception: {e!r}"))
    return failures


def stage2_fine(survivors_csv=None, n_workers=None):
    survivors_csv = survivors_csv or SURVIVORS_CSV
    n_workers = n_workers or N_WORKERS
    os.makedirs(os.path.join(FINE_DIR, "cache"), exist_ok=True)
    parents = pd.read_csv(survivors_csv)
    parents["_la"] = np.floor(parents["lat"]).astype(int)        # DEM tile (SW corner)
    parents["_lo"] = np.floor(parents["lon"]).astype(int)
    f_cache = fine_cache_path

    pend = parents[[not os.path.exists(f_cache(p)) for _, p in parents.iterrows()]]
    groups = list(pend.groupby(["_la", "_lo"]))
    print(f"{len(parents)} surviving coarse patches | {len(parents) - len(pend)} cached | "
          f"{len(groups)} DEM tiles to download with {n_workers} processes")

    ghs = ensure_raster(GHS_POP_TIF, GHS_POP_URL, "GHS-POP 2020 (30 arcsec)")   # downloaded once, up front
    failures = []
    jobs = Parallel(n_jobs=n_workers, backend="loky", return_as="generator_unordered")(
        delayed(_fine_worker)(la, lo, g, ghs) for (la, lo), g in groups)
    for err in tqdm(jobs, total=len(groups), desc="DEM tiles"):
        failures.extend(err)
    if failures:
        print(f"!! {len(failures)} failures (NOT cached; run again): {failures[:10]}")

    parts = [pd.read_csv(f_cache(p)) for _, p in parents.iterrows() if os.path.exists(f_cache(p))]
    if not parts:
        raise RuntimeError("0 subpatches: check the network or the survivors CSV.")
    df = pd.concat(parts, ignore_index=True)
    n0 = len(df)
    df = df[(df["frac_ocean"] <= MAX_OCEAN_FRAC_FINE) & (df["n_pix"] >= MIN_PIX_FINE)].reset_index(drop=True)
    print(f"{n0} subpatches; {n0 - len(df)} removed (mostly sea or too few valid pixels)")

    check_sums(df)
    ev = evaluate(df, THRESHOLDS_FINE)
    os.makedirs(FINE_DIR, exist_ok=True)
    save_csv(ev, os.path.join(FINE_DIR, "subpatches_enriched.csv"))
    save_csv(ev[ev["keep"]].drop(columns=["reason", "keep"]),
                os.path.join(FINE_DIR, "subpatches_clean.csv"))
    print(f"\nKept: {int(ev['keep'].sum())} of {len(ev)} subpatches "
          f"(~{PATCH_KM:g} km, {int(ev['keep'].sum()) * PATCH_KM ** 2:,.0f} km2 approx.)")
    print("Most frequent reasons among discarded rows:")
    print(ev.loc[~ev["keep"], "reason"].str.split(";").explode().value_counts().to_string())
    validate(ev)
    return ev


# =============================================================================
# 5. TEMPORAL ALBEDO SUMMARY (MODIS MCD43C3 v061)
# =============================================================================
# ------------------------------ CONFIG ---------------------------------------
ALB_INPUT = os.path.join(ROOT, "world", "world_integrated_clean.csv")
ALB_OUTPUT = os.path.join(ROOT, "world", "world_integrated_clean_albedo.csv")
ALB_START, ALB_END = "2019-01-01", "2021-12-31"   # 3 anios: promedia anios raros (lluvias, sequias)
ALB_DAY_STEP = 8          # 1 of every 8 days -> ~137 files. Use 4 for more dates
ALB_BATCH = 8               # files downloaded at once
ALB_MAX_QUALITY = 2        # BRDF_Quality bits 0-2: 0 = mejor ... 5 = peor. <=2 = inversiones completas/buenas
ALB_MAX_SNOW = 0          # Percent_Snow above this = snowy date (excluded from albedo, counted in frac_snow)
ALB_MIN_DATES = 10        # with fewer valid dates, the statistics are NaN
ALB_VERBOSE = True         # one line per file: size, download speed, read time, % valid
ALB_CACHE = os.path.join(ROOT, "world", "cache_albedo")   # one small file per date: an interrupted run loses nothing

ALB_RES, ALB_LAT0, ALB_LON0 = 0.05, 90.0, -180.0       # CMG grid: 3600 rows x 7200 columns, NW corner
ALB_FILL = 32767


# ------------------------------ reading --------------------------------------
ALB_WARNED = set()


def alb_norm(x):
    return re.sub(r"[^a-z0-9]", "", x.lower())


def alb_find(names, *candidates):
    """Actual dataset name inside the HDF (ignoring case, spaces and dashes)."""
    norm = {alb_norm(d): d for d in names}
    for c in candidates:                      # normalised exact match
        if alb_norm(c) in norm:
            return norm[alb_norm(c)]
    for c in candidates:                      # partial match
        hits = sorted(d for n, d in norm.items() if alb_norm(c) in n)
        if hits:
            return hits[0]
    return None


def alb_read_day(file_path):
    """Returns (float32 albedo with NaN, uint8 quality, uint8 snow) from one daily HDF4 file."""
    from pyhdf.SD import SD, SDC
    f = SD(file_path, SDC.READ)
    try:
        names = list(f.datasets().keys())
        if "list" not in ALB_WARNED:
            ALB_WARNED.add("list")
            tqdm.write("Datasets in the HDF: " + ", ".join(sorted(names)))
        n_alb = alb_find(names, "Albedo_WSA_shortwave", "WSA_shortwave", "Albedo_WSA_SW")
        if n_alb is None:
            raise KeyError("White-sky shortwave albedo not found. Datasets: " + ", ".join(sorted(names)))
        n_q = alb_find(names, "BRDF_Quality", "Quality")
        n_s = alb_find(names, "Percent_Snow", "PercentSnow")   # not plain "Snow": there are snow-albedo bands
        if (n_alb, n_q, n_s) not in ALB_WARNED:
            ALB_WARNED.add((n_alb, n_q, n_s))
            tqdm.write(f"Using: albedo='{n_alb}' | quality='{n_q}' | snow='{n_s}'")
        alb = f.select(n_alb).get()
        q = f.select(n_q).get() if n_q else np.zeros(alb.shape, np.uint8)
        snow = f.select(n_s).get() if n_s else np.zeros(alb.shape, np.uint8)
    finally:
        f.end()
    alb = np.where(alb == ALB_FILL, np.nan, alb * 0.001).astype(np.float32)
    return alb, (q & 0b111).astype(np.uint8), snow.astype(np.uint8)


def alb_grid_indices(lat, lon):
    grid_row = np.clip(np.floor((ALB_LAT0 - lat) / ALB_RES).astype(int), 0, 3599)
    col = np.clip(np.floor((lon - ALB_LON0) / ALB_RES).astype(int), 0, 7199)
    return grid_row, col


def alb_sample(alb, q, snow, grid_row, col):
    """One day sampled at every subpatch: albedo (NaN if poor quality or snow) and a snow flag."""
    a, qq, nn = alb[grid_row, col], q[grid_row, col], snow[grid_row, col]
    has_snow = (nn > ALB_MAX_SNOW) & (nn <= 100)
    a = np.where((qq <= ALB_MAX_QUALITY) & ~has_snow, a, np.nan)
    observed = np.isfinite(alb[grid_row, col]) & (qq <= ALB_MAX_QUALITY)        # observed day (with or without snow)
    return a.astype(np.float32), has_snow & observed, observed


# ------------------------------ estadisticos ---------------------------------
def alb_compendium(M, N, V):
    """M: (n_subpatches, n_dates) albedo with NaN. N/V: snow / valid-day flags."""
    n = np.isfinite(M).sum(axis=1)
    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore", RuntimeWarning)
        p10, p25, p50, p75, p90 = np.nanpercentile(M, [10, 25, 50, 75, 90], axis=1)
        std = np.nanstd(M, axis=1)
        frac_snow = N.sum(axis=1) / np.maximum(V.sum(axis=1), 1)
    out = pd.DataFrame({"alb_median": p50, "alb_p10": p10, "alb_p90": p90, "alb_iqr": p75 - p25,
                        "alb_std": std, "alb_n": n, "frac_snow": frac_snow})
    too_few = n < ALB_MIN_DATES
    out.loc[too_few, ["alb_median", "alb_p10", "alb_p90", "alb_iqr", "alb_std"]] = np.nan
    return out.astype({"alb_n": int})


# ------------------------------ per-date cache -------------------------------
def alb_name(g):
    return os.path.basename(g.data_links()[0])


def alb_cache_path(g):
    return os.path.join(ALB_CACHE, alb_name(g) + ".npz")


def alb_save_cache(g, a, nv, v):
    """Albedo as uint16 (x1000, 65535 = NaN) + packed flags: ~2 MB per date."""
    a16 = np.where(np.isfinite(a), np.round(a * 1000), 65535).astype(np.uint16)
    np.savez_compressed(alb_cache_path(g), a=a16, n=np.packbits(nv), v=np.packbits(v), size=len(a))


def alb_read_cache(g):
    z = np.load(alb_cache_path(g))
    k = int(z["size"])
    a = np.where(z["a"] == 65535, np.nan, z["a"] / 1000).astype(np.float32)
    return a, np.unpackbits(z["n"])[:k].astype(bool), np.unpackbits(z["v"])[:k].astype(bool)


def alb_log(msg):
    if ALB_VERBOSE:
        tqdm.write(time.strftime("%H:%M:%S ") + msg)


# ------------------------------ main -----------------------------------------
def alb_main():
    df = pd.read_csv(ALB_INPUT, usecols=["lat", "lon"])
    global ALB_CACHE   # the cache stores values PER ROW; a different input CSV gets its own cache
    ALB_CACHE = f"{ALB_CACHE}_{len(df)}"
    grid_row, col = alb_grid_indices(df["lat"].to_numpy(), df["lon"].to_numpy())
    print(f"{len(df):,} subpatches")

    earthaccess.login()            # asks for the Earthdata username/password the first time (or uses ~/.netrc)
    granules = earthaccess.search_data(short_name="MCD43C3", version="061", temporal=(ALB_START, ALB_END))
    granules = granules[::ALB_DAY_STEP]
    print(f"{len(granules)} dates to process ({ALB_START} to {ALB_END}, 1 of every {ALB_DAY_STEP} days)")

    os.makedirs(ALB_CACHE, exist_ok=True)
    pend = [g for g in granules if not os.path.exists(alb_cache_path(g))]
    print(f"{len(granules) - len(pend)} dates cached | {len(pend)} to download")
    if ALB_VERBOSE:
        logging.getLogger("earthaccess").setLevel(logging.INFO)

    bar = tqdm(total=len(pend), desc="dates", unit="date")
    tmp = tempfile.mkdtemp(prefix="mcd43c3_")
    t_ini, mb_total = time.time(), 0.0
    try:
        for i in range(0, len(pend), ALB_BATCH):
            batch = pend[i:i + ALB_BATCH]
            alb_log(f"lote {i // ALB_BATCH + 1}/{-(-len(pend) // ALB_BATCH)}: descargando {len(batch)} archivos "
                f"({alb_name(batch[0])[9:17]} ... {alb_name(batch[-1])[9:17]})")
            t0 = time.time()
            paths = earthaccess.download(batch, tmp, threads=ALB_BATCH, show_progress=ALB_VERBOSE)
            dt = time.time() - t0
            mb = sum(os.path.getsize(str(r)) for r in paths if os.path.exists(str(r))) / 1e6
            mb_total += mb
            alb_log(f"  download: {mb:,.0f} MB in {dt:,.0f} s -> {mb / max(dt, 1e-9):.1f} MB/s"
                + (f"  (!! only {len(paths)} of {len(batch)} arrived)" if len(paths) < len(batch) else ""))
            by_name = {os.path.basename(str(r)): str(r) for r in paths}
            for g in batch:
                r = by_name.get(alb_name(g))
                if r is None:
                    alb_log(f"  MISSING {alb_name(g)} (not downloaded; retried on the next run)")
                    continue
                try:
                    t1 = time.time()
                    a, nv, v = alb_sample(*alb_read_day(r), grid_row, col)
                    alb_save_cache(g, a, nv, v)
                    alb_log(f"  {alb_name(g)[:24]}  leido+muestreado {time.time() - t1:.1f} s | "
                        f"valid albedo {np.isfinite(a).mean():.0%} | snow {nv.mean():.1%}")
                except Exception as e:
                    alb_log(f"  ERROR {alb_name(g)}: {e!r}")
                finally:
                    if os.path.exists(r):
                        os.remove(r)
                bar.update(1)
            done = bar.n
            if done:
                seg = (time.time() - t_ini) / done
                alb_log(f"  average {seg:.0f} s/date | ~{seg * (len(pend) - done) / 60:.0f} min left "
                    f"| downloaded {mb_total / 1e3:.1f} GB")
    finally:
        bar.close()
        shutil.rmtree(tmp, ignore_errors=True)

    ready = [g for g in granules if os.path.exists(alb_cache_path(g))]
    print(f"Building the matrix with {len(ready)} dates...")
    cols = [alb_read_cache(g) for g in tqdm(ready, desc="reading cache")]
    cols_a, cols_n, cols_v = ([c[k] for c in cols] for k in range(3))
    M, N, V = (np.column_stack(x) for x in (cols_a, cols_n, cols_v))
    est = alb_compendium(M, N, V)
    full = pd.concat([pd.read_csv(ALB_INPUT, low_memory=False), est], axis=1)
    full.to_csv(ALB_OUTPUT, index=False)
    print(f"\n{ALB_OUTPUT}: {len(full):,} rows | dates used: {M.shape[1]}")
    print(est.describe().round(3).T.to_string())


# =============================================================================
# 6. TEMPORAL LAND SURFACE TEMPERATURE SUMMARY (MODIS MYD11C3 v061)
# =============================================================================
# ------------------------------ CONFIG ---------------------------------------
LST_INPUT = os.path.join(ROOT, "world", "world_integrated_clean_albedo.csv")
LST_FALLBACK_INPUT = os.path.join(ROOT, "world", "world_integrated_clean.csv")   # used if the albedo step has not finished
LST_OUTPUT = os.path.join(ROOT, "world", "world_integrated_clean_albedo_lst.csv")
LST_CACHE = os.path.join(ROOT, "world", "cache_lst")
LST_START, LST_END = "2016-01-01", "2020-12-31"   # 60 months
LST_BATCH = 6
LST_QC_MAX = 1            # QC bits 0-1: 0 = good quality, 1 = produced, other quality; 2-3 = not produced
LST_MIN_MONTHS = 12        # with fewer valid months, the statistics are NaN
LST_VERBOSE = True

LST_RES, LST_LAT0, LST_LON0 = 0.05, 90.0, -180.0       # malla CMG 3600 x 7200, esquina NW
LST_SCALE, LST_OFFSET_C = 0.02, -273.15           # raw value * 0.02 = Kelvin; 0 = no data


def lst_log(msg):
    if LST_VERBOSE:
        tqdm.write(time.strftime("%H:%M:%S ") + msg)


# ------------------------------ HDF reading ----------------------------------
LST_WARNED = set()


def lst_norm(x):
    return re.sub(r"[^a-z0-9]", "", x.lower())


def lst_find(names, *candidates):
    norm = {lst_norm(d): d for d in names}
    for c in candidates:
        if lst_norm(c) in norm:
            return norm[lst_norm(c)]
    for c in candidates:
        hits = sorted(d for n, d in norm.items() if lst_norm(c) in n)
        if hits:
            return hits[0]
    return None


def lst_read_month(file_path):
    """(lst_day_C, lst_night_C) float32 with NaN where there is no data or quality is poor."""
    from pyhdf.SD import SD, SDC
    f = SD(file_path, SDC.READ)
    try:
        names = list(f.datasets().keys())
        if "list" not in LST_WARNED:
            LST_WARNED.add("list")
            tqdm.write("Datasets in the HDF: " + ", ".join(sorted(names)))
        nd = lst_find(names, "LST_Day_CMG", "LST_Day")
        nn = lst_find(names, "LST_Night_CMG", "LST_Night")
        qd = lst_find(names, "QC_Day")
        qn = lst_find(names, "QC_Night")
        if nd is None or nn is None:
            raise KeyError("LST day/night not found. Datasets: " + ", ".join(sorted(names)))
        if (nd, nn, qd, qn) not in LST_WARNED:
            LST_WARNED.add((nd, nn, qd, qn))
            tqdm.write(f"Using: day='{nd}' | night='{nn}' | day QC='{qd}' | night QC='{qn}'")
        out = []
        for n_lst, n_qc in ((nd, qd), (nn, qn)):
            raw = f.select(n_lst).get().astype(np.float32)
            ok = raw > 0
            if n_qc:
                ok &= (f.select(n_qc).get() & 0b11) <= LST_QC_MAX
            out.append(np.where(ok, raw * LST_SCALE + LST_OFFSET_C, np.nan).astype(np.float32))
    finally:
        f.end()
    return out[0], out[1]


def lst_grid_indices(lat, lon):
    grid_row = np.clip(np.floor((LST_LAT0 - lat) / LST_RES).astype(int), 0, 3599)
    col = np.clip(np.floor((lon - LST_LON0) / LST_RES).astype(int), 0, 7199)
    return grid_row, col


# ------------------------------ per-month cache ------------------------------
def lst_name(g):
    return os.path.basename(g.data_links()[0])


def lst_cache_path(g):
    return os.path.join(LST_CACHE, lst_name(g) + ".npz")


def lst_a16(x):   # degrees C -> uint16 in hundredths of a degree above -150 C (65535 = NaN)
    return np.where(np.isfinite(x), np.round((x + 150) * 100), 65535).astype(np.uint16)


def lst_de16(x):
    return np.where(x == 65535, np.nan, x / 100 - 150).astype(np.float32)


def lst_save_cache(g, d, n):
    np.savez_compressed(lst_cache_path(g), d=lst_a16(d), n=lst_a16(n))


def lst_read_cache(g):
    z = np.load(lst_cache_path(g))
    return lst_de16(z["d"]), lst_de16(z["n"])


# ------------------------------ estadisticos ---------------------------------
def lst_stats(M, stat_prefix):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        p10, p25, p50, p75, p90 = np.nanpercentile(M, [10, 25, 50, 75, 90], axis=1)
    return {f"{stat_prefix}_median_c": p50, f"{stat_prefix}_p10_c": p10, f"{stat_prefix}_p90_c": p90,
            f"{stat_prefix}_iqr_c": p75 - p25}


def lst_compendium(D, N):
    """D, N: (n_subpatches, n_months) in C. Amplitude only for months with both day AND night."""
    A = D - N                                    # NaN if either one is missing
    est = {**lst_stats(D, "lst_day"), **lst_stats(N, "lst_night"), **lst_stats(A, "lst_amp")}
    est = pd.DataFrame(est)
    n = np.isfinite(A).sum(axis=1)
    est["lst_n"] = n
    est.loc[n < LST_MIN_MONTHS, [c for c in est.columns if c.endswith("_c")]] = np.nan
    # percentiles (0-100) within the set of candidate subpatches
    for c in [c for c in est.columns if c.endswith("_c")]:
        est[c.replace("_c", "_pct")] = est[c].rank(pct=True) * 100
    return est


# ------------------------------ main -----------------------------------------
def lst_main():
    input_path = LST_INPUT if os.path.exists(LST_INPUT) else LST_FALLBACK_INPUT
    if input_path != LST_INPUT:
        print(f"!! {LST_INPUT} does not exist (albedo step not finished): using {input_path}. "
              f"Run this step again after the albedo step: months are cached, so it takes seconds.")
    df_ll = pd.read_csv(input_path, usecols=["lat", "lon"])
    global LST_CACHE   # the cache stores values PER ROW; a different input CSV gets its own cache
    LST_CACHE = f"{LST_CACHE}_{len(df_ll)}"
    grid_row, col = lst_grid_indices(df_ll["lat"].to_numpy(), df_ll["lon"].to_numpy())
    print(f"{len(df_ll):,} subpatches in {input_path}")

    earthaccess.login()
    granules = earthaccess.search_data(short_name="MYD11C3", version="061", temporal=(LST_START, LST_END))
    print(f"{len(granules)} months ({LST_START} to {LST_END})")
    os.makedirs(LST_CACHE, exist_ok=True)
    pend = [g for g in granules if not os.path.exists(lst_cache_path(g))]
    print(f"{len(granules) - len(pend)} months cached | {len(pend)} to download")

    bar = tqdm(total=len(pend), desc="months", unit="month")
    tmp = tempfile.mkdtemp(prefix="myd11c3_")
    t_ini, mb_total = time.time(), 0.0
    try:
        for i in range(0, len(pend), LST_BATCH):
            batch = pend[i:i + LST_BATCH]
            lst_log(f"lote {i // LST_BATCH + 1}/{-(-len(pend) // LST_BATCH)}: descargando {len(batch)} archivos")
            t0 = time.time()
            paths = earthaccess.download(batch, tmp, threads=LST_BATCH, show_progress=LST_VERBOSE)
            dt = time.time() - t0
            mb = sum(os.path.getsize(str(r)) for r in paths if os.path.exists(str(r))) / 1e6
            mb_total += mb
            lst_log(f"  download: {mb:,.0f} MB in {dt:,.0f} s -> {mb / max(dt, 1e-9):.1f} MB/s"
                + (f"  (!! only {len(paths)} of {len(batch)} arrived)" if len(paths) < len(batch) else ""))
            by_name = {os.path.basename(str(r)): str(r) for r in paths}
            for g in batch:
                r = by_name.get(lst_name(g))
                if r is None:
                    lst_log(f"  MISSING {lst_name(g)} (retried on the next run)")
                    continue
                try:
                    t1 = time.time()
                    d, n = lst_read_month(r)
                    d, n = d[grid_row, col], n[grid_row, col]
                    lst_save_cache(g, d, n)
                    lst_log(f"  {lst_name(g)[:24]}  {time.time() - t1:.1f} s | valid day {np.isfinite(d).mean():.0%} "
                        f"(median {np.nanmedian(d):.1f} C) | valid night {np.isfinite(n).mean():.0%} "
                        f"(median {np.nanmedian(n):.1f} C)")
                except Exception as e:
                    lst_log(f"  ERROR {lst_name(g)}: {e!r}")
                finally:
                    if os.path.exists(r):
                        os.remove(r)
                bar.update(1)
            if bar.n:
                seg = (time.time() - t_ini) / bar.n
                lst_log(f"  average {seg:.0f} s/month | ~{seg * (len(pend) - bar.n) / 60:.0f} min left "
                    f"| downloaded {mb_total / 1e3:.1f} GB")
    finally:
        bar.close()
        shutil.rmtree(tmp, ignore_errors=True)

    ready = [g for g in granules if os.path.exists(lst_cache_path(g))]
    print(f"Building the matrix with {len(ready)} months...")
    cols = [lst_read_cache(g) for g in tqdm(ready, desc="reading cache")]
    D = np.column_stack([c[0] for c in cols])
    N = np.column_stack([c[1] for c in cols])
    est = lst_compendium(D, N)

    full = pd.read_csv(input_path, low_memory=False)
    full = full.drop(columns=[c for c in est.columns if c in full.columns])   # re-running does not duplicate columns
    full = pd.concat([full, est], axis=1)
    full.to_csv(LST_OUTPUT, index=False)
    print(f"\n{LST_OUTPUT}: {len(full):,} rows | months used: {D.shape[1]} | new columns: {est.shape[1]}")
    print(est[[c for c in est.columns if c.endswith("_c")] + ["lst_n"]].describe().round(1).T.to_string())


# =============================================================================
# 7. PER-CONTINENT CONFIGURATION
# =============================================================================
# Boxes (lat_min, lat_max, lon_min, lon_max) covering the land of each continent.
REGIONS_BY_CONTINENT = {
    "south_america": {
        "puerto_rico":       (17.0, 19.0, -68.0, -65.0),
        "guatemala":         (13.0, 18.6, -92.5, -88.0),
        "central_america":     (7.0, 17.0, -90.0, -77.0),
        "south_america_north":  (-5.0, 13.0, -82.0, -50.0),
        "south_america_center": (-25.0, -5.0, -82.0, -34.0),
        "south_america_south":    (-56.0, -25.0, -76.0, -48.0),
    },
    "europe": {
        "europe_west":  (34.0, 61.0, -11.0, 15.0),
        "europe_east":   (34.0, 61.0, 15.0, 60.0),
        "scandinavia":  (61.0, 71.5, 4.0, 60.0),
        "iceland":      (63.0, 67.0, -25.0, -13.0),
        "svalbard":      (74.0, 81.0, 10.0, 34.0),
        "novaya_zemlya": (70.0, 77.0, 50.0, 70.0),
    },
    "africa": {
        "africa_north":        (19.0, 38.0, -18.0, 37.0),
        "africa_center":       (-5.0, 19.0, -18.0, 44.0),
        "horn_of_africa":              (-5.0, 12.0, 44.0, 52.0),
        "africa_south":          (-35.0, -5.0, 8.0, 52.0),
        "canaries_cape_verde": (14.0, 30.0, -26.0, -13.0),
    },
    "oceania": {
        "australia":     (-44.0, -10.0, 112.0, 154.0),
        "new_guinea":  (-12.0, 0.0, 130.0, 156.0),
        "new_zealand": (-48.0, -34.0, 166.0, 179.0),
        "melanesia":     (-23.0, -13.0, 163.0, 171.0),
        "fiji_west":    (-22.0, -12.0, 176.0, 180.0),
        "fiji_east":     (-22.0, -12.0, -180.0, -178.0),   # crosses the antimeridian
        "hawaii":        (18.5, 22.5, -161.0, -154.0),
    },
    "asia": {
        "middle_east":    (12.0, 42.5, 26.0, 63.0),
        "asia_central":     (35.0, 56.0, 46.0, 88.0),
        "south_asia":         (5.0, 37.0, 60.0, 98.0),
        "china_mongolia":   (18.0, 54.0, 73.0, 135.0),
        "southeast_asia": (-11.0, 28.0, 92.0, 131.0),
        "japan_korea":      (24.0, 46.0, 122.0, 146.0),
        "siberia":          (50.0, 82.0, 60.0, 180.0),
        "chukotka_east":    (62.0, 72.0, -180.0, -168.0),  # crosses the antimeridian
    },
    # North America: in the published dataset, the filtered coarse grid for this region came
    # from another team member (world/teammate_coarse_patches_clean.csv) and only the fine
    # stage was run here. If that file does not exist, these boxes are used.
    "north_america": {
        "northern_mexico":       (23.0, 33.0, -117.5, -103.0),
        "southwest_usa":       (31.0, 42.5, -120.0, -103.0),
        "canadian_arctic":  (60.0, 83.0, -125.0, -60.0),
    },
}
CONTINENTS = list(REGIONS_BY_CONTINENT)
EXTERNAL_NA_PATCHES = os.path.join(ROOT, "world", "teammate_coarse_patches_clean.csv")

# Validation sites (lat, lon, should_stay). Printed only; they do not affect the filters.
SITES_BY_CONTINENT = {
    "south_america":   {"Salar de Uyuni": (-20.13, -67.49, True), "Atacama, Yungay": (-24.09, -69.93, True),
                     "Bogota": (4.71, -74.07, False), "Sao Paulo": (-23.55, -46.63, False),
                     "Amazon": (-3.47, -62.21, False)},
    "europe":       {"Holuhraun": (64.87, -16.83, True), "Madrid": (40.42, -3.70, False),
                     "Paris": (48.86, 2.35, False), "Moscow": (55.76, 37.62, False)},
    "africa":       {"Namib": (-24.70, 15.30, True), "Sahara, Libya": (25.00, 20.00, True),
                     "Cairo": (30.04, 31.24, False), "Lagos": (6.52, 3.38, False)},
    "oceania":      {"Simpson Desert": (-25.00, 137.00, True), "Sydney": (-33.87, 151.21, False)},
    "asia":         {"Qaidam Basin": (38.50, 91.50, True), "Gobi": (43.50, 103.00, True),
                     "Tokyo": (35.68, 139.69, False), "Delhi": (28.61, 77.21, False)},
    "north_america": {"Death Valley": (36.23, -116.77, True), "Pinacate": (31.77, -113.50, True),
                     "Phoenix": (33.45, -112.07, False), "Las Vegas": (36.17, -115.14, False)},
}
CONTINENT = None


def configure_continent(cont):
    """Sets the global paths and regions used by stages 1–3 for one continent."""
    global CONTINENT, BASE_DIR, COARSE_CSV, TILES_DIR, CLEANING_DIR, FINE_DIR
    global CSV_IN, OUT_DIR, SURVIVORS_CSV, REGIONS, ACTIVE_REGIONS, SITES
    CONTINENT = cont
    BASE_DIR = os.path.join(ROOT, f"{cont}_patches")
    COARSE_CSV = CSV_IN = os.path.join(BASE_DIR, f"patches_{cont}.csv")
    TILES_DIR = os.path.join(BASE_DIR, "tiles_v2")
    CLEANING_DIR = OUT_DIR = os.path.join(BASE_DIR, "cleaning")
    FINE_DIR = os.path.join(BASE_DIR, "fine")
    SURVIVORS_CSV = os.path.join(CLEANING_DIR, f"patches_{cont}_clean.csv")
    REGIONS = REGIONS_BY_CONTINENT[cont]
    ACTIVE_REGIONS = list(REGIONS)
    SITES = SITES_BY_CONTINENT.get(cont, {})
    for d in (BASE_DIR, TILES_DIR, CLEANING_DIR, FINE_DIR, DATA_DIR):
        os.makedirs(d, exist_ok=True)


def run_continent(cont, steps):
    configure_continent(cont)
    print(f"\n################ {cont.upper()} ################")
    external = cont == "north_america" and os.path.exists(EXTERNAL_NA_PATCHES)
    if external and ("cleaning" in steps or "relief" in steps):
        # coarse grid already filtered (provided by another team member)
        na = pd.read_csv(EXTERNAL_NA_PATCHES)
        na = na.loc[na["region"] != "south_america", ["lat", "lon", "patch_lat_deg", "patch_lon_deg"]]
        save_csv(na, SURVIVORS_CSV)
        print(f"North America: {len(na)} coarse patches taken from {EXTERNAL_NA_PATCHES}")
    else:
        if "relief" in steps:
            df = extract()
            plot_map(df, "slope_p50")
        if "cleaning" in steps:
            run_cleaning()
    if "fine" in steps:
        stage2_fine()


# =============================================================================
# 8. WORLD INTEGRATION
# =============================================================================
def integrate_world():
    """Merges the fine subpatches of all continents into world/world_integrated_*.csv."""
    parts = []
    for cont in CONTINENTS:
        f = os.path.join(ROOT, f"{cont}_patches", "fine", "subpatches_enriched.csv")
        if not os.path.exists(f):
            print(f"  missing {f}; skipping {cont}")
            continue
        x = pd.read_csv(f, low_memory=False)
        x.insert(0, "continent", cont)
        x.insert(1, "source", "teammate_recalculated_v2" if cont == "north_america" else "world_v2")
        parts.append(x)
    df = pd.concat(parts, ignore_index=True, sort=False)
    key = df["lat"].round(4).astype(str) + "_" + df["lon"].round(4).astype(str)
    dup = key.duplicated()                       # overlaps between neighbouring continent boxes
    df = df[~dup].reset_index(drop=True)
    df["level"], df["relief_scale_m"] = "subpatch_5km", int(MARS_SCALE_M)
    first_cols = ["continent", "source", "level", "relief_scale_m", "lat", "lon"]
    df = df[first_cols + [c for c in df.columns if c not in first_cols]]
    os.makedirs(os.path.join(ROOT, "world"), exist_ok=True)
    df.to_csv(os.path.join(ROOT, "world", "world_integrated_enriched.csv"), index=False)
    lim = df[df["keep"].astype(bool)]
    lim.to_csv(os.path.join(ROOT, "world", "world_integrated_clean.csv"), index=False)
    print(f"World: {len(df):,} subpatches ({dup.sum():,} duplicates removed); {len(lim):,} pass all rules")
    print(lim.groupby("continent").size().to_string())


# =============================================================================
# 9. FINAL DATASET
# =============================================================================
# Gower distance to every Mars site (same definition as in the search notebook)
EARTH_VARS = ["elev_std_m", "slope_p50", "slope_p95", "rough_p50", "rough_p95", "alb_median"]
MARS_VARS = ["elev_std_m", "slope_p50", "slope_p95", "rough_p50", "rough_p95", "vis_albedo_p50"]
GOWER_WEIGHTS = np.array([0.30, 0.15, 0.15, 0.15, 0.15, 0.10])
MARS_CSV = os.path.join(ROOT, "mars_reference_sites.csv")


def build_final_dataset():
    input_path = os.path.join(ROOT, "world", "world_integrated_clean_albedo_lst.csv")
    final_dir = os.path.join(ROOT, "dataset_final")
    os.makedirs(final_dir, exist_ok=True)
    d = pd.read_csv(input_path, low_memory=False)
    d = d[d["alb_median"].notna()].reset_index(drop=True)          # only subpatches with complete data
    d.insert(0, "subpatch_id", np.arange(len(d)))
    d = d.drop(columns=[c for c in ("level", "relief_scale_m", "lc_no_tile", "reason", "keep") if c in d])

    if os.path.exists(MARS_CSV):
        M = pd.read_csv(MARS_CSV)
        lo = np.minimum(d[EARTH_VARS].quantile(0.01).values, M[MARS_VARS].min().values)
        hi = np.maximum(d[EARTH_VARS].quantile(0.99).values, M[MARS_VARS].max().values)
        sp = np.maximum(hi - lo, 1e-12)
        X = np.clip((d[EARTH_VARS].values - lo) / sp, 0, 1)
        for _, r in M.iterrows():
            ref = np.clip((r[MARS_VARS].values.astype(float) - lo) / sp, 0, 1)
            g = (np.abs(X - ref) * GOWER_WEIGHTS).sum(axis=1)
            d[f"gower_{r['site']}"] = g.round(6)
            d[f"rank_{r['site']}"] = pd.Series(g).rank(method="first").astype(int).values
        M.dropna(axis=1, how="all").to_csv(os.path.join(final_dir, "mars_reference_sites.csv"), index=False)
    else:
        print(f"!! {MARS_CSV} not found: the dataset is written without Gower/rank columns")

    groups = [
        ["subpatch_id", "continent", "source", "lat", "lon", "patch_lat_deg", "patch_lon_deg",
         "parent_lat", "parent_lon", "area_km2"],
        ["elev_mean_m", "elev_min_m", "elev_max_m", "elev_std_m", "relief_m", "slope_mean", "slope_p50",
         "slope_p95", "rough_mean", "rough_p50", "rough_p95", "frac_exact_flat", "frac_ocean", "n_pix", "pix_m"],
        [c for c in d.columns if c.startswith("frac_") and c not in ("frac_exact_flat", "frac_ocean", "frac_snow")]
        + ["pop_total", "pop_density"],
        [c for c in d.columns if c.startswith("alb_")] + ["frac_snow"],
        [c for c in d.columns if c.startswith("lst_")],
        [c for c in d.columns if c.startswith("gower_") or c.startswith("rank_")],
    ]
    order = [c for g in groups for c in g if c in d.columns]
    d = d[order + [c for c in d.columns if c not in order]]
    f = os.path.join(final_dir, "earth_subpatches_analogs.csv")
    d.to_csv(f, index=False, float_format="%.6g")
    print(f"Final dataset: {f} ({len(d):,} rows x {d.shape[1]} columns)")


# =============================================================================
# 10. ENTRY POINT
# =============================================================================
STEPS = ["relief", "cleaning", "fine", "integrate", "albedo", "lst", "dataset"]


def main():
    ap = argparse.ArgumentParser(description="Data pipeline: Earth analogs of Mars sites")
    ap.add_argument("--steps", nargs="+", choices=STEPS, default=STEPS)
    ap.add_argument("--continents", nargs="+", choices=CONTINENTS, default=CONTINENTS)
    args = ap.parse_args()
    t0 = time.time()
    if any(p in args.steps for p in ("relief", "cleaning", "fine")):
        for cont in args.continents:
            run_continent(cont, args.steps)
    if "integrate" in args.steps:
        integrate_world()
    if "albedo" in args.steps or "lst" in args.steps:
        if earthaccess is None:
            raise SystemExit("Missing earthaccess/pyhdf: pip install earthaccess pyhdf")
    if "albedo" in args.steps:
        alb_main()
    if "lst" in args.steps:
        lst_main()
    if "dataset" in args.steps:
        build_final_dataset()
    print(f"\nDone in {(time.time() - t0) / 60:.1f} min. Working folder: {ROOT}")


if __name__ == "__main__":
    main()
