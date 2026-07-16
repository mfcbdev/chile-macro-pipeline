-- Small static frequency lookup. Recreated (drop + insert) on every deploy —
-- content is fully determined by this file.

CREATE OR REPLACE TABLE `raw.dim_frequency`
(
    frequency     STRING NOT NULL OPTIONS(description = "daily | monthly | quarterly."),
    description   STRING          OPTIONS(description = "Publication cadence description."),
    schedule_cron STRING          OPTIONS(description = "Cloud Scheduler cron expression used for ingestion.")
);

INSERT INTO `raw.dim_frequency` (frequency, description, schedule_cron) VALUES
    ('daily',     'Series published on business days',          '0 9 * * 1-5'),
    ('monthly',   'Series published monthly',                    '0 10 5 * *'),
    ('quarterly', 'Series published quarterly (Jan/Apr/Jul/Oct)', '0 10 15 1,4,7,10 *');
