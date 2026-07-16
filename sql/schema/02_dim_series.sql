-- Series metadata. Seeded from config/series.yaml via sql/seed/dim_series.sql.

CREATE TABLE IF NOT EXISTS `raw.dim_series`
(
    series_id   STRING    NOT NULL OPTIONS(description = "BDE timeseries code; primary key."),
    name        STRING    NOT NULL OPTIONS(description = "Human-readable name (Spanish)."),
    unit        STRING             OPTIONS(description = "Measurement unit."),
    frequency   STRING    NOT NULL OPTIONS(description = "daily | monthly | quarterly."),
    description STRING             OPTIONS(description = "Longer description."),
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY series_id
OPTIONS(description = "Series metadata dimension. Seeded from config/series.yaml.");
