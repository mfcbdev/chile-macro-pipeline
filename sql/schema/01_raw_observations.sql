-- Raw BDE observations. One row per (series_id, observation_date).
-- Loaded via MERGE from a staging table by functions/transform_load.

CREATE TABLE IF NOT EXISTS `raw.observations`
(
    series_id        STRING    NOT NULL OPTIONS(description = "BDE timeseries code (e.g. F073.TCO.PRE.Z.D)."),
    observation_date DATE      NOT NULL OPTIONS(description = "Calendar date of the observation (no timezone)."),
    value            FLOAT64            OPTIONS(description = "Observed value; NULL when BDE returned an unparseable value."),
    status_code      STRING             OPTIONS(description = "BDE per-observation status code, e.g. 'OK'."),
    frequency        STRING             OPTIONS(description = "daily | monthly | quarterly."),
    ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
                                        OPTIONS(description = "UTC timestamp of the load into BigQuery.")
)
PARTITION BY observation_date
CLUSTER BY series_id
OPTIONS(
    description = "Raw BDE observations, one row per (series_id, observation_date). MERGE-upserted by functions/transform_load.",
    require_partition_filter = FALSE
);
