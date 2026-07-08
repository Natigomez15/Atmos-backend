ALTER TABLE registros
ADD COLUMN IF NOT EXISTS ultima_accion_ir text,
ADD COLUMN IF NOT EXISTS ciclo_enfriamiento_temp_inicio double precision,
ADD COLUMN IF NOT EXISTS ciclo_enfriamiento_inicio timestamptz;

COMMENT ON COLUMN registros.ultima_accion_ir IS
  'Ultimo comando IR real autorizado/enviado por ATMOS: TEMP_22, TEMP_23, TEMP_24, APAGAR o NINGUNO.';

COMMENT ON COLUMN registros.ciclo_enfriamiento_temp_inicio IS
  'Temperatura ambiente al iniciar un ciclo enfriar_fuerte.';

COMMENT ON COLUMN registros.ciclo_enfriamiento_inicio IS
  'Timestamp en que inicio el ciclo enfriar_fuerte para calcular minutos_enfriando.';
