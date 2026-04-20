"""
lmr_feature_dict.py — v3
========================
Human-readable descriptions for all features used in the LMR v3 pipeline.
Covers all variables referenced in lmr_pipeline_script_v3_1.py.

Usage:
    from lmr_feature_dict import FEATURE_DESCRIPTIONS
    print(FEATURE_DESCRIPTIONS["ndvi_250m"])
    # → "NDVI 250m — Normalized Difference Vegetation Index from MODIS at 250m resolution"
"""

FEATURE_DESCRIPTIONS = {

    # ── Core vegetation indices ───────────────────────────────────────────────
    "ndvi_250m":            "NDVI 250m — Normalized Difference Vegetation Index from MODIS at 250m resolution",
    "evi_250m":             "EVI 250m — Enhanced Vegetation Index (soil-adjusted) from MODIS at 500m res, 250m name",
    "lai":                  "LAI — Leaf Area Index; total one-sided area of leaf tissue per unit ground surface area",
    "fpar":                 "FPAR — Fraction of Photosynthetically Active Radiation absorbed by vegetation canopy",
    "osavi":                "OSAVI — Optimized Soil Adjusted Vegetation Index (NIR-Red)/(NIR+Red+0.16)",
    "lswi":                 "LSWI — Land Surface Water Index; sensitive to both leaf and soil moisture (NIR-SWIR2)/(NIR+SWIR2)",
    "ndwi":                 "NDWI — Normalized Difference Water Index; vegetation water content (NIR-SWIR1)/(NIR+SWIR1)",
    "nbr":                  "NBR — Normalized Burn Ratio; fire damage and recovery indicator (NIR-SWIR2)/(NIR+SWIR2)",
    "bsi":                  "BSI — Bare Soil Index; land degradation indicator (Red+SWIR1)/(NIR+SWIR1)",
    "swir_ratio":           "SWIR ratio — SWIR1/SWIR2; discriminates soil type, crust, and moisture state",
    "s2_ndvi":              "Sentinel-2 NDVI — High-resolution (100m) vegetation index from Sentinel-2",
    "s2_ndwi":              "Sentinel-2 NDWI — High-resolution (100m) water content index from Sentinel-2",

    # ── SAR ──────────────────────────────────────────────────────────────────
    "s1_vv":                "SAR VV — Sentinel-1 vertical-transmit vertical-receive backscatter (dB)",
    "s1_vh":                "SAR VH — Sentinel-1 vertical-transmit horizontal-receive backscatter (dB)",
    "sar_vv_vh_ratio":      "SAR VV/VH ratio — VV_dB minus VH_dB; distinguishes dense canopy from bare surface",
    "sar_rvi":              "SAR RVI — Radar Vegetation Index 4×VH/(VV+VH); cloud-penetrating vegetation indicator",

    # ── Thermal / drought indices ─────────────────────────────────────────────
    "tci":                  "TCI — Temperature Condition Index (P98_LST - LST)/(P98_LST - P02_LST)×100; 0=hottest ever, 100=coolest",
    "vci":                  "VCI — Vegetation Condition Index (NDVI - P02_NDVI)/(P98_NDVI - P02_NDVI)×100; 0=worst, 100=best",
    "vhi":                  "VHI — Vegetation Health Index 0.5×VCI + 0.5×TCI; <40 drought, <20 severe, <10 extreme",
    "ndvi_anom":            "NDVI anomaly — NDVI minus long-term calendar-month mean (expanding window, no leakage)",
    "lst_anom":             "LST anomaly — Land Surface Temperature minus long-term calendar-month mean (expanding window)",
    "ppt_anomaly":          "Precipitation anomaly — CHIRPS rainfall minus long-term calendar-month mean (expanding window)",

    # ── GPP ───────────────────────────────────────────────────────────────────
    "gpp":                  "GPP — Gross Primary Productivity from MODIS MOD17A2HGF; monthly carbon fixed (kg C/m²/month)",
    "gpp_anomaly":          "GPP anomaly — GPP minus long-term calendar-month mean per household (expanding window)",
    "gpp_deficit":          "GPP deficit — max(0, GPP_climatology - GPP); monthly productivity shortfall",
    "gpp_fpar_decoupling":  "GPP-FPAR decoupling — gpp_anomaly minus fpar_anomaly; positive=more productive than greenness suggests; negative=stressed",

    # ── Light Use Efficiency ──────────────────────────────────────────────────
    "lue":                  "LUE — Light Use Efficiency GPP/FPAR; how efficiently vegetation converts absorbed light to carbon (kg C/m²/month per unit FPAR)",
    "lue_anomaly":          "LUE anomaly — LUE minus long-term calendar-month mean; water or heat stress signal invisible in NDVI alone",

    # ── Precipitation ─────────────────────────────────────────────────────────
    "ppt":                  "Precipitation — CHIRPS monthly rainfall total (mm/month); gold standard for East Africa at 5km",

    # ── Soil moisture ─────────────────────────────────────────────────────────
    "swvl1":                "Soil moisture layer 1 — ERA5-Land volumetric soil water 0–7cm depth (m³/m³)",
    "swvl2":                "Soil moisture layer 2 — ERA5-Land volumetric soil water 7–28cm depth (m³/m³)",
    "swvl3":                "Soil moisture layer 3 — ERA5-Land volumetric soil water 28–100cm depth (m³/m³)",
    "swvl4":                "Soil moisture layer 4 — ERA5-Land volumetric soil water 100–289cm depth (m³/m³)",
    "swvl1_anom":           "Soil moisture layer 1 anomaly — swvl1 minus long-term calendar-month mean (expanding window)",
    "swvl2_anom":           "Soil moisture layer 2 anomaly — swvl2 minus long-term calendar-month mean (expanding window)",
    "swvl3_anom":           "Soil moisture layer 3 anomaly — swvl3 minus long-term calendar-month mean (expanding window)",
    "swvl4_anom":           "Soil moisture layer 4 anomaly — swvl4 minus long-term calendar-month mean (expanding window)",
    "soil_composite":       "Soil moisture composite — 0.4×swvl1 + 0.35×swvl2 + 0.15×swvl3 + 0.10×swvl4; depth-weighted aggregate",
    "soil_shallow_deep":    "Soil shallow/deep ratio — swvl1/swvl4; surface vs deep storage contrast",
    "soil_composite_anom":  "Soil composite anomaly — composite minus long-term calendar-month mean (expanding window)",
    "soil_deficit":         "Soil moisture deficit — max(0, soil_climatology - soil_composite); monthly moisture shortfall",
    "soil_cum_deficit_3m":  "3-month cumulative soil moisture deficit — rolling 3-month sum of soil_deficit",
    "soil_cum_deficit_6m":  "6-month cumulative soil moisture deficit — rolling 6-month sum of soil_deficit",

    # ── Stress composites ─────────────────────────────────────────────────────
    "et_deficit":           "ET deficit — PET minus ET (>=0); annual evapotranspiration water stress",
    "et_fraction":          "ET fraction — ET/PET (0–1); 0=fully stressed, 1=unstressed",
    "et_deficit_roll3_mean":"ET deficit 3-month rolling mean — sustained water stress signal",
    "months_since_rain":    "Months since rain — months since precipitation exceeded 30mm; dry spell duration proxy (0–24)",
    "drought_mild":         "Drought mild flag — binary indicator of mild drought conditions (VHI-based)",
    "drought_severe":       "Drought severe flag — binary indicator of severe drought conditions (VHI-based)",
    "drought_extreme":      "Drought extreme flag — binary indicator of extreme drought conditions (VHI-based)",
    "compound_stress":      "Compound stress — combined vegetation + temperature + soil moisture stress indicator",
    "ppt_above_30":         "Precipitation above 30mm — binary flag; month received meaningful rainfall",

    # ── Phenology ─────────────────────────────────────────────────────────────
    "sos_month":            "Start of Season month — first month NDVI exceeds 20% of annual amplitude",
    "eos_month":            "End of Season month — last month NDVI exceeds 20% of annual amplitude",
    "season_length":        "Season length — EOS minus SOS plus 1 (months); longer=more reliable forage",
    "peak_ndvi":            "Peak NDVI — maximum NDVI value within the year",
    "green_months":         "Green months — count of months with NDVI above 10% of annual amplitude",
    "ndvi_amplitude":       "NDVI amplitude — max minus min NDVI in year; seasonal productivity range",
    "season_length_anom":   "Season length anomaly — season_length minus household long-term mean (expanding window)",

    # ── Long-term NDVI statistics (expanding window — no leakage) ────────────
    "ndvi_lt_mean_exp":              "NDVI long-term mean (expanding) — mean NDVI across all years up to current year; no future leakage",
    "ndvi_lt_std_exp":               "NDVI long-term std (expanding) — std of annual NDVI up to current year",
    "ndvi_lt_p10_exp":               "NDVI long-term P10 (expanding) — 10th percentile of NDVI up to current year",
    "ndvi_lt_p90_exp":               "NDVI long-term P90 (expanding) — 90th percentile of NDVI up to current year",
    "ndvi_lt_cv_exp":                "NDVI long-term CV (expanding) — coefficient of variation std/mean up to current year",
    "ndvi_mean_mam_exp":             "NDVI long rains mean (expanding) — mean NDVI in March-April-May up to current year",
    "ndvi_mean_ond_exp":             "NDVI short rains mean (expanding) — mean NDVI in Oct-Nov-Dec up to current year",
    "ndvi_mean_jfas_exp":            "NDVI dry season mean (expanding) — mean NDVI in Jan-Feb and Aug-Sep up to current year",
    "ndvi_drought_year_count_expanding": "NDVI drought year count (expanding) — count of years with annual NDVI below P20, computed up to current year",

    # ── Fire ─────────────────────────────────────────────────────────────────
    "fire_detected":        "Fire detected — binary flag; MODIS fire mask >= 7 detected in month",
    "fire_cumulative_count":"Fire cumulative count — running count of fire events to date",
    "fire_count_12m":       "Fire count 12-month — rolling 12-month fire frequency",
    "months_since_fire":    "Months since fire — months since most recent MODIS fire detection (dominant fire feature)",

    # ── Static / land cover ───────────────────────────────────────────────────
    "dem":                  "DEM — Copernicus GLO-30 digital elevation model; elevation in meters (245–2838m in Marsabit)",
    "dem_std":              "DEM std — elevation standard deviation in 20km window; proxy for terrain roughness",
    "dem_range":            "DEM range — elevation range in 20km window; topographic relief",
    "wc_trees":             "WorldCover trees — fraction of tree cover in 20km window (ESA WorldCover 10m)",
    "wc_shrubland":         "WorldCover shrubland — fraction of shrubland in 20km window",
    "wc_grassland":         "WorldCover grassland — fraction of grassland in 20km window",
    "wc_water":             "WorldCover water — fraction of water bodies in 20km window",
    "wc_builtup":           "WorldCover built-up — fraction of built-up/urban area in 20km window",
    "wc_cropland":          "WorldCover cropland — fraction of cropland in 20km window",
    "jrc_occurrence":       "JRC water occurrence — percentage of time water present 1984–2020 (JRC Global Surface Water 30m)",
    "jrc_seasonality":      "JRC water seasonality — months water present in reference year (JRC Global Surface Water 30m) [REMOVED in v3 — use jrc_occurrence]",

    # ── Lags ─────────────────────────────────────────────────────────────────
    "ndvi_250m_lag1":       "NDVI 1-month lag — NDVI value from 1 month prior",
    "ndvi_250m_lag2":       "NDVI 2-month lag — NDVI value from 2 months prior",
    "ndvi_250m_lag3":       "NDVI 3-month lag — NDVI value from 3 months prior",
    "evi_250m_lag1":        "EVI 1-month lag — EVI value from 1 month prior",
    "lst_day_lag1":         "LST day 1-month lag — daytime land surface temperature from 1 month prior (K)",
    "lst_day_lag2":         "LST day 2-month lag — daytime land surface temperature from 2 months prior (K)",
    "lst_day_lag3":         "LST day 3-month lag — daytime land surface temperature from 3 months prior (K)",
    "fpar_lag1":            "FPAR 1-month lag — FPAR value from 1 month prior",
    "fpar_lag2":            "FPAR 2-month lag — FPAR value from 2 months prior",
    "fpar_lag3":            "FPAR 3-month lag — FPAR value from 3 months prior",
    "ppt_lag1":             "Precipitation 1-month lag — rainfall from 1 month prior (mm/month)",
    "ppt_lag2":             "Precipitation 2-month lag — rainfall from 2 months prior (mm/month)",
    "ppt_lag3":             "Precipitation 3-month lag — rainfall from 3 months prior (mm/month)",
    "tci_lag1":             "TCI 1-month lag — Temperature Condition Index from 1 month prior",
    "tci_lag2":             "TCI 2-month lag — Temperature Condition Index from 2 months prior",
    "tci_lag3":             "TCI 3-month lag — Temperature Condition Index from 3 months prior",
    "vci_lag1":             "VCI 1-month lag — Vegetation Condition Index from 1 month prior",
    "vci_lag2":             "VCI 2-month lag — Vegetation Condition Index from 2 months prior",
    "vci_lag3":             "VCI 3-month lag — Vegetation Condition Index from 3 months prior",
    "vhi_lag1":             "VHI 1-month lag — Vegetation Health Index from 1 month prior",
    "vhi_lag2":             "VHI 2-month lag — Vegetation Health Index from 2 months prior",
    "vhi_lag3":             "VHI 3-month lag — Vegetation Health Index from 3 months prior",
    "gpp_lag1":             "GPP 1-month lag — Gross Primary Productivity from 1 month prior",
    "gpp_lag2":             "GPP 2-month lag — Gross Primary Productivity from 2 months prior",
    "gpp_lag3":             "GPP 3-month lag — Gross Primary Productivity from 3 months prior",

    # ── Rolling windows ───────────────────────────────────────────────────────
    "ndvi_250m_roll3_mean": "NDVI 3-month rolling mean — mean NDVI over past 3 months",
    "ndvi_250m_roll3_std":  "NDVI 3-month rolling std — variability of NDVI over past 3 months",
    "evi_250m_roll3_mean":  "EVI 3-month rolling mean — mean EVI over past 3 months",
    "lst_day_roll3_mean":   "LST 3-month rolling mean — mean daytime land surface temperature over past 3 months",
    "lst_day_roll3_std":    "LST 3-month rolling std — variability of daytime LST over past 3 months",
    "ppt_roll3_sum":        "Precipitation 3-month rolling sum — total rainfall over past 3 months (mm)",
    "ppt_roll3_mean":       "Precipitation 3-month rolling mean — mean monthly rainfall over past 3 months (mm/month)",
    "ppt_roll6_sum":        "Precipitation 6-month rolling sum — total rainfall over past 6 months (mm)",
    "ppt_roll12_sum":       "Precipitation 12-month rolling sum — total rainfall over past 12 months; annual proxy (mm)",
    "gpp_roll3_mean":       "GPP 3-month rolling mean — mean productivity over past 3 months",
    "gpp_roll6_mean":       "GPP 6-month rolling mean — mean productivity over past 6 months; seasonal trend",
    "tci_roll3_mean":       "TCI 3-month rolling mean — mean Temperature Condition Index over past 3 months",
    "vci_roll3_mean":       "VCI 3-month rolling mean — mean Vegetation Condition Index over past 3 months",
    "vhi_roll3_mean":       "VHI 3-month rolling mean — mean Vegetation Health Index over past 3 months",
    "swvl1_roll3":          "Soil layer 1 3-month rolling mean — mean shallow soil moisture over past 3 months",
    "swvl2_roll3":          "Soil layer 2 3-month rolling mean — mean mid-depth soil moisture over past 3 months",
    "sar_rvi_roll3_mean":   "SAR RVI 3-month rolling mean — mean radar vegetation index over past 3 months",

    # ── Year-on-year differences ──────────────────────────────────────────────
    "ndvi_250m_yoy_diff":   "NDVI year-on-year difference — NDVI[t] minus NDVI[t-12]; is this year greener than last?",
    "ndvi_250m_yoy_ratio":  "NDVI year-on-year ratio — NDVI[t] / NDVI[t-12]; proportional change vs same month last year",
    "evi_250m_yoy_diff":    "EVI year-on-year difference — EVI[t] minus EVI[t-12]",
    "ppt_yoy_diff":         "Precipitation year-on-year difference — rainfall[t] minus rainfall[t-12]; wetter or drier than last year?",
    "gpp_yoy_diff":         "GPP year-on-year difference — GPP[t] minus GPP[t-12]; productivity change vs same month last year",
    "lst_day_yoy_diff":     "LST year-on-year difference — LST[t] minus LST[t-12]; temperature change vs same month last year",

    # ── Season flag / ancillary ───────────────────────────────────────────────
    "season_flag":          "Season flag — binary; 1 if long rains season (March–September), 0 if short rains/dry",
    "is_lrld":              "Long rains / long dry flag — binary season indicator used in paper baseline phases",

    # ── Month dummies ─────────────────────────────────────────────────────────
    "m_2":  "Month dummy February — binary; 1 if observation month is February (January is reference)",
    "m_3":  "Month dummy March",
    "m_4":  "Month dummy April",
    "m_5":  "Month dummy May",
    "m_6":  "Month dummy June",
    "m_7":  "Month dummy July",
    "m_8":  "Month dummy August",
    "m_9":  "Month dummy September",
    "m_10": "Month dummy October",
    "m_11": "Month dummy November",
    "m_12": "Month dummy December",
}

"""
lmr_collections_dict.py
=======================
Planetary Computer (and related) data collections used in the
Marsabit LMR v3 feature engineering pipeline.

Each key is the collection/dataset identifier.
Each value is a dict with:
    title       — short human-readable name
    description — what it is, resolution, temporal cadence, and which pipeline features it produces
    source      — data provider
    resolution  — native spatial resolution
    cadence     — temporal cadence
    features    — list of pipeline variable names derived from this collection
"""

PC_COLLECTIONS = {

    "modis-vegetation-indices": {
        "name":       "modis-vegetation-indices",
        "enabled":    True,
        "collection": "modis-13Q1-061",
        "assets":     ["250m_16_days_NDVI", "250m_16_days_EVI"],
    },

    "modis-fpar": {
        "name":       "modis-fpar",
        "enabled":    True,
        "collection": "modis-15A2H-061",
        "assets":     ["Fpar_500m", "Lai_500m"],
    },

    "modis-surface-reflectance": {
        "name":       "modis-surface-reflectance",
        "enabled":    True,
        "collection": "modis-09A1-061",
        "assets":     ["sur_refl_b01", "sur_refl_b02", "sur_refl_b06", "sur_refl_b07"],
    },

    "modis-lst": {
        "name":       "modis-lst",
        "enabled":    True,
        "collection": "modis-11A2-061",
        "assets":     ["LST_Day_1km", "LST_Night_1km"],
    },

    "modis-et": {
        "name":       "modis-et",
        "enabled":    True,
        "collection": "modis-16A2GF-061",
        "assets":     ["ET_500m"],
    },

    "modis-gpp": {
        "name":       "modis-gpp",
        "enabled":    True,
        "collection": "modis-17A2HGF-061",
        "assets":     ["Gpp_500m"],
    },

    "modis-fire": {
        "name":       "modis-fire",
        "enabled":    True,
        "collection": "modis-14A1-061",
        "assets":     ["FireMask", "MaxFRP"],
    },

    "sentinel-1-sar": {
        "name":       "sentinel-1-sar",
        "enabled":    True,
        "collection": "sentinel-1-grd",
        "assets":     ["vv", "vh"],
    },

    "sentinel-2-optical": {
        "name":       "sentinel-2-optical",
        "enabled":    True,
        "collection": "sentinel-2-l2a",
        "assets":     ["B04", "B08", "B11"],
    },

    "cop-dem": {
        "name":       "cop-dem",
        "enabled":    True,
        "collection": "cop-dem-glo-30",
        "assets":     ["data"],
    },

    "esa-worldcover": {
        "name":       "esa-worldcover",
        "enabled":    True,
        "collection": "esa-worldcover",
        "assets":     ["map"],
    },

    "jrc-water": {
        "name":       "jrc-water",
        "enabled":    True,
        "collection": "jrc-gsw",
        "assets":     ["occurrence"],
    },


}



if __name__ == "__main__":
    print(f"Total collections: {len(PC_COLLECTIONS)}\n")
    for key, val in PC_COLLECTIONS.items():
        print(f"  {key}")
        print(f"    {val['title']}")
        print(f"    Resolution: {val['resolution']} | Cadence: {val['cadence']}")
        print(f"    Features: {len(val['features'])}")
        print()
    print("="*80)
    print(f"Total features documented: {len(FEATURE_DESCRIPTIONS)}")
    print(f"\nSample lookups:")
    for k in ["ndvi_250m", "ppt_roll12_sum", "ndvi_lt_cv_exp", "vhi", "swvl1"]:
        print(f"  {k:30s} → {FEATURE_DESCRIPTIONS[k]}")
