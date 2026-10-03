# Inventario Flash

Sistema web de inventarios con valuación **PEPS**, **UEPS** y **promedio ponderado**. Proyecto universitario de contabilidad, hecho con Python y Flask.

## Qué hace

- **Entradas y salidas** con cantidad, precio y total. Cada movimiento lleva un documento (`COMP-0001` para compras, `VEN-0001` para ventas, o el que escribas).
- **Kardex** con los tres métodos. En PEPS y UEPS, una salida que cruza varios lotes se muestra como una fila por lote.
- **Estado de resultados** que compara los tres métodos lado a lado.
- **Reportes con gráficas** (barras, línea y dona) y exportación a Excel, PDF, CSV y TXT.
- **Importación sin plantilla** de Excel, CSV, Word, PDF, fotos y texto libre (por ejemplo: "El 5 de septiembre se vendieron 20 unidades de cuaderno"), siempre con vista previa antes de guardar.
- **Usuarios, roles y permisos**, historial de ediciones y alertas de stock, con sugerencia de reposición y aviso de productos de baja rotación.
- **Base de datos** con filtros y tabla dinámica (solo superadmin).
- Cinco temas de apariencia.

## Los tres métodos (ejemplo)

Compras de 100 u a Q5 y 50 u a Q5.50, y una venta de 120 u:

| Método | Costo de ventas | Inventario final (30 u) |
|---|---|---|
| PEPS | Q610 | Q165 |
| UEPS | Q625 | Q150 |
| Promedio ponderado | Q620 | Q155 |

La mercadería disponible (Q775) es la misma; el método solo decide cómo se reparte entre costo de ventas e inventario final.

## Requisitos

- Python 3.10 o superior.
- Opcional, solo para leer fotos y PDF escaneados: [Tesseract-OCR](https://github.com/UB-Mannheim/tesseract/wiki) (con el idioma español) y Poppler.

## Cómo correrlo (Windows, PowerShell)

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abre <http://127.0.0.1:5000>. La primera vez se crea sola la base de datos `inventarios.db`.

**Usuario inicial:** `admin` / `admin123`. Cámbiala cuanto antes en *Mi cuenta* (tocando tu nombre en la barra superior).

En Linux o Mac, activa el entorno con `source venv/bin/activate`.

## Configuración

- `PEPS_SECRET`: clave para firmar las sesiones. Define una propia si lo vas a usar en serio.

## Probar la importación

La carpeta [`ejemplos/`](ejemplos) trae un archivo de cada tipo (Excel, CSV, TXT, Word, PDF y fotos) para probar el importador desde *Importar*.

## Estructura

| Archivo | Para qué sirve |
|---|---|
| `app.py` | Rutas y lógica del sistema |
| `metodos_valuacion.py` | PEPS, UEPS y promedio ponderado |
| `importador.py` | Lectura de archivos y texto libre |
| `exportador.py` | Exportación a Excel, PDF, CSV y TXT |
| `database.py` | Acceso a SQLite y migraciones |
| `seguridad.py` | Contraseñas (PBKDF2-SHA256) |
| `templates/` y `static/` | Pantallas (HTML) y estilos (CSS) |

## Nota

Esto es un proyecto académico y corre con el servidor de desarrollo de Flask. Para un uso real habría que cambiar la clave secreta, desactivar el modo debug y agregar protección contra falsificación de formularios (CSRF).

## Autores

_(escribe aquí los nombres del equipo)_
