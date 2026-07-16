-- Create the two datasets used by the pipeline.
-- Run once (or on every deploy — CREATE SCHEMA IF NOT EXISTS is idempotent).
-- Location should be set consistently at the dataset level; the deploy script passes
-- `--location=us-central1` (or whichever region the raw bucket lives in).

CREATE SCHEMA IF NOT EXISTS `raw`
    OPTIONS(description = "Raw ingested BDE observations + dimensions.");

CREATE SCHEMA IF NOT EXISTS `analytics`
    OPTIONS(description = "Curated views for BI / Looker Studio.");
