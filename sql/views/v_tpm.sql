-- Monetary Policy Rate (TPM) with change-point detection (basis points delta from previous day).

CREATE OR REPLACE VIEW `analytics.v_tpm` AS
WITH base AS (
    SELECT
        observation_date,
        value AS tpm_pct
    FROM `raw.observations`
    WHERE series_id = 'F022.TPM.TIN.D001.NO.Z.D'
      AND status_code = 'OK'
      AND value IS NOT NULL
)
SELECT
    observation_date,
    tpm_pct,
    -- Difference in percentage points; nonzero rows correspond to Consejo policy meetings.
    tpm_pct - LAG(tpm_pct) OVER (ORDER BY observation_date) AS cambio_pp,
    CASE
        WHEN LAG(tpm_pct) OVER (ORDER BY observation_date) IS NULL THEN NULL
        WHEN tpm_pct > LAG(tpm_pct) OVER (ORDER BY observation_date) THEN 'alza'
        WHEN tpm_pct < LAG(tpm_pct) OVER (ORDER BY observation_date) THEN 'baja'
        ELSE 'sin_cambio'
    END AS movimiento
FROM base;
