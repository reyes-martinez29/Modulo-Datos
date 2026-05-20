"""
app/cache.py — Cache en memoria con TTL configurable por endpoint.

Decisión de diseño: cache en memoria con dict de Python, sin Redis ni
dependencias externas. El enunciado no exige persistencia entre reinicios
ni distribución entre múltiples instancias — un dict en memoria es la
solución más simple que cumple los requisitos.

Estructura interna de cada entrada del cache:
    {cache_key: CacheEntry(value, expires_at)}

Por qué no usamos functools.lru_cache o cachetools:
    - lru_cache no permite TTL por entrada
    - cachetools es una dependencia adicional innecesaria
    - Un dict con timestamps da control total sobre el comportamiento
      y es trivial de entender en un code review

El cache es un singleton: se instancia una vez en el módulo y se usa
desde todos los endpoints. FastAPI es single-process en desarrollo;
en producción con múltiples workers cada worker tendría su propio cache,
lo cual es aceptable para este caso de uso.

TTLs por defecto:
    /analytics/summary:      300s — los datos cambian solo con nuevos batches
    /analytics/top-merchants: 300s — misma razón
    /health:                  NO se cachea — debe reportar estado real

La cache key incluye todos los parámetros que afectan el resultado.
Para top-merchants: "top-merchants:limit=10:country=MX". Si no se incluye
el country, la key es "top-merchants:limit=10:country=None". Esto evita
que una query filtrada por país devuelva el resultado cacheado de la query
sin filtro, o viceversa.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Optional


# TTL por defecto para endpoints analíticos (segundos)
DEFAULT_TTL = 300


@dataclass
class CacheEntry:
    """Una entrada individual del cache con su valor y su tiempo de expiración."""
    value: Any
    expires_at: float  # timestamp Unix


@dataclass
class CacheStats:
    """Estadísticas del cache desde que arrancó el servidor."""
    hits:   int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        """
        Proporción de requests que encontraron dato en cache.
        Retorna 0.0 si no hubo ningún request todavía.
        """
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class TTLCache:
    """
    Cache en memoria con TTL configurable por clave.

    Uso típico desde un endpoint:

        result = cache.get("analytics:summary")
        if result is None:
            result = compute_expensive_query()
            cache.set("analytics:summary", result, ttl=300)
        return result

    El método get() retorna None tanto si la clave no existe como si expiró,
    de forma que el endpoint no necesita distinguir entre ambos casos.
    """

    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}
        self._stats = CacheStats()

    # ------------------------------------------------------------------
    # Operaciones principales
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        """
        Retorna el valor cacheado si existe y no expiró, o None.

        Registra hit o miss en las estadísticas. Los entries expirados
        se eliminan del dict en el momento de acceso (lazy eviction) para
        evitar un proceso de limpieza en background innecesario.
        """
        entry = self._store.get(key)

        if entry is None:
            self._stats.misses += 1
            return None

        if time.monotonic() > entry.expires_at:
            # Entry expirado — limpiarlo y reportar miss
            del self._store[key]
            self._stats.misses += 1
            return None

        self._stats.hits += 1
        return entry.value

    def set(self, key: str, value: Any, ttl: float = DEFAULT_TTL) -> None:
        """
        Guarda un valor en el cache con el TTL indicado.

        ttl es en segundos. Después de ese tiempo, get() retornará None
        para esta clave aunque el valor siga en el dict hasta el próximo
        acceso (lazy eviction).
        """
        self._store[key] = CacheEntry(
            value=value,
            expires_at=time.monotonic() + ttl,
        )

    def invalidate(self, key: str) -> None:
        """
        Elimina una entrada del cache manualmente.

        Útil después de un batch insert para que el próximo request a
        /analytics/* no sirva datos desactualizados.
        """
        self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """
        Elimina todas las entradas cuya clave empieza con prefix.

        Después de un batch insert conviene invalidar todos los endpoints
        analíticos: cache.invalidate_prefix("analytics:")
        """
        keys_to_delete = [k for k in self._store if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._store[k]

    # ------------------------------------------------------------------
    # Estadísticas — usadas por GET /health
    # ------------------------------------------------------------------

    @property
    def hit_rate(self) -> float:
        return self._stats.hit_rate

    @property
    def hits(self) -> int:
        return self._stats.hits

    @property
    def misses(self) -> int:
        return self._stats.misses

    # ------------------------------------------------------------------
    # Helpers de construcción de cache keys
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(*parts: Any) -> str:
        """
        Construye una cache key canónica a partir de partes.

        Ejemplos:
            make_key("analytics", "summary")
            → "analytics:summary"

            make_key("analytics", "top-merchants", "limit=10", "country=MX")
            → "analytics:top-merchants:limit=10:country=MX"

        Usar este método en lugar de f-strings ad-hoc garantiza que todas
        las keys siguen el mismo formato y son fáciles de invalidar por prefijo.
        """
        return ":".join(str(p) for p in parts)


# Instancia global — se crea una vez al importar el módulo.
# Todos los endpoints y el health endpoint acceden a este mismo objeto.
cache = TTLCache()