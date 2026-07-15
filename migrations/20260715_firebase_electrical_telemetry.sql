-- Telemetría eléctrica calculada por el ESP32 y publicada en Firebase bajo:
-- /Atmos/registro/{pabellon}/{aire}/lecturas/{firebase_key}
ALTER TABLE registros
    ADD COLUMN IF NOT EXISTS corriente_rms double precision,
    ADD COLUMN IF NOT EXISTS voltaje_red_v double precision,
    ADD COLUMN IF NOT EXISTS factor_potencia double precision,
    ADD COLUMN IF NOT EXISTS potencia_aparente_va double precision,
    ADD COLUMN IF NOT EXISTS potencia_activa_w double precision,
    ADD COLUMN IF NOT EXISTS potencia_activa_kw double precision,
    ADD COLUMN IF NOT EXISTS consumo_intervalo_kwh double precision,
    ADD COLUMN IF NOT EXISTS consumo_acumulado_sesion_kwh double precision,
    ADD COLUMN IF NOT EXISTS tarifa_kwh double precision,
    ADD COLUMN IF NOT EXISTS costo_intervalo double precision,
    ADD COLUMN IF NOT EXISTS costo_acumulado_sesion double precision;

COMMENT ON COLUMN registros.potencia_w IS
    'Campo compatible del dashboard; replica potencia_activa_w cuando Firebase la entrega.';
COMMENT ON COLUMN registros.energia_kwh IS
    'Acumulado monotono del backend construido sumando consumo_intervalo_kwh entre sesiones.';
