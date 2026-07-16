-- Real GDP (PIB) with quarter-over-quarter and year-over-year growth.

CREATE OR REPLACE VIEW `analytics.v_pib_trimestral` AS
WITH base AS (
    SELECT
        observation_date,
        value AS pib_mmm_clp_encadenados
    FROM `raw.observations`
    WHERE series_id = 'F032.PIB.FLU.R.CLP.EP18.Z.Z.0.T'
      AND status_code = 'OK'
      AND value IS NOT NULL
)
SELECT
    observation_date,
    EXTRACT(YEAR FROM observation_date) AS anio,
    EXTRACT(QUARTER FROM observation_date) AS trimestre,
    pib_mmm_clp_encadenados,
    ROUND(
        SAFE_DIVIDE(
            pib_mmm_clp_encadenados - LAG(pib_mmm_clp_encadenados) OVER (ORDER BY observation_date),
            LAG(pib_mmm_clp_encadenados) OVER (ORDER BY observation_date)
        ) * 100,
        2
    ) AS variacion_trimestral_pct,
    ROUND(
        SAFE_DIVIDE(
            pib_mmm_clp_encadenados - LAG(pib_mmm_clp_encadenados, 4) OVER (ORDER BY observation_date),
            LAG(pib_mmm_clp_encadenados, 4) OVER (ORDER BY observation_date)
        ) * 100,
        2
    ) AS variacion_12m_pct
FROM base;
