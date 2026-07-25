-- Fase de seguridad IR: este archivo NO ejecuta comandos ni toca Firebase.
-- Debe aplicarse manualmente en Supabase antes de activar el control físico.

alter table if exists public.ac_commands
    add column if not exists command_id uuid default gen_random_uuid(),
    add column if not exists created_at timestamptz default now(),
    add column if not exists expires_at timestamptz,
    add column if not exists estado text default 'pendiente',
    add column if not exists pabellon text,
    add column if not exists aire text,
    add column if not exists accion text,
    add column if not exists estado_deseado text;

create unique index if not exists ac_commands_command_id_uidx
    on public.ac_commands(command_id)
    where command_id is not null;

create index if not exists ac_commands_pendientes_vigentes_idx
    on public.ac_commands(pabellon, aire, estado, expires_at)
    where estado = 'pendiente';

alter table if exists public.registros
    add column if not exists estado_deseado text,
    add column if not exists ultimo_comando_enviado text,
    add column if not exists estado_reportado_por_software text,
    add column if not exists estado_electrico_observado text,
    add column if not exists compresor_confirmado boolean default false;

comment on column public.ac_commands.expires_at is
    'Obligatorio para ejecución. Los comandos legados sin vencimiento no se entregan al ESP32.';
comment on column public.registros.aire_encendido_atmos is
    'Estado legado reportado por software; no constituye confirmación física.';
