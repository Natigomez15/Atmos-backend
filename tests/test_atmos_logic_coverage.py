import pytest

from app.ml import atmos_logic


def test_validacion_cubre_limites_y_errores_de_dominio():
    assert atmos_logic.validar_lectura(1, 10, 5, 0)["valido"] is True
    assert atmos_logic.validar_lectura(1, 45, 35, 100)["valido"] is True

    resultado = atmos_logic.validar_lectura(2, 46, 4, 101, minutos_sin_presencia=-1)

    assert resultado["valido"] is False
    assert len(resultado["errores"]) == 5


@pytest.mark.parametrize(
    ("campo", "argumentos"),
    [
        ("presencia", (None, 25, 20, 50, 0)),
        ("temperatura ambiente", (1, None, 20, 50, 0)),
        ("temperatura del ac", (1, 25, None, 50, 0)),
        ("humedad", (1, 25, 20, None, 0)),
        ("minutos sin presencia", (1, 25, 20, 50, None)),
    ],
)
def test_validacion_rechaza_nulos_sin_lanzar_excepciones(campo, argumentos):
    resultado = atmos_logic.validar_lectura(*argumentos)

    assert resultado["valido"] is False
    assert any(campo in error.lower() for error in resultado["errores"])


@pytest.mark.parametrize("valor_invalido", ["abc", [], {}, True, False])
def test_validacion_rechaza_tipos_no_numericos_incluidos_booleanos(valor_invalido):
    resultado = atmos_logic.validar_lectura(1, valor_invalido, 20, 50)

    assert resultado["valido"] is False
    assert any("temperatura ambiente" in error.lower() for error in resultado["errores"])


def test_validacion_regresion_temperatura_ambiente_nula_no_lanza_typeerror():
    resultado = atmos_logic.validar_lectura(1, None, 20, 50)

    assert resultado["valido"] is False
    assert any("temperatura ambiente" in error.lower() for error in resultado["errores"])


def test_validacion_rechaza_numeros_no_finitos():
    for valor_invalido in [float("nan"), float("inf"), float("-inf")]:
        resultado = atmos_logic.validar_lectura(1, valor_invalido, 20, 50)

        assert resultado["valido"] is False
        assert any("temperatura ambiente" in error.lower() for error in resultado["errores"])


def test_validacion_reporta_varios_campos_invalidos_en_una_sola_respuesta():
    resultado = atmos_logic.validar_lectura(True, "25", [], {}, minutos_sin_presencia=False)

    assert resultado["valido"] is False
    assert len(resultado["errores"]) == 5


def test_validacion_acepta_enteros_y_flotantes_validos():
    assert atmos_logic.validar_lectura(1.0, 24.5, 18.2, 55.5, 0.0)["valido"] is True
