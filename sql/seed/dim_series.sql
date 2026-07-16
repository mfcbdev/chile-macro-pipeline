-- Seed / refresh raw.dim_series from the canonical list in config/series.yaml.
-- Idempotent: MERGE updates existing rows and inserts missing ones.
-- Regenerate this file whenever config/series.yaml changes (see scripts/seed_dim_series.py).

MERGE INTO `raw.dim_series` T
USING (
    SELECT * FROM UNNEST([
        STRUCT(
            'F073.TCO.PRE.Z.D' AS series_id,
            'Dólar observado' AS name,
            'CLP/USD' AS unit,
            'daily' AS frequency,
            'Tipo de cambio observado publicado por el Banco Central de Chile.' AS description
        ),
        STRUCT(
            'F073.UFF.PRE.Z.D',
            'Unidad de Fomento (UF)',
            'CLP',
            'daily',
            'Valor diario de la Unidad de Fomento.'
        ),
        STRUCT(
            'F022.TPM.TIN.D001.NO.Z.D',
            'Tasa de política monetaria',
            'porcentaje',
            'daily',
            'TPM nominal diaria.'
        ),
        STRUCT(
            'F074.IPC.VAR.Z.Z.C.M',
            'IPC variación mensual',
            'porcentaje',
            'monthly',
            'Variación mensual del Índice de Precios al Consumidor.'
        ),
        STRUCT(
            'F032.IMC.IND.Z.Z.EP18.Z.Z.0.M',
            'IMACEC',
            'índice',
            'monthly',
            'Imacec empalmado, serie original (índice 2018=100).'
        ),
        STRUCT(
            'F032.PIB.FLU.R.CLP.EP18.Z.Z.0.T',
            'PIB real',
            'millones CLP encadenados',
            'quarterly',
            'Producto Interno Bruto real, series de referencia encadenadas.'
        )
    ])
) S
ON T.series_id = S.series_id
WHEN MATCHED THEN UPDATE SET
    name        = S.name,
    unit        = S.unit,
    frequency   = S.frequency,
    description = S.description,
    updated_at  = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (series_id, name, unit, frequency, description, updated_at)
    VALUES (S.series_id, S.name, S.unit, S.frequency, S.description, CURRENT_TIMESTAMP());
