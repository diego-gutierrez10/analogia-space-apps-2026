# ANALOG(IA): Data-Driven Earth Analogs for Mars Exploration Sites

**NASA Space Apps Challenge 2026** · Challenge: *Identify Earth Locations that Analog the Permanent Moon Base Locations and Mars* · Team **La Cafeteria**

**Live platform:** https://analogia2026.netlify.app/

ANALOG(IA) ranks every arid, uninhabited 5 km patch of land on Earth by how closely its terrain matches a given site on Mars. It then groups the best matches into distinct field regions. We applied it to six sites: Jezero, Gale, Eberswalde, Holden, Saheki and the Olympus Mons caldera.

## Results at a glance

| Mars site | Best Earth analog | Gower distance | Similarity |
|---|---|---|---|
| Jezero | Mojave Desert, USA (35.70° N, 115.64° W) | 0.047 | 95.3% |
| Olympus Mons caldera | Pamir (37.61° N, 74.75° E) | 0.055 | 94.5% |
| Eberswalde | Tibetan Plateau (32.84° N, 87.03° E) | 0.079 | 92.1% |
| Saheki | Tarim / Kunlun margin (40.34° N, 78.72° E) | 0.189 | 81.2% |
| Holden | Mauritania (20.16° N, 13.23° W) | 0.217 | 78.3% |
| Gale | Aksai Chin (34.80° N, 80.58° E) | 0.284 | 71.6% |

Known Mars analogs (Atacama, Death Valley, Wadi Rum) rank in the top 1% of the global ranking.

## Download the Earth dataset

The Earth catalog is 228 MB zipped, too large for a normal GitHub file. Download `Dataset_world_clean.zip` from the [v1.0 release](https://github.com/diego-gutierrez10/analogia-space-apps-2026/releases/tag/v1.0) and unzip it into `Dataset/`.

## Repository structure

```
Code/
  Data Extraction/
    mars_crater_extraction.ipynb      # Mars site profiles from PDS DTMs + THEMIS
    mars_analog_data_pipeline.py      # Builds the global Earth catalog (708,793 subpatches)
    mars_reference_sites.csv          # Mars input table (one row per site)
    world/teammate_coarse_patches_clean.csv   # Coarse North America grid (optional input)
  Methodology/
    Methodology_Craters.ipynb         # Gower ranking, HDBSCAN clustering, metrics
Dataset/
  Craters.csv                         # Mars site profiles (6 sites × 53 variables)
  Dataset_world_clean.zip             # Earth catalog (708,793 rows × 71 variables) — download from the Releases page (see above)
  readme_craters.pdf                  # Variable dictionary for the Mars table
Results/<site>/
  top20_<site>.csv                    # Top 20 Earth analogs with per-variable contributions
  clusters_<site>.csv                 # Top 500 candidates with HDBSCAN cluster label
  metrics_<site>.csv                  # Clusters, noise %, silhouette, Davies–Bouldin, mean Gower
Website/marte-web/                    # Three.js Mars globe + analog explorer + AI assistant (development repo: JesusEBarcenas/mars-earth-analog-explorer)
Presentation/                         # Demo slides (PDF, PPTX)
translation_map.json                  # Original (Spanish) → English column names
```

## Method

1. **Mars profiles.** HiRISE, CTX and HRSC DTMs and THEMIS IRPBT4/VISALB4 scenes were downloaded from the NASA PDS via the Orbital Data Explorer. They were resampled to ~125 m/px and summarised as elevation std, slope p50/p95, roughness p50/p95, albedo and brightness temperature.
2. **Earth catalog.** The Copernicus GLO-30 DEM was processed at 463 m/px (MOLA scale) in 0.5° patches. Patches were filtered with ESA WorldCover 2021 and GHS-POP 2023 to remove urban, cropland, forest, water and populated areas, then split into ~5 km subpatches. MODIS albedo (MCD43C3) and LST (MYD11C3) were added.
3. **Similarity.** A weighted Gower distance was computed to every subpatch: variability 0.30, slopes 0.30, roughness 0.30, albedo 0.10. Scaling was shared (Earth p1–p99 widened to include Mars values). Similarity = 100 × (1 − distance).
4. **Clustering.** HDBSCAN (haversine, min_cluster_size 8, min_samples 4) was run on the top 500 candidates per site. Each cluster has geographic, geomorphological and optimal-analog representatives.

## Limitations

- **Different scales on each planet.** Mars profiles are computed at ~125 m/pixel over each site's region of interest, with a 625 m roughness window. Earth subpatches are computed at 463 m/pixel over ~5 km. Slope and roughness depend on scale, so the comparison is approximate and favours broad landforms over fine texture.
- Temperature is not physically comparable between planets, so it is used only in exploratory scenarios.
- Composition and mineralogy are not modelled: a morphological match does not imply a chemical one.
- Filtering removes vegetated, populated or ice-covered areas, including some known analogs.

## Future work

- Tune the clustering parameters and the Gower weights with a multi-objective NSGA-II search. A prototype exists in the methodology notebook but was not used for these results.
- Harmonise Mars and Earth to the same resolution and add multi-scale roughness.
- Add lunar sites (Artemis South Pole candidates) with LOLA DEMs.
- Add spectral and mineralogical layers (CRISM, ASTER).

See the technical report for details and validation.

## Reproduce

```bash
pip install numpy pandas scipy rasterio joblib requests earthaccess pyhdf hdbscan scikit-learn matplotlib seaborn

# 1. Earth catalog (downloads Copernicus DEM, WorldCover, GHS-POP and MODIS automatically)
export ANALOGS_ROOT="$(pwd)/Code/Data Extraction"
python "Code/Data Extraction/mars_analog_data_pipeline.py" \
  --steps relief cleaning fine integrate albedo lst dataset \
  --continents south_america europe africa oceania asia north_america

# 2. Ranking + clustering: unzip Dataset/Dataset_world_clean.zip into Dataset/,
#    then run all cells of Code/Methodology/Methodology_Craters.ipynb
#    (it reads ../../Dataset/Craters.csv and ../../Dataset/world_integrated_clean_albedo_lst.csv).
```

Licensed font files are not included in this repository; the website falls back to system fonts when run locally.

MODIS downloads require a free [NASA Earthdata Login](https://urs.earthdata.nasa.gov/). The full Earth extraction takes several hours on a laptop. Steps are cached and resumable.

All files use English column names. `translation_map.json` lists the original Spanish name of every renamed column, for anyone comparing with earlier versions.

## Data sources

**NASA:** MRO HiRISE and CTX DTMs, Mars Odyssey THEMIS (PDS), MGS MOLA, Viking MDIM 2.1 (USGS), Terra/Aqua MODIS MCD43C3 and MYD11C3 (LP DAAC).
**Partner and other:** ESA Mars Express HRSC DTMs, Copernicus DEM GLO-30 (ESA), ESA WorldCover 2021, GHS-POP R2023A (EC JRC).

## Use of AI

The website assistant uses Google Gemini 2.5 Flash and is constrained to the precomputed variables of each analog. Anthropic Claude was used as a coding and documentation assistant. All analyses were run and validated by the team.

## Team

Diego Gutierrez Rios · Rafael Moreno Garcia · Jose Samuel Perez Caballero · Jesus Enrique Barcenas Ventura

## License

Code is released under the [MIT License](LICENSE). Derived data (`Dataset/`, `Results/`) are released under [CC BY 4.0](DATA_LICENSE.md). Source datasets keep their original licenses and attributions, listed in `DATA_LICENSE.md`.
