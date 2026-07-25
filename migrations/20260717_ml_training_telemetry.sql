-- Variables de calidad y diagnóstico publicadas por el ESP32 en Firebase.
-- Complementan la telemetría eléctrica usada para preparar datasets de ML.
ALTER TABLE registros
    ADD COLUMN IF NOT EXISTS corriente_rms_cruda double precision,
    ADD COLUMN IF NOT EXISTS corriente_rms_instantanea double precision,
    ADD COLUMN IF NOT EXISTS corriente_calculada_vpp double precision,
    ADD COLUMN IF NOT EXISTS factor_calibracion_sct double precision,
    ADD COLUMN IF NOT EXISTS corriente_retenida_por_filtro boolean,
    ADD COLUMN IF NOT EXISTS ceros_consecutivos_sct integer,
    ADD COLUMN IF NOT EXISTS dht_ok boolean,
    ADD COLUMN IF NOT EXISTS ds18b20_ok boolean,
    ADD COLUMN IF NOT EXISTS fallos_dht integer,
    ADD COLUMN IF NOT EXISTS fallos_ds18b20 integer;

COMMENT ON COLUMN registros.dht_ok IS
    'Indica si la lectura ambiental puede utilizarse para entrenamiento.';
COMMENT ON COLUMN registros.ds18b20_ok IS
    'Indica si la temperatura de salida puede utilizarse para entrenamiento.';
COMMENT ON COLUMN registros.corriente_retenida_por_filtro IS
    'Marca corriente retenida por filtrado; permite excluir mediciones no instantáneas.';
