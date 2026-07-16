-- Monthly CPI (IPC) variation plus a properly compounded 12-month accumulated variation.
--
-- The BDE series is already a % change (variación mensual), so annual inflation is:
--   ((1 + v1/100) * (1 + v2/100) * ... * (1 + v12/100)) - 1  (times 100 for %)
--
-- We compute the product via SUM(LN(...))/EXP() over a 12-row window.

CREATE OR REPLACE VIEW `analytics.v_ipc_mensual` AS
WITH base AS (
    SELECT
        observation_date,
        value AS variacion_mensual_pct
    FROM `raw.observations`
    WHERE series_id = 'F074.IPC.VAR.Z.Z.C.M'
      AND status_code = 'OK'
      AND value IS NOT NULL
)
SELECT
    observation_date,
    variacion_mensual_pct,
    ROUND(
        (
            EXP(
                SUM(LN(1 + variacion_mensual_pct / 100)) OVER (
                    ORDER BY observation_date
                    ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
                )
            ) - 1
        ) * 100,
        4
    ) AS variacion_12m_pct,
    -- Row counter to let downstream filter out incomplete 12-month windows.
    COUNT(*) OVER (
        ORDER BY observation_date
        ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ) AS meses_en_ventana
FROM base;
