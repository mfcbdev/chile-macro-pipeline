-- Daily UF (Unidad de Fomento) with day-over-day change and % variation.

CREATE OR REPLACE VIEW `analytics.v_uf_diaria` AS
WITH base AS (
    SELECT
        observation_date,
        value AS uf_clp
    FROM `raw.observations`
    WHERE series_id = 'F073.UFF.PRE.Z.D'
      AND status_code = 'OK'
      AND value IS NOT NULL
)
SELECT
    observation_date,
    uf_clp,
    uf_clp - LAG(uf_clp) OVER w AS cambio_diario_clp,
    ROUND(
        SAFE_DIVIDE(uf_clp - LAG(uf_clp) OVER w, LAG(uf_clp) OVER w) * 100,
        6
    ) AS variacion_pct
FROM base
WINDOW w AS (ORDER BY observation_date);
