ALTER TABLE rooms
ADD COLUMN IF NOT EXISTS tipo text NOT NULL DEFAULT 'laboratorio',
ADD COLUMN IF NOT EXISTS activo boolean NOT NULL DEFAULT true;

ALTER TABLE rooms
DROP CONSTRAINT IF EXISTS rooms_tipo_check;

ALTER TABLE rooms
ADD CONSTRAINT rooms_tipo_check
CHECK (tipo IN ('laboratorio', 'oficina', 'salon'));

CREATE INDEX IF NOT EXISTS idx_rooms_activo ON rooms (activo);

COMMENT ON COLUMN rooms.tipo IS
  'Tipo de espacio gestionado por ATMOS: laboratorio, oficina o salon.';

COMMENT ON COLUMN rooms.activo IS
  'Soft-delete para ocultar espacios del dashboard sin borrar historial asociado.';
