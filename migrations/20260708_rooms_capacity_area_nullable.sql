ALTER TABLE rooms
ALTER COLUMN capacidad DROP NOT NULL,
ALTER COLUMN area_m2 DROP NOT NULL;

COMMENT ON COLUMN rooms.capacidad IS
'Campo legado opcional. Ya no se captura en el formulario de espacios.';

COMMENT ON COLUMN rooms.area_m2 IS
'Campo legado opcional. Ya no se captura en el formulario de espacios.';
