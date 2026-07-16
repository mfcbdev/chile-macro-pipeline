-- USD/CLP observed exchange rate with daily change, % change, and 7-day moving average.

CREATE OR REPLACE VIEW `analytics.v_tipo_cambio` AS
WITH base AS (
    SELECT
        observation_date,
        value AS clp_per_usd
    FROM `raw.observations`
    WHERE series_id = 'F073.TCO.PRE.Z.D'
      AND status_code = 'OK'
      AND value IS NOT NULL
)
SELECT
    observation_date,
    clp_per_usd,
    clp_per_usd - LAG(clp_per_usd) OVER w AS cambio_diario_clp,
    ROUND(
        SAFE_DIVIDE(clp_per_usd - LAG(clp_per_usd) OVER w, LAG(clp_per_usd) OVER w) * 100,
        4
    ) AS variacion_pct,
    AVG(clp_per_usd) OVER (
        ORDER BY observation_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS media_movil_7d
FROM base
WINDOW w AS (ORDER BY observation_date);
