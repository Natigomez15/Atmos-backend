from unittest.mock import MagicMock

from app.services import notificaciones_service as servicio


def test_push_rechaza_configuracion_y_claves_faltantes(monkeypatch):
    monkeypatch.setattr(servicio.configuracion, "vapid_clave_privada", "")
    assert servicio.enviar_notificacion_push_detallada({}, "t", "c")["motivo"] == "vapid_privada_no_configurada"

    monkeypatch.setattr(servicio.configuracion, "vapid_clave_privada", "clave")
    monkeypatch.setattr(servicio.configuracion, "vapid_correo", "admin@example.test")
    resultado = servicio.enviar_notificacion_push_detallada({"endpoint": "x"}, "t", "c")
    assert resultado["motivo"] == "suscripcion_sin_claves"


def test_push_exitoso_no_usa_red_real(monkeypatch):
    cliente = MagicMock()
    for metodo in ("table", "update", "eq"):
        getattr(cliente, metodo).return_value = cliente
    monkeypatch.setattr(servicio, "obtener_cliente", lambda: cliente)
    monkeypatch.setattr(servicio.configuracion, "vapid_clave_privada", "clave")
    monkeypatch.setattr(servicio.configuracion, "vapid_correo", "admin@example.test")
    enviado = {}
    monkeypatch.setattr(servicio, "webpush", lambda **kwargs: enviado.update(kwargs))

    resultado = servicio.enviar_notificacion_push_detallada(
        {"endpoint": "https://push.test", "p256dh": "p", "auth": "a"},
        "Titulo", "Cuerpo", {"tipo_alerta": "power_anomaly", "sala_id": "s1"},
    )
    assert resultado == {"enviada": True, "motivo": "enviada"}
    assert enviado["subscription_info"]["endpoint"] == "https://push.test"


def test_notificar_alerta_filtra_roles_y_horarios(monkeypatch):
    cliente = MagicMock()
    for metodo in ("table", "select", "eq"):
        getattr(cliente, metodo).return_value = cliente
    cliente.execute.return_value = MagicMock(data=[
        {"rol": "admin"}, {"rol": "visitante"}, {"rol": "mantenimiento"},
    ])
    monkeypatch.setattr(servicio, "obtener_cliente", lambda: cliente)
    monkeypatch.setattr(servicio, "esta_en_horario_usuario", lambda s: s.get("rol") != "mantenimiento")
    monkeypatch.setattr(servicio, "enviar_notificacion_push", lambda *args: True)

    resultado = servicio.notificar_alerta_push("power_anomaly", "high", "msg", "s1", "Sala")
    assert resultado == {"enviadas": 1, "fuera_de_horario": 1, "fallidas": 0, "total_suscriptores": 3}
