-- Long/tall consolidated view for Looker Studio.
--
-- One row per (series, observation). Includes series metadata via LEFT JOIN so the
-- dashboard can display Spanish names/units without a second join. Filtering, pivoting,
-- and metric selection happen in the BI layer.

CREATE OR REPLACE VIEW `analytics.v_dashboard_consolidado` AS
SELECT
    o.observation_date,
    o.frequency,
    o.series_id,
    COALESCE(s.name, o.series_id)  AS series_nombre,
    s.unit                          AS unidad,
    o.value,
    EXTRACT(YEAR FROM o.observation_date)  AS anio,
    EXTRACT(MONTH FROM o.observation_date) AS mes,
    EXTRACT(QUARTER FROM o.observation_date) AS trimestre
FROM `raw.observations` o
LEFT JOIN `raw.dim_series` s USING (series_id)
WHERE o.status_code = 'OK'
  AND o.value IS NOT NULL;
