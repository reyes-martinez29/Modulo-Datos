"""
transactions/exceptions.py — Exception handler personalizado para DRF.

Por qué existe este archivo:
    Django REST Framework devuelve HTTP 400 Bad Request cuando un serializer
    falla la validación. Los ejercicios anteriores (E4) y el enunciado del E5
    esperan HTTP 422 Unprocessable Entity para errores de schema inválido —
    que es el código semánticamente correcto según RFC 9110 para datos que
    tienen el formato correcto pero violan las reglas de negocio.

    En lugar de cambiar manualmente el status_code en cada view, se registra
    este handler en settings.REST_FRAMEWORK['EXCEPTION_HANDLER'] y aplica
    la conversión de forma centralizada.

Configuración en settings.py:
    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'transactions.exceptions.custom_exception_handler',
    }
"""

from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    """
    Intercepta las respuestas de DRF y convierte 400 → 422.

    El handler estándar de DRF maneja la mayoría de los casos correctamente
    (404, 401, 403, 500). Solo cambiamos el 400 porque es el que DRF
    genera para errores de validación de serializer.

    Parámetros
    ----------
    exc     : la excepción lanzada por la view
    context : dict con 'view', 'args', 'kwargs', 'request'

    Retorna
    -------
    Response con status 422 si el handler estándar retornó 400,
    o el response original sin modificar para cualquier otro status.
    """
    response = exception_handler(exc, context)

    if response is not None and response.status_code == 400:
        response.status_code = 422

    return response