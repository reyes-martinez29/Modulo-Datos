-- =============================================================================
-- schema.sql — Schema de SQLite para el sistema de transacciones financieras
-- Ejercicio 3: La Capa Transaccional
-- =============================================================================
--
-- Este schema está diseñado para 5 patrones de acceso específicos con SLAs
-- concretos. Cada decisión de tipo de dato e índice está documentada en
-- schema_design.md. Los comentarios aquí explican el QUÉ — schema_design.md
-- explica el POR QUÉ técnico de cada elección.
--
-- Uso:
--     sqlite3 data/transactions.db < schema.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Tabla principal de transacciones
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions (

    -- IDENTIFICADOR ÚNICO
    -- Declarado como PRIMARY KEY: SQLite crea automáticamente un índice B-Tree
    -- único sobre esta columna. Sin PRIMARY KEY explícito, SQLite usa el rowid
    -- interno como clave — perderíamos el acceso directo por transaction_id.
    -- Tipo TEXT porque los UUIDs son strings de 36 caracteres.
    transaction_id  TEXT        NOT NULL,

    -- TIMESTAMP
    -- Almacenado como TEXT en formato ISO8601 ('YYYY-MM-DD HH:MM:SS').
    -- SQLite no tiene tipo DATETIME nativo. TEXT ISO8601 permite comparaciones
    -- de rango con <, >, BETWEEN porque el orden lexicográfico coincide con
    -- el orden cronológico cuando el formato es consistente.
    -- Alternativa descartada: INTEGER (epoch Unix). Sería igual de rápido
    -- para range scans pero requeriría conversiones en cada query y haría
    -- las queries menos legibles y más propensas a errores de zona horaria.
    timestamp       TEXT        NOT NULL,

    -- ENTEROS NUMÉRICOS
    -- INTEGER es el tipo nativo de SQLite — sin conversión ni overhead.
    -- user_id entre 1-50000 y merchant_id entre 1-10000 caben en 2 bytes
    -- (SMALLINT) pero SQLite no distingue tamaños de INTEGER internamente,
    -- usa el mínimo necesario por valor almacenado (1-8 bytes variable).
    user_id         INTEGER     NOT NULL,
    merchant_id     INTEGER     NOT NULL,

    -- AMOUNT
    -- REAL es el tipo de punto flotante de SQLite (8 bytes, IEEE 754 double).
    -- Alternativa considerada: INTEGER en centavos (amount * 100).
    -- Se descartó porque requeriría conversión en cada lectura/escritura
    -- y el enunciado especifica float. Para un sistema financiero real se
    -- usaría NUMERIC o centavos enteros, pero aquí priorizamos fidelidad
    -- con el schema del módulo.
    amount          REAL        NOT NULL,

    -- COLUMNAS DE TEXTO CON BAJA CARDINALIDAD
    -- category:     10 valores posibles
    -- country_code: 15 valores posibles
    -- status:       3 valores posibles (completed, failed, pending)
    -- SQLite no tiene tipo ENUM. TEXT con CHECK constraint sería lo ideal
    -- para integridad, pero agrega overhead en cada INSERT. Para ingesta
    -- masiva de 1M filas, omitimos CHECK y confiamos en que los datos
    -- vienen del pipeline validado del Ejercicio 1.
    category        TEXT        NOT NULL,
    country_code    TEXT        NOT NULL,
    status          TEXT        NOT NULL,

    -- PRIMARY KEY sobre transaction_id
    -- Esto crea el índice B-Tree para P1 (lookup exacto, SLA < 10ms).
    -- Sin WITHOUT ROWID: SQLite mantiene el rowid interno además del PK.
    -- Consideramos WITHOUT ROWID para eliminar la indirección PK → rowid,
    -- pero WITHOUT ROWID requiere que el PK sea la clave de clustering, lo
    -- que perjudica las inserciones ordenadas por timestamp (fragmentación).
    -- Con rowid normal, SQLite inserta siempre al final del heap — óptimo
    -- para ingesta secuencial de datos generados en orden temporal.
    PRIMARY KEY (transaction_id)
);


-- -----------------------------------------------------------------------------
-- Índice compuesto para P2, P3 y P4 — acceso por usuario y tiempo
-- -----------------------------------------------------------------------------
--
-- Este es el índice más importante del schema. Sirve para tres patrones:
--
--   P2: Últimas 20 transacciones de un user_id ordenadas por timestamp
--       Query: WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20
--       SQLite usa el índice para encontrar el rango del usuario y recorre
--       las entradas en orden inverso sin sort adicional.
--
--   P3: Transacciones de un user_id en un rango de fechas
--       Query: WHERE user_id = ? AND timestamp BETWEEN ? AND ?
--       SQLite hace range scan dentro del sub-árbol del usuario.
--
--   P4: Suma de amount de un user_id en el último mes
--       Query: WHERE user_id = ? AND timestamp >= ?
--       Igual que P3 pero sin cota superior. SQLite escanea desde el inicio
--       del rango hasta el final del sub-árbol del usuario.
--
-- Por qué un solo índice sirve para los tres:
-- En un índice compuesto (A, B), SQLite puede usar el prefijo A solo para
-- filtrar por user_id, y el par (A, B) para filtrar por usuario + rango de
-- timestamp. Los tres patrones tienen user_id como primer predicado, así
-- que todos se benefician del mismo índice sin duplicación.
--
-- Por qué DESC en timestamp:
-- P2 pide ORDER BY timestamp DESC (las más recientes primero). Con DESC en
-- el índice, SQLite puede servir P2 recorriendo el índice en orden natural
-- (forward scan) sin necesidad de Sort. Sin DESC, tendría que recorrer en
-- orden inverso (backward scan) o hacer un Sort — ambos más lentos.
-- P3 y P4 no se ven afectados negativamente por el DESC porque para range
-- scans SQLite puede recorrer en cualquier dirección.

CREATE INDEX IF NOT EXISTS idx_user_timestamp
    ON transactions (user_id, timestamp DESC);


-- -----------------------------------------------------------------------------
-- Índice para P5 — usuarios de un país con más de N transacciones
-- -----------------------------------------------------------------------------
--
--   P5: Todos los user_id de un country_code con más de N transacciones
--       Query: WHERE country_code = ? GROUP BY user_id HAVING COUNT(*) > N
--
-- Sin este índice: SQLite haría full scan de 1M filas, filtraría por país
-- en memoria, y luego agruparía. Imposible en < 200ms.
--
-- Con este índice: SQLite navega directamente al rango del country_code en
-- el B-Tree, escanea solo las filas de ese país (~67k filas para 15 países
-- uniformes), y agrupa por user_id sobre ese subconjunto.
--
-- Por qué (country_code, user_id) y no solo (country_code):
-- Con solo country_code, SQLite encuentra las filas del país pero aún
-- necesita agrupar por user_id con un hash/sort sobre ~67k filas.
-- Con (country_code, user_id), las filas ya vienen agrupadas por usuario
-- dentro del país — SQLite puede hacer el COUNT con un simple scan
-- contando cambios de user_id, sin sort ni hash table adicional.

CREATE INDEX IF NOT EXISTS idx_country_user
    ON transactions (country_code, user_id);


-- -----------------------------------------------------------------------------
-- Vista de verificación (opcional, útil para desarrollo)
-- -----------------------------------------------------------------------------
--
-- Permite verificar rápidamente que la ingesta fue correcta sin leer
-- la tabla completa.

CREATE VIEW IF NOT EXISTS v_ingestion_summary AS
SELECT
    COUNT(*)                        AS total_rows,
    COUNT(DISTINCT user_id)         AS unique_users,
    COUNT(DISTINCT merchant_id)     AS unique_merchants,
    COUNT(DISTINCT country_code)    AS unique_countries,
    MIN(timestamp)                  AS earliest_ts,
    MAX(timestamp)                  AS latest_ts,
    ROUND(SUM(amount), 2)           AS total_amount,
    ROUND(AVG(amount), 4)           AS avg_amount,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
    SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END) AS failed_count,
    SUM(CASE WHEN status = 'pending'   THEN 1 ELSE 0 END) AS pending_count
FROM transactions;