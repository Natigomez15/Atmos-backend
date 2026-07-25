# Protocolo seguro pendiente para el control IR ESP32

El firmware IR no se encuentra en este repositorio. Antes de habilitar el control real, el ESP32 debe implementar este contrato de forma fail-closed.

## Sobre de comando obligatorio

El lector solo puede considerar un comando con todos estos campos:

- `command_id`: UUID no vacío.
- `created_at`: fecha UTC ISO-8601.
- `expires_at`: fecha UTC ISO-8601 posterior a `created_at`.
- `estado`: `pendiente`.
- `pabellon`: debe coincidir exactamente con el dispositivo.
- `aire`: debe coincidir exactamente con el dispositivo.
- `accion`: una acción IR explícita permitida.

Los comandos legados sin `command_id` o `expires_at` se rechazan. `mantener` y `no_op` se rechazan y nunca se traducen a un código IR.

## Algoritmo antes de transmitir

1. Confirmar que el control IR local está habilitado explícitamente.
2. Validar todos los campos, el destino y el estado del comando.
3. Obtener la hora mediante una fuente sincronizada. Si la hora no es confiable, no ejecutar.
4. Rechazar si `ahora >= expires_at`, si `created_at` está en el futuro o si el TTL es inválido.
5. Leer `last_executed_command_id` de almacenamiento persistente NVS (`Preferences`).
6. Rechazar si coincide con `command_id`.
7. Traducir únicamente la acción explícita a su código IR. No inferir un código desde una recomendación.
8. Transmitir una sola vez.
9. Persistir atómicamente `command_id` en NVS inmediatamente después del intento de transmisión y antes de volver al ciclo de lectura.
10. Reportar separadamente `enviado_sin_confirmacion`, `confirmado` o `fallido`. Una transmisión IR no confirma que el aire cambió de estado.

## Reconexión y reinicio

Al arrancar o recuperar Wi-Fi/Firebase, el firmware no debe ejecutar el valor existente automáticamente. Primero debe aplicar todas las validaciones anteriores. La persistencia NVS evita repetir el último comando tras reinicios; el vencimiento evita ejecutar comandos más antiguos que el último identificador recordado.

## Estado físico

El firmware debe reportar por separado el estado de software y las mediciones eléctricas. No debe utilizar `aire_encendido_atmos` como prueba física. Hasta calibrar umbrales con mediciones reales, debe publicar:

- `estado_electrico = "no_confirmado"`
- `compresor_confirmado = false`

Una lectura aislada de 64.5 W no confirma que el equipo ni el compresor estén encendidos.
