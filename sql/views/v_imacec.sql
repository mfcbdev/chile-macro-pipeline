-- IMACEC monthly index with year-over-year growth (LAG 12 months).

CREATE OR REPLACE VIEW `analytics.v_imacec` AS
WITH base AS (
    SELECT
        observation_date,
        value AS imacec_indice
    FROM `raw.observations`
    WHERE series_id = 'F032.IMC.IND.Z.Z.EP18.Z.Z.0.M'
      AND status_code = 'OK'
      AND value IS NOT NULL
)
SELECT
    observation_date,
    imacec_indice,
    ROUND(
        SAFE_DIVIDE(
            imacec_indice - LAG(imacec_indice, 12) OVER (ORDER BY observation_date),
            LAG(imacec_indice, 12) OVER (ORDER BY observation_date)
        ) * 100,
        2
    ) AS variacion_12m_pct,
    -- Month-over-month change for high-frequency context.
    ROUND(
        SAFE_DIVIDE(
            imacec_indice - LAG(imacec_indice) OVER (ORDER BY observation_date),
            LAG(imacec_indice) OVER (ORDER BY observation_date)
        ) * 100,
        2
    ) AS variacion_mensual_pct
FROM base;
