import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from seguridad import generar_hash, verificar_password

DB_PATH = Path(__file__).parent / "inventarios.db"
UNIDADES_BASE = ["unidad", "caja", "paquete", "docena", "par", "bolsa", "saco", "rollo", "botella", "frasco", "libra", "kilogramo", "gramo", "onza", "quintal", "litro", "galón", "metro"]


_creando = False


def conectar() -> sqlite3.Connection:
    """Abre la base de datos. Si el archivo está vacío o le faltan las tablas
    (se borró, se reemplazó o es nuevo), las crea automáticamente."""
    global _creando
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _creando and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='productos'").fetchone() is None:
        _creando = True
        try:
            init_db()
        finally:
            _creando = False
    return conn


def init_db() -> None:
    conn = conectar()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        rol TEXT NOT NULL,
        nombre_completo TEXT
    );

    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        codigo TEXT UNIQUE NOT NULL,
        nombre TEXT NOT NULL,
        unidad_medida TEXT DEFAULT 'unidad'
    );

    CREATE TABLE IF NOT EXISTS movimientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        tipo TEXT NOT NULL CHECK(tipo IN ('entrada','salida')),
        cantidad REAL NOT NULL,
        costo_unitario REAL,
        origen TEXT DEFAULT 'manual',
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );
    """)
    _migrar(conn)
    conn.commit()

    # Crea un usuario admin por defecto la primera vez que se levanta el sistema
    existe = conn.execute("SELECT COUNT(*) AS n FROM usuarios").fetchone()["n"]
    if existe == 0:
        conn.execute(
            "INSERT INTO usuarios (username, password_hash, rol, nombre_completo) VALUES (?, ?, ?, ?)",
            ("admin", generar_hash("admin123"), "superadmin", "Superadministrador"),
        )
        conn.commit()
    conn.close()


# ---------- Usuarios ----------

def obtener_usuario_por_username(username: str) -> Optional[sqlite3.Row]:
    conn = conectar()
    fila = conn.execute("SELECT * FROM usuarios WHERE username = ?", (username,)).fetchone()
    conn.close()
    return fila


def listar_usuarios() -> List[sqlite3.Row]:
    conn = conectar()
    filas = conn.execute(
        "SELECT id, username, rol, nombre_completo FROM usuarios ORDER BY username"
    ).fetchall()
    conn.close()
    return filas


def crear_usuario(username: str, password: str, rol: str, nombre_completo: str = "") -> None:
    conn = conectar()
    conn.execute(
        "INSERT INTO usuarios (username, password_hash, rol, nombre_completo) VALUES (?, ?, ?, ?)",
        (username, generar_hash(password), rol, nombre_completo),
    )
    conn.commit()
    conn.close()


# ---------- Productos ----------

def listar_productos() -> List[sqlite3.Row]:
    conn = conectar()
    filas = conn.execute("SELECT * FROM productos ORDER BY nombre").fetchall()
    conn.close()
    return filas


def obtener_producto(producto_id: int) -> Optional[sqlite3.Row]:
    conn = conectar()
    fila = conn.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    conn.close()
    return fila


def obtener_producto_por_codigo(codigo: str) -> Optional[sqlite3.Row]:
    conn = conectar()
    fila = conn.execute("SELECT * FROM productos WHERE codigo = ?", (codigo,)).fetchone()
    conn.close()
    return fila


def crear_producto(codigo: str, nombre: str, unidad_medida: str = "unidad") -> None:
    conn = conectar()
    conn.execute(
        "INSERT INTO productos (codigo, nombre, unidad_medida) VALUES (?, ?, ?)",
        (codigo, nombre, unidad_medida),
    )
    conn.commit()
    conn.close()


def obtener_o_crear_producto(
    codigo: str, nombre: Optional[str] = None, unidad_medida: str = "unidad"
) -> int:
    """Usado por el importador: si el código ya existe reutiliza el producto,
    si no existe lo crea. Así el mismo archivo puede traer varios productos."""
    producto = obtener_producto_por_codigo(codigo)
    if producto:
        return int(producto["id"])
    conn = conectar()
    cur = conn.execute(
        "INSERT INTO productos (codigo, nombre, unidad_medida) VALUES (?, ?, ?)",
        (codigo, nombre or codigo, unidad_medida),
    )
    conn.commit()
    nuevo_id = cur.lastrowid
    conn.close()
    return int(nuevo_id) if nuevo_id is not None else 0


# ---------- Movimientos ----------

def listar_movimientos(producto_id: int) -> List[sqlite3.Row]:
    conn = conectar()
    filas = conn.execute(
        "SELECT * FROM movimientos WHERE producto_id = ? AND COALESCE(anulado,0)=0 ORDER BY fecha, id",
        (producto_id,),
    ).fetchall()
    conn.close()
    return filas


def crear_movimiento(
    producto_id: int,
    fecha: str,
    tipo: str,
    cantidad: float,
    costo_unitario: Optional[float] = None,
    origen: str = "manual",
    usuario: str = "",
    documento: str = "",
    observaciones: str = "",
) -> None:
    conn = conectar()
    conn.execute(
        """INSERT INTO movimientos (producto_id, fecha, tipo, cantidad, costo_unitario, origen, usuario, documento, observaciones)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (producto_id, fecha, tipo, cantidad, costo_unitario, origen, usuario, documento, observaciones),
    )
    conn.commit()
    conn.close()
    completar_documentos()


def obtener_movimiento(movimiento_id: int) -> Optional[sqlite3.Row]:
    conn = conectar()
    fila = conn.execute("SELECT * FROM movimientos WHERE id = ?", (movimiento_id,)).fetchone()
    conn.close()
    return fila


def actualizar_movimiento(
    movimiento_id: int,
    fecha: str,
    tipo: str,
    cantidad: float,
    costo_unitario: Optional[float] = None,
) -> None:
    """Permite corregir un movimiento ya registrado (manual o importado), por
    ejemplo cuando el OCR/lectura del archivo interpretó mal un dato."""
    conn = conectar()
    conn.execute(
        """UPDATE movimientos SET fecha = ?, tipo = ?, cantidad = ?, costo_unitario = ?
           WHERE id = ?""",
        (fecha, tipo, cantidad, costo_unitario, movimiento_id),
    )
    conn.commit()
    conn.close()


def eliminar_movimiento(movimiento_id: int) -> None:
    conn = conectar()
    conn.execute("DELETE FROM movimientos WHERE id = ?", (movimiento_id,))
    conn.commit()
    conn.close()


def actualizar_producto(producto_id: int, codigo: str, nombre: str, unidad_medida: str) -> None:
    """Permite corregir los datos de un producto ya registrado (manual o
    creado automáticamente durante una importación)."""
    conn = conectar()
    conn.execute(
        "UPDATE productos SET codigo = ?, nombre = ?, unidad_medida = ? WHERE id = ?",
        (codigo, nombre, unidad_medida, producto_id),
    )
    conn.commit()
    conn.close()


def contar_productos() -> int:
    conn = conectar()
    n = conn.execute("SELECT COUNT(*) AS n FROM productos").fetchone()["n"]
    conn.close()
    return int(n)


def contar_movimientos() -> int:
    conn = conectar()
    n = conn.execute("SELECT COUNT(*) AS n FROM movimientos").fetchone()["n"]
    conn.close()
    return int(n)


def resumen_inventario(umbral_stock_bajo: float = 5.0) -> Dict[str, Any]:
    """Recorre todos los productos y sus movimientos (orden cronológico) para
    calcular, con promedio ponderado, la existencia y el valor actual de cada
    uno. Además clasifica cada producto en un 'estado' para el panel de
    alertas del dashboard:
      - 'negativo': se registraron más salidas que entradas (dato mal capturado)
      - 'agotado':  existencia en cero
      - 'bajo':     existencia positiva pero por debajo del umbral
      - 'ok':       existencia normal
    """
    conn = conectar()
    productos = conn.execute("SELECT p.*, c.nombre AS categoria, '' AS proveedor FROM productos p LEFT JOIN categorias c ON c.id=p.categoria_id ORDER BY p.nombre").fetchall()
    filas_resumen: List[Dict[str, Any]] = []
    valor_total = 0.0
    existencia_total = 0.0
    productos_sin_stock = 0
    productos_negativos = 0
    productos_stock_bajo = 0

    for p in productos:
        movimientos = conn.execute(
            "SELECT * FROM movimientos WHERE producto_id = ? AND COALESCE(anulado,0)=0 ORDER BY fecha, id", (p["id"],)
        ).fetchall()
        saldo_cantidad = 0.0
        saldo_valor = 0.0
        for m in movimientos:
            cantidad = m["cantidad"]
            if m["tipo"] == "entrada":
                costo_unitario = m["costo_unitario"] or 0.0
                saldo_cantidad += cantidad
                saldo_valor += cantidad * costo_unitario
            else:
                costo_unitario = (saldo_valor / saldo_cantidad) if saldo_cantidad > 0 else 0.0
                saldo_cantidad -= cantidad
                saldo_valor -= cantidad * costo_unitario

        saldo_cantidad = round(saldo_cantidad, 2)
        saldo_valor = round(saldo_valor, 2)

        minimo = p["stock_minimo"] if p["stock_minimo"] is not None else umbral_stock_bajo
        if saldo_cantidad < 0:
            estado = "negativo"
            productos_negativos += 1
            productos_sin_stock += 1
        elif saldo_cantidad == 0:
            estado = "agotado"
            productos_sin_stock += 1
        elif saldo_cantidad < minimo:
            estado = "bajo"
            productos_stock_bajo += 1
        else:
            estado = "ok"

        valor_total += saldo_valor
        existencia_total += saldo_cantidad
        filas_resumen.append({
            "id": p["id"],
            "codigo": p["codigo"],
            "nombre": p["nombre"],
            "unidad_medida": p["unidad_medida"],
            "existencia": saldo_cantidad,
            "valor": saldo_valor,
            "movimientos": len(movimientos),
            "estado": estado,
            "categoria": p["categoria"], "categoria_id": p["categoria_id"], "stock_minimo": minimo, "precio_venta": p["precio_venta"],
        })

    conn.close()
    filas_resumen.sort(key=lambda f: f["valor"], reverse=True)
    return {
        "productos": filas_resumen,
        "valor_total": round(valor_total, 2),
        "existencia_total": round(existencia_total, 2),
        "total_productos": len(filas_resumen),
        "productos_sin_stock": productos_sin_stock,
        "productos_negativos": productos_negativos,
        "productos_stock_bajo": productos_stock_bajo,
    }


def crear_movimientos_masivo(movimientos: List[Dict[str, Any]]) -> None:
    """movimientos: lista de dicts con producto_id, fecha, tipo, cantidad, costo_unitario.
    Se usa desde /importar para guardar en una sola pasada todo lo extraído del archivo,
    cumpliendo con no tener que volver a subir la información."""
    if not movimientos:
        return
    conn = conectar()
    conn.executemany(
        """INSERT INTO movimientos (producto_id, fecha, tipo, cantidad, costo_unitario, origen, usuario)
           VALUES (:producto_id, :fecha, :tipo, :cantidad, :costo_unitario, 'importado', :usuario)""",
        movimientos,
    )
    conn.commit()
    conn.close()
    completar_documentos()

# ---------- Migración, actividad y configuración ----------

def _migrar(conn: sqlite3.Connection) -> None:
    """Agrega columnas y tablas nuevas sin tocar los datos existentes."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(movimientos)")}
    for c in ("usuario", "documento", "observaciones"):
        if c not in cols:
            conn.execute(f"ALTER TABLE movimientos ADD COLUMN {c} TEXT DEFAULT ''")
    for tabla, nuevas in (("movimientos", (("anulado", "INTEGER DEFAULT 0"), ("motivo_anulacion", "TEXT DEFAULT ''"))),
                          ("productos", (("categoria_id", "INTEGER"), ("stock_minimo", "REAL"), ("precio_venta", "REAL"))),
                          ("usuarios", (("activo", "INTEGER DEFAULT 1"), ("ultimo_acceso", "TEXT")))):
        existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}
        for c, t in nuevas:
            if c not in existentes:
                conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {c} {t}")
    sqlu = conn.execute("SELECT sql FROM sqlite_master WHERE name='usuarios'").fetchone()
    if sqlu and "CHECK" in sqlu[0]:  # versiones anteriores limitaban los roles; ahora son editables
        conn.executescript("""ALTER TABLE usuarios RENAME TO usuarios_old;
        CREATE TABLE usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            rol TEXT NOT NULL, nombre_completo TEXT, activo INTEGER DEFAULT 1, ultimo_acceso TEXT);
        INSERT INTO usuarios (id, username, password_hash, rol, nombre_completo, activo, ultimo_acceso)
            SELECT id, username, password_hash, rol, nombre_completo, activo, ultimo_acceso FROM usuarios_old;
        DROP TABLE usuarios_old;""")
    conn.execute("UPDATE usuarios SET rol='superadmin' WHERE rol='admin'")
    conn.executescript("CREATE TABLE IF NOT EXISTS roles (nombre TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS permisos (rol TEXT, permiso TEXT, PRIMARY KEY (rol, permiso));")
    if conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0:
        conn.executemany("INSERT INTO roles VALUES (?)", [("operador",), ("consulta",)])
        conn.executemany("INSERT INTO permisos VALUES (?, ?)", [("operador", p) for p in ("movimientos", "productos", "importar", "exportar")] + [("consulta", "exportar")])
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS categorias (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS proveedores (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT UNIQUE NOT NULL, telefono TEXT, correo TEXT);
    CREATE TABLE IF NOT EXISTS importaciones (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, usuario TEXT, archivo TEXT, registros INTEGER, errores INTEGER);
    CREATE TABLE IF NOT EXISTS actividad (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL,
        usuario TEXT, accion TEXT, modulo TEXT, descripcion TEXT);
    CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT);
    """)

    # --- catálogo de unidades, historial de ediciones y rol "administrador" (se crea una sola vez) ---
    conn.executescript("CREATE TABLE IF NOT EXISTS unidades (nombre TEXT PRIMARY KEY COLLATE NOCASE); CREATE TABLE IF NOT EXISTS ediciones (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, usuario TEXT, accion TEXT, tabla TEXT, registro TEXT, detalle TEXT);")
    conn.executemany("INSERT OR IGNORE INTO unidades VALUES (?)", [(u,) for u in UNIDADES_BASE])
    conn.execute("INSERT OR IGNORE INTO unidades SELECT DISTINCT TRIM(unidad_medida) FROM productos WHERE unidad_medida IS NOT NULL AND TRIM(unidad_medida) <> ''")
    if not conn.execute("SELECT 1 FROM config WHERE clave='rol_administrador'").fetchone():
        conn.execute("INSERT OR IGNORE INTO roles VALUES ('administrador')")
        conn.executemany("INSERT OR IGNORE INTO permisos VALUES ('administrador', ?)", [(p,) for p in ("movimientos", "productos", "importar", "exportar", "empresa")])
        conn.execute("INSERT INTO config VALUES ('rol_administrador', '1')")
    conn.commit()


def registrar_actividad(usuario: str, accion: str, modulo: str, descripcion: str) -> None:
    conn = conectar()
    conn.execute("INSERT INTO actividad (fecha, usuario, accion, modulo, descripcion) VALUES (datetime('now','localtime'),?,?,?,?)",
                 (usuario, accion, modulo, descripcion))
    conn.commit()
    conn.close()


def listar_actividad(limite: int = 200) -> List[sqlite3.Row]:
    conn = conectar()
    filas = conn.execute("SELECT * FROM actividad ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
    conn.close()
    return filas


def obtener_config(clave: str, defecto: str = "") -> str:
    conn = conectar()
    fila = conn.execute("SELECT valor FROM config WHERE clave = ?", (clave,)).fetchone()
    conn.close()
    return fila["valor"] if fila else defecto


def guardar_config(clave: str, valor: str) -> None:
    conn = conectar()
    conn.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES (?, ?)", (clave, valor))
    conn.commit()
    conn.close()


def sql(consulta: str, params: Any = (), uno: bool = False, escribir: bool = False) -> Any:
    """Atajo para consultas puntuales: devuelve filas (o una fila) o el último id insertado."""
    conn = conectar()
    cur = conn.execute(consulta, params)
    if escribir:
        conn.commit()
        r = cur.lastrowid
    else:
        r = cur.fetchone() if uno else cur.fetchall()
    conn.close()
    return r


def completar_documentos() -> None:
    """Da número de documento a los movimientos que no lo tienen: COMP-0001 a las compras (entradas) y VEN-0001 a las ventas (salidas).
    Respeta los documentos escritos por el usuario y continúa la numeración después del mayor que exista."""
    conn = conectar()
    for tipo, pref in (("entrada", "COMP"), ("salida", "VEN")):
        sin = conn.execute("SELECT id FROM movimientos WHERE tipo=? AND (documento IS NULL OR TRIM(documento)='') ORDER BY fecha, id", (tipo,)).fetchall()
        if not sin:
            continue
        usados = [int(m.group(1)) for (x,) in conn.execute("SELECT documento FROM movimientos WHERE documento LIKE ?", (pref + "-%",)).fetchall() if (m := re.fullmatch(pref + r"-(\d+)", x))]
        n = max(usados, default=0)
        for r in sin:
            n += 1
            conn.execute("UPDATE movimientos SET documento=? WHERE id=?", (f"{pref}-{n:04d}", r["id"]))
    conn.commit()
    conn.close()


def siguiente_documento(pref: str) -> str:
    """Siguiente número libre para un tipo de documento: AJ-0001, INI-0001, COMP-0001..."""
    conn = conectar()
    usados = [int(m.group(1)) for (x,) in conn.execute("SELECT documento FROM movimientos WHERE documento LIKE ?", (pref + "-%",)).fetchall() if (m := re.fullmatch(pref + r"-(\d+)", x))]
    conn.close()
    return f"{pref}-{max(usados, default=0) + 1:04d}"


def registrar_edicion(usuario: str, accion: str, tabla: str, registro: str, detalle: str) -> None:
    conn = conectar()
    conn.execute("INSERT INTO ediciones (fecha, usuario, accion, tabla, registro, detalle) VALUES (datetime('now','localtime'),?,?,?,?,?)", (usuario, accion, tabla, registro, detalle))
    conn.commit()
    conn.close()
