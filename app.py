"""Inventario Flash: inventario con PEPS, UEPS y promedio ponderado."""
import json
import math
import re
import os
import sqlite3
import tempfile
from datetime import date, datetime
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from markupsafe import Markup, escape

import database as db
import exportador
import importador
import metodos_valuacion as mv
from seguridad import generar_hash, verificar_password

app = Flask(__name__)
app.secret_key = os.environ.get("PEPS_SECRET", "cambia-esta-clave")  # define PEPS_SECRET en producción
METODOS = {"peps": mv.calcular_peps, "ueps": mv.calcular_ueps, "promedio": mv.calcular_promedio_ponderado}
NOMBRES = {"peps": "PEPS", "ueps": "UEPS", "promedio": "Promedio ponderado"}
PERMISOS = {"movimientos": "Entradas y salidas", "productos": "Productos", "importar": "Importar", "exportar": "Exportar", "empresa": "Empresa y apariencia"}
ESTADO = {"ok": ("Disponible", "ok"), "bajo": ("Stock bajo", "low"), "agotado": ("Agotado", "out"), "negativo": ("Negativo", "out")}
TEMAS = {"claro": "Azul marino (claro)", "oscuro": "Oscuro", "beige": "Beige", "rosita": "Rosita (cristal)", "aqua": "Aqua y rosita (cristal)"}
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
TABLAS = ("productos", "movimientos", "actividad", "ediciones")
REPORTES = {"inventario": "Inventario valorizado", "entradas": "Entradas", "salidas": "Salidas", "mas_vendidos": "Productos más vendidos", "estado": "Estado de resultados"}
TIPOS = {"barras": "Barras", "linea": "Línea", "dona": "Dona"}
ORDEN = ("promedio", "peps", "ueps")
CAMPOS = [("codigo", "Código"), ("nombre", "Producto"), ("fecha", "Fecha"), ("tipo", "Tipo"), ("cantidad", "Cantidad"), ("costo", "Precio")]
db.init_db()
db.completar_documentos()
print(f" * Base de datos en uso: {db.DB_PATH}")
cfg = db.obtener_config


def hoy():
    return date.today().isoformat()


def resumen():
    return db.resumen_inventario(float(cfg("stock_minimo", "5") or 5))


def log(accion, modulo, desc=""):
    db.registrar_actividad(session.get("user", {}).get("username", "-"), accion, modulo, desc)


def puede(p):
    u = session.get("user")
    return bool(u) and (u["rol"] == "superadmin" or db.sql("SELECT 1 FROM permisos WHERE rol=? AND permiso=?", (u["rol"], p), uno=True) is not None)


def acceso(perm=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = session.get("user") and db.obtener_usuario_por_username(session["user"]["username"])
            if not u or u["activo"] == 0:
                session.clear()
                return redirect(url_for("login"))
            session["user"]["rol"] = u["rol"]
            session.modified = True
            if (perm == "superadmin" and u["rol"] != "superadmin") or (perm not in (None, "superadmin") and not puede(perm)):
                flash("No tienes permiso para esa sección.", "error")
                return redirect(url_for("inicio"))
            return f(*a, **k)
        return w
    return deco


def metodo_actual():
    m = request.args.get("metodo")
    if m in METODOS:
        session["metodo"] = m
    return session.get("metodo", "peps")


@app.context_processor
def _ctx():
    d = {"u": session.get("user"), "puede": puede, "NOMBRES": NOMBRES, "ESTADO": ESTADO, "EMPRESA": cfg("empresa", "Inventario Flash"),
         "M": cfg("moneda", "Q"), "tema": cfg("tema", "claro"), "metodo": session.get("metodo", "peps")}
    if "user" in session:
        on = {k: cfg("notif_" + k, "1") == "1" for k in ("bajo", "agotado", "negativo")}
        d["notifs"] = [p for p in resumen()["productos"] if p["estado"] != "ok" and on[p["estado"]]]
    d["pagina"] = PAGINAS.get(request.endpoint, "")
    return d


# ---------- Cálculos con el método elegido ----------

def calcular_inventario(m):
    r = resumen()["productos"]
    for p in r:
        f = METODOS[m](db.listar_movimientos(p["id"]))
        p["valor"] = f[-1]["saldo_valor"] if f else 0.0
        p["precio"] = round(p["valor"] / p["existencia"], 2) if p["existencia"] > 0 else 0.0
    return r


def movimientos_costeados(m, tipo=None, ini="", fin="", pid=None):
    """Movimientos con cantidad, precio y total. El precio sale del inventario con el método elegido; en PEPS/UEPS
    una salida que cruza varios lotes aparece como una fila por lote ('primera' marca la fila principal)."""
    out = []
    for p in db.listar_productos():
        if pid and p["id"] != pid:
            continue
        ms, vistos = db.listar_movimientos(p["id"]), set()
        for f in METODOS[m](ms):
            mo = ms[f["idx"]]
            if (tipo and mo["tipo"] != tipo) or (ini and mo["fecha"] < ini) or (fin and mo["fecha"] > fin):
                continue
            out.append({"id": mo["id"], "fecha": mo["fecha"], "codigo": p["codigo"], "producto": p["nombre"], "pid": p["id"], "tipo": mo["tipo"],
                        "cantidad": f["cantidad"], "precio": f["costo_unitario"], "total": f["costo_total"], "documento": mo["documento"] or "",
                        "obs": mo["observaciones"] or "", "usuario": mo["usuario"] or "", "primera": f["idx"] not in vistos, "cant_mov": mo["cantidad"]})
            vistos.add(f["idx"])
    return sorted(out, key=lambda x: (x["fecha"], x["id"]))


def filas_kardex(pid, m, ini, fin):
    ms, out, vistos = db.listar_movimientos(pid), [], set()
    for f in METODOS[m](ms):
        mo = ms[f["idx"]]
        cont = f["idx"] in vistos
        vistos.add(f["idx"])
        if (ini and mo["fecha"] < ini) or (fin and mo["fecha"] > fin):
            continue
        blk = (f["cantidad"], f["costo_unitario"], f["costo_total"])
        sc, sv = f["saldo_cantidad"], f["saldo_valor"]
        out.append({"fecha": mo["fecha"], "doc": mo["documento"] or "", "cont": cont, "e": blk if mo["tipo"] == "entrada" else None,
                    "s": blk if mo["tipo"] == "salida" else None, "v": (sc, round(sv / sc, 2) if sc else 0.0, sv)})
    return out


def kardex_tabla(filas):
    enc = ["Fecha", "Documento", "Entrada cant.", "Entrada precio", "Entrada total", "Salida cant.", "Salida precio", "Salida total", "Saldo cant.", "Saldo precio", "Saldo total"]
    return enc, [[f["fecha"], f["doc"]] + list(f["e"] or ("", "", "")) + list(f["s"] or ("", "", "")) + list(f["v"]) for f in filas]


def existencia(pid):
    f = mv.calcular_promedio_ponderado(db.listar_movimientos(pid))
    return f[-1]["saldo_cantidad"] if f else 0.0


# ---------- Gráficas (SVG propio: funcionan sin internet) ----------

def svg_barras(etq, series, ancho=780, alto=310):
    if not etq:
        return Markup("")
    mx = max([v for _, _, vs in series for v in vs] + [1])
    T = 30
    n, L, alt = len(etq), 64, alto - 96
    gw = (ancho - L - 18) / n
    bw = max(min(gw * 0.66 / len(series), 46), 4)
    paso, rotulos = max(1, -(-n // 12)), n * len(series) <= 16
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;max-width:{ancho}px;height:auto;display:block" font-family="sans-serif" font-size="12">']
    for i in range(5):
        y = T + alt * i / 4
        s.append(f'<line x1="{L}" x2="{ancho - 12}" y1="{y:.1f}" y2="{y:.1f}" class="gr"/><text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end" class="ax">{mx * (4 - i) / 4:,.0f}</text>')
    s.append(f'<path d="M{L} {T + alt}V{T - 12}M{L - 4} {T - 6}L{L} {T - 14}L{L + 4} {T - 6}M{L} {T + alt}H{ancho - 6}" class="axl" fill="none"/>')
    for i, e in enumerate(etq):
        for j, (nombre, c, vs) in enumerate(series):
            h = alt * max(vs[i], 0) / mx
            x, y = L + gw * i + (gw - bw * len(series)) / 2 + j * bw, T + alt - h
            suf = (" · " + escape(nombre)) if nombre else ""
            s.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 2:.1f}" height="{h:.1f}" rx="4" style="fill:{c}"><title>{escape(str(e))}{suf}: {vs[i]:,.2f}</title></rect>')
            if rotulos and vs[i] > 0:
                s.append(f'<text x="{x + (bw - 2) / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" class="vl" font-weight="700">{vs[i]:,.0f}</text>')
        if i % paso == 0:
            s.append(f'<text transform="translate({L + gw * i + gw / 2:.1f},{T + alt + 14}) rotate(-28)" text-anchor="end" class="ax">{escape(str(e)[:11])}</text>')
    for j, (nm, c) in enumerate([(nm, c) for nm, c, _ in series if nm]):
        s.append(f'<rect x="{L + j * 160}" y="{alto - 15}" width="11" height="11" rx="3" style="fill:{c}"/><text x="{L + 17 + j * 160}" y="{alto - 5}" class="vl">{escape(nm)}</text>')
    return Markup("".join(s) + "</svg>")


def svg_lineas(etq, series, ancho=780, alto=310):
    """Líneas con rejilla, ejes con flecha y marcadores de anillo. series: [(nombre, color, valores, destacada)]."""
    if not etq or not series:
        return Markup("")
    todos = [v for _, _, vs, _ in series for v in vs]
    mn, mx = min(todos + [0]), max(todos + [0])
    if mx == mn:
        mx = mn + 1
    L, R, T, B, n = 62, 34, 34, 34, len(etq)
    paso = max(1, -(-n // 9))
    px = lambda i: L + (ancho - L - R) * (i / (n - 1) if n > 1 else 0.5)
    py = lambda v: T + (alto - T - B) * (1 - (v - mn) / (mx - mn))
    lab = lambda e: (str(e)[5:] if len(str(e)) == 10 and str(e)[4] == "-" else str(e)[:9])
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;max-width:{ancho}px;height:auto;display:block" font-family="sans-serif" font-size="12">']
    for i in range(5):
        y = T + (alto - T - B) * i / 4
        s.append(f'<line x1="{L}" x2="{ancho - R}" y1="{y:.1f}" y2="{y:.1f}" class="gr"/><text x="{L - 8}" y="{y + 3:.1f}" text-anchor="end" class="ax">{mx - (mx - mn) * i / 4:,.0f}</text>')
    for i in range(0, n, paso):
        s.append(f'<line x1="{px(i):.1f}" x2="{px(i):.1f}" y1="{T}" y2="{alto - B}" class="gr"/><text x="{px(i):.1f}" y="{alto - B + 16}" text-anchor="middle" class="ax">{escape(lab(etq[i]))}</text>')
    s.append(f'<path d="M{L} {alto - B}V{T - 6}M{L - 4} {T}L{L} {T - 8}L{L + 4} {T}M{L} {alto - B}H{ancho - R + 8}" class="axl" fill="none"/>')
    for nombre, color, vs, dest in sorted(series, key=lambda x: x[3]):
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vs))
        s.append(f'<polyline points="{pts}" fill="none" style="stroke:{color}" stroke-width="{3.4 if dest else 2.4}" stroke-linejoin="round" stroke-linecap="round"/>')
        for i, v in enumerate(vs):
            s.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="{6 if dest else 4.6}" class="ring" style="stroke:{color}" stroke-width="3"><title>{escape(nombre)} · {escape(str(etq[i]))}: {v:,.2f}</title></circle>')
            if dest and n <= 8:
                s.append(f'<text x="{px(i):.1f}" y="{py(v) - 12:.1f}" text-anchor="middle" font-weight="700" font-size="13" style="fill:{color}">{v:,.0f}</text>')
    return Markup("".join(s) + "</svg>")


def svg_linea(etq, vals, color, ancho=780, alto=310):
    return svg_lineas(etq, [("", color, vals, True)], ancho, alto)


def svg_puntos(pts, xt, yt, xlab=None, color="var(--azul)", ancho=780, alto=330):
    """Gráfica de puntos. pts: [(x, y, detalle, tamaño de 0 a 1)]. El detalle sale al pasar el mouse."""
    if not pts:
        return Markup("")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    rx, ry = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    x0, x1, y0, y1 = min(xs) - rx * 0.06, max(xs) + rx * 0.06, min(ys) - ry * 0.08, max(ys) + ry * 0.08
    L, R, T, B = 62, 26, 26, 48
    px = lambda v: L + (ancho - L - R) * (v - x0) / (x1 - x0)
    py = lambda v: T + (alto - T - B) * (1 - (v - y0) / (y1 - y0))
    f = xlab or (lambda v: f"{v:,.0f}")
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;max-width:{ancho}px" font-family="sans-serif" font-size="12">']
    for i in range(5):
        y, x = T + (alto - T - B) * i / 4, L + (ancho - L - R) * i / 4
        s.append(f'<line x1="{L}" x2="{ancho - R}" y1="{y:.1f}" y2="{y:.1f}" class="gr"/><text x="{L - 5}" y="{y + 3:.1f}" text-anchor="end" class="ax">{y1 - (y1 - y0) * i / 4:,.0f}</text>')
        s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{alto - B}" class="gr"/><text x="{x:.1f}" y="{alto - B + 14}" text-anchor="middle" class="ax">{escape(f(x0 + (x1 - x0) * i / 4))}</text>')
    s.append(f'<text x="{(L + ancho - R) / 2:.0f}" y="{alto - 8}" text-anchor="middle" class="ax">{escape(xt)}</text><text transform="translate(12,{(T + alto - B) / 2:.0f}) rotate(-90)" text-anchor="middle" class="ax">{escape(yt)}</text>')
    s.append(f'<path d="M{L} {alto - B}V{T - 6}M{L - 4} {T}L{L} {T - 8}L{L + 4} {T}M{L} {alto - B}H{ancho - R + 8}" class="axl" fill="none"/>')
    for x, y, det, t in pts:
        s.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="{4 + 9 * t:.1f}" style="fill:{color};fill-opacity:.55;stroke:{color}" stroke-width="1.5"><title>{escape(det)}</title></circle>')
    return Markup("".join(s) + "</svg>")


def svg_dona(etq, vals, ancho=780, alto=310, hueco=70):
    tot = sum(v for v in vals if v > 0)
    if not etq or tot <= 0:
        return Markup("")
    pares = sorted(((e, v) for e, v in zip(etq, vals) if v > 0), key=lambda p: -p[1])
    if len(pares) > 7:
        pares = pares[:7] + [("Otros", sum(v for _, v in pares[7:]))]
    cols = ["#4FD1C5", "#FFB84D", "#FF7A90", "#8ecbff", "#B79CFF", "#7ff0cf", "#ffd27a", "#c9d1d9"]
    cx, cy, R, r, ang = 220, alto / 2, 120, hueco, -math.pi / 2
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;max-width:{ancho}px" font-family="sans-serif" font-size="12.5">']
    for i, (n, v) in enumerate(pares):
        fr, c = v / tot, cols[i % len(cols)]
        a2 = ang + 2 * math.pi * fr
        if fr >= 0.9999:
            s.append(f'<circle cx="{cx}" cy="{cy}" r="{(R + r) / 2}" fill="none" style="stroke:{c}" stroke-width="{R - r}"><title>{escape(n)}: 100%</title></circle>')
        else:
            p = lambda rad, an: f"{cx + rad * math.cos(an):.1f} {cy + rad * math.sin(an):.1f}"
            big = 1 if fr > 0.5 else 0
            s.append(f'<path d="M {p(R, ang)} A {R} {R} 0 {big} 1 {p(R, a2)} L {p(r, a2)} A {r} {r} 0 {big} 0 {p(r, ang)} Z" style="fill:{c}"><title>{escape(n)}: {v:,.2f} ({fr * 100:.1f}%)</title></path>')
        s.append(f'<rect x="430" y="{22 + i * 28}" width="11" height="11" rx="2" style="fill:{c}"/><text x="449" y="{33 + i * 28}" class="vl">{escape(str(n)[:22])} · {v:,.0f} · {fr * 100:.1f}%</text>')
        ang = a2
    if hueco:
        s.append(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" class="vl" font-weight="700" font-size="16">{tot:,.0f}</text>')
    return Markup("".join(s) + "</svg>")


def svg_spark(etq, vals, color="var(--azul)", ancho=300, alto=84):
    """Minigráfica de línea con área para tarjetas pequeñas. Cada punto muestra su valor al pasar el mouse."""
    if len(vals) < 2:
        return Markup("")
    mn, mx, n = min(vals), max(vals), len(vals)
    r = (mx - mn) or 1
    px = lambda i: 8 + (ancho - 16) * i / (n - 1)
    py = lambda v: 8 + (alto - 24) * (1 - (v - mn) / r)
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;height:auto;display:block" font-family="sans-serif" font-size="12">',
         f'<polygon points="8,{alto - 16} {pts} {ancho - 8},{alto - 16}" style="fill:{color};fill-opacity:.14"/><polyline points="{pts}" fill="none" style="stroke:{color}" stroke-width="2"/>']
    for i, v in enumerate(vals):
        s.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3.2" style="fill:{color}"><title>{escape(str(etq[i]))}: {v:,.2f}</title></circle>')
    s.append(f'<text x="8" y="{alto - 3}" class="ax">{escape(str(etq[0]))}</text><text x="{ancho - 8}" y="{alto - 3}" text-anchor="end" class="ax">{escape(str(etq[-1]))}</text>')
    return Markup("".join(s) + "</svg>")


def _ord(f):
    try:
        return date.fromisoformat(f).toordinal()
    except ValueError:
        return date.today().toordinal()


# ---------- Acceso ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db.obtener_usuario_por_username(request.form["username"].strip())
        if u and u["activo"] != 0 and verificar_password(request.form["password"], u["password_hash"]):
            session["user"] = {"username": u["username"], "rol": u["rol"], "nombre": u["nombre_completo"] or u["username"]}
            db.sql("UPDATE usuarios SET ultimo_acceso=datetime('now','localtime') WHERE id=?", (u["id"],), escribir=True)
            log("inició sesión", "Acceso")
            return redirect(url_for("inicio"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    if "user" in session:
        log("cerró sesión", "Acceso")
    session.clear()
    return redirect(url_for("login"))


@app.route("/cuenta", methods=["GET", "POST"])
@acceso()
def cuenta():
    if request.method == "POST":
        f = request.form
        u = db.obtener_usuario_por_username(session["user"]["username"])
        if not verificar_password(f["actual"], u["password_hash"]):
            flash("La contraseña actual no es correcta.", "error")
        elif f["nueva"] != f["confirmar"] or len(f["nueva"]) < 6:
            flash("La nueva contraseña debe tener 6 caracteres o más y coincidir con la confirmación.", "error")
        else:
            db.sql("UPDATE usuarios SET password_hash=? WHERE id=?", (generar_hash(f["nueva"]), u["id"]), escribir=True)
            log("cambió su contraseña", "Seguridad")
            flash("Contraseña actualizada.", "ok")
        return redirect(url_for("cuenta"))
    return render_template("cuenta.html")


# ---------- Inicio e inventario ----------

COLORES = {"promedio": "#FFB84D", "peps": "#4FD1C5", "ueps": "#FF7A90"}
PAGINAS = {"inicio": "Inicio", "inventario": "Inventario", "movimientos": "Movimientos", "kardex": "Kardex", "reportes": "Reportes", "importar": "Importar", "importar_vista": "Importar",
           "base": "Base de datos", "configuracion": "Empresa y apariencia", "usuarios": "Usuarios y roles", "cuenta": "Mi cuenta"}


def baja_rotacion(inv):
    """Productos cuyo lote más antiguo que sigue en bodega superó los días estimados (config 'dias_estadia'). Los menos vendidos primero."""
    dias, hoy_o, out = int(cfg("dias_estadia", "60") or 60), date.today().toordinal(), []
    for p in inv:
        if p["existencia"] <= 0:
            continue
        lotes, ultima, vendidas = [], None, 0.0
        for mo in db.listar_movimientos(p["id"]):
            if mo["tipo"] == "entrada":
                lotes.append([mo["fecha"], mo["cantidad"]])
                continue
            ultima, falta = mo["fecha"], mo["cantidad"]
            vendidas += mo["cantidad"]
            while falta > 1e-9 and lotes:  # salen primero los lotes más antiguos (orden físico de llegada)
                u = min(lotes[0][1], falta)
                lotes[0][1] -= u
                falta -= u
                if lotes[0][1] <= 1e-9:
                    lotes.pop(0)
        if lotes and hoy_o - _ord(lotes[0][0]) > dias:
            out.append({"nombre": p["nombre"], "edad": hoy_o - _ord(lotes[0][0]), "estimado": dias, "existencia": p["existencia"], "valor": p["valor"],
                        "vendidas": vendidas, "ultima": (hoy_o - _ord(ultima)) if ultima else None})
    return sorted(out, key=lambda x: (-x["edad"], x["vendidas"]))


def serie_valor(n=7):
    """Valor del inventario con cada método en las últimas n fechas con movimientos."""
    fechas = [r["fecha"] for r in db.sql("SELECT DISTINCT fecha FROM movimientos WHERE COALESCE(anulado,0)=0 ORDER BY fecha DESC LIMIT ?", (n,))][::-1]
    movs = [db.listar_movimientos(p["id"]) for p in db.listar_productos()]
    out = {k: [] for k in METODOS}
    for f in fechas:
        for k in METODOS:
            v = 0.0
            for ms in movs:
                hasta = [x for x in ms if x["fecha"] <= f]
                if hasta:
                    v += METODOS[k](hasta)[-1]["saldo_valor"]
            out[k].append(round(v, 2))
    return fechas, out


@app.route("/")
@acceso()
def inicio():
    m = metodo_actual()
    inv = calcular_inventario(m)
    repo = reposicion(inv)
    repo_total = round(sum(x["inversion"] or 0 for x in repo), 2)
    cnt = lambda t: db.sql("SELECT COUNT(*) FROM movimientos WHERE tipo=? AND fecha=? AND COALESCE(anulado,0)=0", (t, hoy()), uno=True)[0]
    hoy_movs = db.sql("SELECT m.*, p.nombre FROM movimientos m JOIN productos p ON p.id=m.producto_id WHERE m.fecha=? AND COALESCE(m.anulado,0)=0 ORDER BY m.id DESC", (hoy(),))
    ahora, (fechas, ser), est = datetime.now(), serie_valor(), estado_resultados("", "")
    comp = {k: {"valor": sum(p["valor"] for p in (inv if k == m else calcular_inventario(k))), "costo": est[k]["ven_v"]} for k in METODOS}
    serie = {"ult": ser[m][-1], "var": round(ser[m][-1] - ser[m][0], 2), "max": max(ser[m]), "min": min(ser[m]), "n": len(fechas)} if fechas else None
    graf_valor = svg_lineas(fechas, [(NOMBRES[k], COLORES[k], ser[k], k == m) for k in ORDEN], ancho=480, alto=170)
    graf_m = {k: svg_lineas(fechas, [(NOMBRES[k], COLORES[k], ser[k], True)], ancho=540, alto=240) for k in ORDEN} if fechas else {}
    stats_m = {k: {"ult": ser[k][-1], "var": round(ser[k][-1] - ser[k][0], 2), "max": max(ser[k]), "min": min(ser[k])} for k in ORDEN} if fechas else {}
    nom = lambda *e: [p["nombre"] for p in inv if p["estado"] in e]
    tv = max(inv, key=lambda p: p["valor"]) if inv else None
    vend = db.sql("SELECT p.nombre, SUM(m.cantidad) q FROM movimientos m JOIN productos p ON p.id=m.producto_id WHERE m.tipo='salida' AND COALESCE(m.anulado,0)=0 GROUP BY p.id ORDER BY q DESC LIMIT 1", uno=True)
    resumen_p = {"con_stock": sum(1 for p in inv if p["existencia"] > 0), "unidades": sum(max(p["existencia"], 0) for p in inv),
                 "top_valor": (tv["nombre"], tv["valor"]) if tv else None, "top_vendido": (vend["nombre"], vend["q"]) if vend else None}
    return render_template("inicio.html", inv=inv, valor=sum(p["valor"] for p in inv), ent=cnt("entrada"), sal=cnt("salida"), alertas=[p for p in inv if p["estado"] != "ok"],
                           usa_metodo=True, hora=ahora.hour, fecha_hoy=f"{DIAS[ahora.weekday()].capitalize()} {ahora.day} de {MESES[ahora.month - 1]}, {ahora.year}",
                           graf_valor=graf_valor, graf_m=graf_m, stats_m=stats_m, nfechas=len(fechas), COLORES=COLORES, comp=comp, hoy_movs=hoy_movs, repo=repo, repo_total=repo_total, estatus=estatus_inventario(inv), serie=serie, ORDEN=ORDEN,
                           leyenda=[(NOMBRES[k], COLORES[k], k == m) for k in ORDEN], resumen_p=resumen_p, lenta=baja_rotacion(inv), dias=cfg("dias_estadia", "60"),
                           grupos=[("disponibles", "var(--verde)", nom("ok")), ("con stock bajo", "var(--warn)", nom("bajo")), ("agotados", "var(--rojo)", nom("agotado", "negativo"))])


@app.route("/inventario")
@acceso()
def inventario():
    inv = calcular_inventario(metodo_actual())
    return render_template("inventario.html", inv=inv, total=sum(p["valor"] for p in inv), cant=sum(p["existencia"] for p in inv), usa_metodo=True,
                           unidades=db.sql("SELECT nombre FROM unidades ORDER BY nombre COLLATE NOCASE"))


@app.post("/inventario/guardar")
@acceso("productos")
def producto_guardar():
    f = request.form
    cod, nom = f["codigo"].strip(), f["nombre"].strip()
    uni = f.get("unidad", "").strip()
    if uni == "__nueva__":
        uni = f.get("unidad_nueva", "").strip()
    uni = uni or "unidad"
    try:
        minimo = float(f["stock_minimo"]) if f.get("stock_minimo", "").strip() else None
    except ValueError:
        minimo = None
    if not cod or not nom:
        flash("Código y nombre son obligatorios.", "error")
        return redirect(url_for("inventario"))
    try:
        es_nuevo = not f.get("pid")
        if es_nuevo:
            db.crear_producto(cod, nom, uni)
            pid = db.obtener_producto_por_codigo(cod)["id"]
        else:
            pid = int(f["pid"])
            antes = dict(db.obtener_producto(pid))
            db.actualizar_producto(pid, cod, nom, uni)
        db.sql("UPDATE productos SET stock_minimo=? WHERE id=?", (minimo, pid), escribir=True)
        db.sql("INSERT OR IGNORE INTO unidades VALUES (?)", (uni,), escribir=True)
        if not es_nuevo:
            det = cambios(antes, {"codigo": cod, "nombre": nom, "unidad_medida": uni, "stock_minimo": minimo},
                          [("codigo", "código"), ("nombre", "nombre"), ("unidad_medida", "unidad"), ("stock_minimo", "stock mínimo")])
            if det:
                log_edicion("editó producto", "productos", f"{cod} – {nom}", det)
        log("guardó producto", "Inventario", f"{cod} – {nom}")
        err = None
        if f.get("existencia", "").strip():
            err = ajustar_existencia(pid, f["existencia"], f.get("costo", ""), f.get("ex_orig", ""), es_nuevo) if puede("movimientos") else "no tienes permiso para registrar movimientos."
        flash(f"Producto guardado, pero no se cambió la existencia: {err}" if err else "Producto guardado.", "error" if err else "ok")
    except sqlite3.IntegrityError:
        flash("Ese código ya existe.", "error")
    return redirect(url_for("inventario"))


# ---------- Movimientos (entradas y salidas) ----------

@app.route("/movimientos")
@acceso()
def movimientos():
    a = request.args
    ini, fin, pid = a.get("desde", ""), a.get("hasta", ""), a.get("pid", type=int)
    ms = sorted(movimientos_costeados(metodo_actual(), None, ini, fin, pid), key=lambda x: (x["fecha"], x["id"]), reverse=True)
    return render_template("movimientos.html", ent=[x for x in ms if x["tipo"] == "entrada"], sal=[x for x in ms if x["tipo"] == "salida"], ini=ini, fin=fin,
                           pid=pid, prods=db.listar_productos(), ex={p["id"]: p["existencia"] for p in resumen()["productos"]}, hoy=hoy(), usa_metodo=True)


def guardar_mov(f, mid=None):
    """Devuelve un mensaje de error, o None si se guardó."""
    pid = int(f["pid"]) if f.get("pid", "").isdigit() else None
    try:
        cant = float(f["cantidad"])
        precio = float(f["precio"]) if f.get("precio", "").strip() else None
    except ValueError:
        return "Cantidad y precio deben ser números."
    tipo = f["tipo"]
    if tipo == "salida":
        precio = None  # el precio de una salida sale solo del inventario, con el método elegido
    if not pid:
        return "Elige un producto."
    if cant <= 0:
        return "La cantidad debe ser mayor que cero."
    if tipo == "entrada" and not precio:
        return "Las entradas necesitan precio (costo unitario)."
    if tipo == "salida" and mid is None and cant > existencia(pid):
        return f"No hay suficiente existencia: hay {existencia(pid):,.2f}."
    fecha, doc, obs = f.get("fecha") or hoy(), f.get("documento", "").strip(), f.get("observaciones", "").strip()
    if mid:
        antes = dict(db.obtener_movimiento(mid))
        doc = doc or (antes["documento"] or "")  # si lo borran, se conserva el número que ya tenía
        db.sql("UPDATE movimientos SET fecha=?, cantidad=?, costo_unitario=?, documento=?, observaciones=? WHERE id=?", (fecha, cant, precio, doc, obs, mid), escribir=True)
        db.completar_documentos()
        det = cambios(antes, {"fecha": fecha, "cantidad": cant, "costo_unitario": precio, "documento": doc, "observaciones": obs},
                      [("fecha", "fecha"), ("cantidad", "cantidad"), ("costo_unitario", "precio"), ("documento", "documento"), ("observaciones", "observaciones")])
        if det:
            log_edicion("editó movimiento", "movimientos", f"{doc} · {db.obtener_producto(pid)['nombre']}", det)
    else:
        db.crear_movimiento(pid, fecha, tipo, cant, precio, usuario=session["user"]["username"], documento=doc, observaciones=obs)
    log(("editó " if mid else "registró ") + tipo, "Movimientos", f"{db.obtener_producto(pid)['nombre']} × {cant:g}")


@app.post("/movimientos/nuevo/<tipo>")
@acceso("movimientos")
def mov_nuevo(tipo):
    if tipo not in ("entrada", "salida"):
        abort(404)
    err = guardar_mov({**request.form.to_dict(), "tipo": tipo})
    flash(err or ("Entrada registrada correctamente." if tipo == "entrada" else "Salida registrada correctamente."), "error" if err else "ok")
    return redirect(url_for("movimientos"))


@app.post("/movimientos/<int:mid>/editar")
@acceso("movimientos")
def mov_editar(mid):
    mo = db.obtener_movimiento(mid) or abort(404)
    err = guardar_mov({**request.form.to_dict(), "tipo": mo["tipo"], "pid": str(mo["producto_id"])}, mid)
    flash(err or "Cambios guardados.", "error" if err else "ok")
    return redirect(url_for("movimientos"))


@app.post("/movimientos/<int:mid>/eliminar")
@acceso("movimientos")
def mov_eliminar(mid):
    mo = db.obtener_movimiento(mid)
    if mo:
        pr = db.obtener_producto(mo["producto_id"])
        log_edicion("eliminó movimiento", "movimientos", f"{mo['documento'] or '#' + str(mid)} · {pr['nombre'] if pr else ''}",
                    f"{mo['tipo']} de {mo['cantidad']:g} u del {mo['fecha']}" + (f" a {mo['costo_unitario']:g}" if mo["costo_unitario"] else ""))
    db.eliminar_movimiento(mid)
    log("eliminó movimiento", "Movimientos", f"#{mid}")
    flash("Movimiento eliminado.", "ok")
    return redirect(url_for("movimientos"))


# ---------- Kardex y reportes ----------

@app.route("/kardex")
@acceso()
def kardex():
    m, prods = metodo_actual(), db.listar_productos()
    pid = request.args.get("pid", type=int) or (prods[0]["id"] if prods else None)
    if not pid:
        return render_template("kardex.html", p=None, prods=prods, usa_metodo=True)
    ini, fin = request.args.get("desde", ""), request.args.get("hasta", "")
    filas = filas_kardex(pid, m, ini, fin)
    etq = [f["fecha"] for f in filas]
    g1 = svg_barras(etq, [("Entradas", "var(--verde)", [f["e"][0] if f["e"] else 0 for f in filas]), ("Salidas", "var(--rojo)", [f["s"][0] if f["s"] else 0 for f in filas])])
    g2 = svg_linea(etq, [f["v"][2] for f in filas], "var(--azul)")
    mon, vs = cfg("moneda", "Q"), [f["v"][2] for f in filas]
    st1 = [("Entradas", f"{sum(f['e'][0] for f in filas if f['e']):,.0f} u"), ("Salidas", f"{sum(f['s'][0] for f in filas if f['s']):,.0f} u"), ("Movimientos", str(len(filas)))]
    st2 = [("Saldo final", f"{mon}{vs[-1]:,.2f}"), ("Máximo", f"{mon}{max(vs):,.2f}"), ("Mínimo", f"{mon}{min(vs):,.2f}")] if vs else []
    return render_template("kardex.html", p=db.obtener_producto(pid) or abort(404), prods=prods, pid=pid, ini=ini, fin=fin, filas=filas,
                           ult=filas[-1]["v"] if filas else (0, 0, 0), g1=g1, g2=g2, st1=st1, st2=st2, usa_metodo=True)


@app.route("/kardex/exportar/<fmt>")
@acceso("exportar")
def kardex_exportar(fmt):
    if fmt not in ("xlsx", "pdf", "csv", "txt"):
        abort(404)
    m, pid = metodo_actual(), request.args.get("pid", type=int)
    p = db.obtener_producto(pid) or abort(404)
    ini, fin = request.args.get("desde", ""), request.args.get("hasta", "")
    filas = filas_kardex(pid, m, ini, fin)
    titulo = f"Kardex – {p['nombre']} ({p['codigo']}) – {NOMBRES[m]}"
    log("exportó kardex", "Kardex", f"{p['nombre']} ({fmt})")
    if fmt in ("pdf", "xlsx"):  # con el mismo formato de la pantalla: bloques Entradas / Salidas / Saldo
        buf = exportador.exportar_kardex(fmt, titulo, filas, cfg("moneda", "Q"), cfg("empresa", "Inventario Flash"), NOMBRES[m], ini, fin, session["user"]["nombre"])
    else:
        enc, rows = kardex_tabla(filas)
        buf = exportador.exportar(fmt, titulo, enc, rows)
    return send_file(buf, as_attachment=True, download_name=f"kardex_{p['codigo']}.{fmt}")


def svg_barras_h(etq, vals, color, ancho=780):
    if not etq:
        return Markup("")
    mx, L, alto = max(vals + [1]), 170, 24 + len(etq) * 30
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;max-width:{ancho}px;height:auto;display:block" font-family="sans-serif" font-size="12.5">']
    for i, (e, v) in enumerate(zip(etq, vals)):
        y, w = 12 + i * 30, (ancho - L - 110) * max(v, 0) / mx
        s.append(f'<text x="{L - 10}" y="{y + 15}" text-anchor="end" class="ax">{escape(str(e)[:24])}</text><rect x="{L}" y="{y}" width="{ancho - L - 110}" height="20" rx="5" class="trk"/>'
                 f'<rect x="{L}" y="{y}" width="{w:.1f}" height="20" rx="5" style="fill:{color}"><title>{escape(str(e))}: {v:,.2f}</title></rect><text x="{L + w + 8:.1f}" y="{y + 15}" class="vl" font-weight="700">{v:,.2f}</text>')
    return Markup("".join(s) + "</svg>")


def svg_puntos_cat(etq, vals, color, ancho=780, alto=310):
    if not etq:
        return Markup("")
    T = 30
    mx, n, L, alt = max(vals + [1]), len(etq), 64, alto - 96
    gw, rotulos = (ancho - L - 18) / n, len(etq) <= 16
    s = [f'<svg viewBox="0 0 {ancho} {alto}" style="width:100%;max-width:{ancho}px;height:auto;display:block" font-family="sans-serif" font-size="12">']
    for i in range(5):
        y = T + alt * i / 4
        s.append(f'<line x1="{L}" x2="{ancho - 12}" y1="{y:.1f}" y2="{y:.1f}" class="gr"/><text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end" class="ax">{mx * (4 - i) / 4:,.0f}</text>')
    s.append(f'<path d="M{L} {T + alt}V{T - 12}M{L - 4} {T - 6}L{L} {T - 14}L{L + 4} {T - 6}M{L} {T + alt}H{ancho - 6}" class="axl" fill="none"/>')
    for i, (e, v) in enumerate(zip(etq, vals)):
        x, y = L + gw * i + gw / 2, T + alt - alt * max(v, 0) / mx
        s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T + alt}" y2="{y:.1f}" class="gr" stroke-width="2"/><circle cx="{x:.1f}" cy="{y:.1f}" r="7" class="ring" style="stroke:{color}" stroke-width="3"><title>{escape(str(e))}: {v:,.2f}</title></circle>')
        if rotulos:
            s.append(f'<text x="{x:.1f}" y="{y - 13:.1f}" text-anchor="middle" class="vl" font-weight="700">{v:,.0f}</text>')
        s.append(f'<text transform="translate({x:.1f},{T + alt + 14}) rotate(-28)" text-anchor="end" class="ax">{escape(str(e)[:11])}</text>')
    return Markup("".join(s) + "</svg>")


def dibujar(tipo, etq, vals, color):
    """Dibuja la información elegida con el tipo de gráfico elegido."""
    corta = (etq[:15], vals[:15])
    if tipo == "barras":
        return svg_barras(corta[0], [("", color, corta[1])])
    if tipo == "horizontal":
        return svg_barras_h(corta[0], corta[1], color)
    if tipo == "linea":
        return svg_linea(etq[:30], vals[:30], color)
    if tipo == "puntos":
        return svg_puntos_cat(corta[0], corta[1], color)
    return svg_dona(etq, vals, hueco=62 if tipo == "dona" else 0)


def estado_resultados(ini, fin, pid=None):
    """Para cada método: inventario inicial, compras, mercadería para la venta, inventario final, costo de ventas y las unidades."""
    out = {}
    for m in METODOS:
        d = dict.fromkeys(("ini_c", "ini_v", "com_c", "com_v", "ven_c", "ven_v", "fin_c", "fin_v"), 0.0)
        for p in db.listar_productos():
            if pid and p["id"] != pid:
                continue
            ms = db.listar_movimientos(p["id"])
            filas = METODOS[m](ms)
            antes = [f for f in filas if ini and ms[f["idx"]]["fecha"] < ini]
            hasta = [f for f in filas if not fin or ms[f["idx"]]["fecha"] <= fin]
            if antes:
                d["ini_c"] += antes[-1]["saldo_cantidad"]
                d["ini_v"] += antes[-1]["saldo_valor"]
            if hasta:
                d["fin_c"] += hasta[-1]["saldo_cantidad"]
                d["fin_v"] += hasta[-1]["saldo_valor"]
            for f in filas:
                fe = ms[f["idx"]]["fecha"]
                if (ini and fe < ini) or (fin and fe > fin):
                    continue
                k = "com" if f["tipo"] == "entrada" else "ven"
                d[k + "_c"] += f["cantidad"]
                d[k + "_v"] += f["costo_total"]
        d["disp_c"], d["disp_v"] = d["ini_c"] + d["com_c"], d["ini_v"] + d["com_v"]
        out[m] = {k: round(v, 2) for k, v in d.items()}
    return out


def datos_reporte(r, m, ini, fin, pid):
    """Devuelve (encabezados, filas, total, métricas, burbujas, resumen). 'métricas' = {clave: (título, etiquetas, valores, color)}: la información que se puede graficar."""
    mon = cfg("moneda", "Q")
    if r == "estado":
        e = estado_resultados(ini, fin, pid)
        lin = [("Inventario inicial", "ini_v"), ("(+) Compras", "com_v"), ("Costo de mercadería para la venta", "disp_v"), ("(-) Inventario final", "fin_v"), ("Costo de ventas", "ven_v"),
               ("Unidades: inventario inicial", "ini_c"), ("Unidades compradas", "com_c"), ("Total de unidades", "disp_c"), ("Unidades vendidas", "ven_c"), ("Unidades en existencia", "fin_c")]
        rows = [[n] + [e[k][c] for k in ORDEN] for n, c in lin]
        return ["Concepto", "Promedio ponderado", "PEPS", "UEPS"], rows, 0, {}, Markup(""), [(f"Costo de ventas · {NOMBRES[k]}", f"{mon}{e[k]['ven_v']:,.2f}") for k in ORDEN]
    if r == "inventario":
        inv = sorted([p for p in calcular_inventario(m) if not pid or p["id"] == pid], key=lambda p: -p["valor"])
        rows = [[p["codigo"], p["nombre"], p["existencia"], p["precio"], p["valor"]] for p in inv]
        nom, mx = [x[1] for x in rows], max([x[4] for x in rows] + [1])
        met = {"valor": ("Valor por producto", nom, [x[4] for x in rows], "var(--azul)"), "unidades": ("Unidades por producto", nom, [x[2] for x in rows], "var(--verde)"),
               "precio": ("Precio unitario por producto", nom, [x[3] for x in rows], "var(--azul)")}
        burb = svg_puntos([(x[2], x[3], f"{x[1]}: {x[2]:,.0f} u × {mon}{x[3]:,.2f} = {mon}{x[4]:,.2f}", x[4] / mx) for x in rows], "Cantidad en existencia", "Precio unitario")
        tot = sum(x[4] for x in rows)
        return ["Código", "Producto", "Cantidad", "Precio", "Total"], rows, tot, met, burb, [("Productos", len(rows)), ("Unidades en existencia", f"{sum(x[2] for x in rows):,.0f}"), ("Valor total", f"{mon}{tot:,.2f}")]
    if r in ("entradas", "salidas"):
        rows = [[x["fecha"], x["documento"] or "–", x["codigo"], x["producto"], x["cantidad"], x["precio"], x["total"]] for x in movimientos_costeados(m, r[:-1], ini, fin, pid)]
        dia, udia, prod, uprod = {}, {}, {}, {}
        for x in rows:
            dia[x[0]], udia[x[0]] = dia.get(x[0], 0) + x[6], udia.get(x[0], 0) + x[4]
            prod[x[3]], uprod[x[3]] = prod.get(x[3], 0) + x[6], uprod.get(x[3], 0) + x[4]
        col, mc = ("var(--verde)" if r == "entradas" else "var(--rojo)"), max([x[4] for x in rows] + [1])
        ordp = sorted(prod, key=lambda k: -prod[k])
        met = {"total_dia": ("Total por día", list(dia), list(dia.values()), col), "unidades_dia": ("Unidades por día", list(udia), list(udia.values()), col),
               "total_producto": ("Total por producto", ordp, [prod[k] for k in ordp], "var(--azul)"), "unidades_producto": ("Unidades por producto", ordp, [uprod[k] for k in ordp], "var(--azul)")}
        burb = svg_puntos([(_ord(x[0]), x[6], f"{x[3]} · {x[0]}: {x[4]:g} × {mon}{x[5]:,.2f}", x[4] / mc) for x in rows], "Fecha", "Total del movimiento", xlab=lambda v: date.fromordinal(int(v)).isoformat()[5:], color=col)
        tot = sum(x[6] for x in rows)
        return ["Fecha", "Documento", "Código", "Producto", "Cantidad", "Precio", "Total"], rows, tot, met, burb, [("Movimientos", len(rows)), ("Unidades", f"{sum(x[4] for x in rows):,.0f}"), ("Total", f"{mon}{tot:,.2f}")]
    ag = {}
    for x in movimientos_costeados(m, "salida", ini, fin, pid):
        a = ag.setdefault((x["codigo"], x["producto"]), [0, 0])
        a[0] += x["cantidad"]
        a[1] += x["total"]
    rows = [[i + 1, k[0], k[1], v[0], round(v[1], 2)] for i, (k, v) in enumerate(sorted(ag.items(), key=lambda kv: -kv[1][0])[:10])]
    mt = max([x[4] for x in rows] + [1])
    met = {"unidades": ("Unidades vendidas", [x[2] for x in rows], [x[3] for x in rows], "var(--rojo)"), "total": ("Total por producto", [x[2] for x in rows], [x[4] for x in rows], "var(--azul)")}
    burb = svg_puntos([(x[3], x[4], f"{x[2]}: {x[3]:g} u, {mon}{x[4]:,.2f}", x[4] / mt) for x in rows], "Unidades vendidas", "Total", color="var(--rojo)")
    tot = sum(x[4] for x in rows)
    return ["#", "Código", "Producto", "Unidades", "Total"], rows, tot, met, burb, [("Productos", len(rows)), ("Unidades", f"{sum(x[3] for x in rows):,.0f}"), ("Total", f"{mon}{tot:,.2f}")]


def args_reporte():
    a = request.args
    r = a.get("r", "inventario")
    return (r if r in REPORTES else "inventario"), metodo_actual(), a.get("desde", ""), a.get("hasta", ""), a.get("pid", type=int)


@app.route("/reportes")
@acceso()
def reportes():
    r, m, ini, fin, pid = args_reporte()
    enc, rows, total, met, burb, resumen = datos_reporte(r, m, ini, fin, pid)
    a = request.args
    info = a.get("info") if a.get("info") in met else next(iter(met), "")
    g = a.get("g") if a.get("g") in TIPOS else "barras"
    grafico, est, g_est, gstats, gtitulo = Markup(""), None, Markup(""), [], ""
    if r == "estado":
        est = estado_resultados(ini, fin, pid)
        g_est = svg_barras(["Promedio", "PEPS", "UEPS"], [("Costo de ventas", "var(--rojo)", [est[k]["ven_v"] for k in ORDEN]), ("Inventario final", "var(--azul)", [est[k]["fin_v"] for k in ORDEN])])
    elif g == "burbujas":
        grafico = burb
        gtitulo = "Cantidad contra precio (el tamaño del punto es el valor)" if r == "inventario" else "Dos variables (el tamaño del punto es la cantidad o el total)"
        gstats = [("Puntos", str(len(rows)))]
    elif g != "ninguno" and info:
        gtitulo, etq, vals, color = met[info]
        grafico = dibujar(g, etq, vals, color)
        if vals:
            mon = cfg("moneda", "Q") if info in ("valor", "total", "total_dia", "total_producto", "precio") else ""
            f = lambda v: f"{mon}{v:,.2f}"
            gstats = [("Total", f(sum(vals))), ("Promedio", f(sum(vals) / len(vals))), ("Máximo", f"{etq[vals.index(max(vals))]} · {f(max(vals))}"),
                      ("Mínimo", f"{etq[vals.index(min(vals))]} · {f(min(vals))}"), ("Datos", str(len(vals)))]
    return render_template("reportes.html", r=r, ini=ini, fin=fin, pid=pid, enc=enc, rows=rows, total=total, met=met, info=info, g=g, TIPOS=TIPOS, grafico=grafico, resumen=resumen,
                           est=est, g_est=g_est, gstats=gstats, gtitulo=gtitulo, ORDEN=ORDEN, REPORTES=REPORTES, prods=db.listar_productos(), usa_metodo=(r != "estado"))


@app.route("/reportes/exportar/<fmt>")
@acceso("exportar")
def reportes_exportar(fmt):
    if fmt not in ("xlsx", "pdf", "csv", "txt"):
        abort(404)
    r, m, ini, fin, pid = args_reporte()
    enc, rows, total = datos_reporte(r, m, ini, fin, pid)[:3]
    log("exportó reporte", "Reportes", f"{REPORTES[r]} ({fmt})")
    return send_file(exportador.exportar(fmt, f"{cfg('empresa', 'Inventario Flash')} – {REPORTES[r]} ({NOMBRES[m]})", enc, rows, f"Total: {total:,.2f}" if r != "estado" else ""), as_attachment=True, download_name=f"reporte_{r}.{fmt}")


# ---------- Importar (sin plantilla: detecta las columnas solo) ----------

def _nv(v):
    if v is None or str(v).strip() == "":
        return ""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v).strip()


def cambios(antes, despues, campos):
    """Texto con lo que cambió: 'cantidad: 100 → 90; documento: COMP-0001 → F-9'."""
    out = []
    for k, etq in campos:
        a, b = _nv(antes.get(k)), _nv(despues.get(k))
        if a != b:
            out.append(f"{etq}: {a or '–'} → {b or '–'}")
    return "; ".join(out)


def log_edicion(accion, tabla, registro, detalle):
    db.registrar_edicion(session.get("user", {}).get("username", "-"), accion, tabla, registro, detalle)


def ajustar_existencia(pid, nuevo_txt, costo_txt, orig_txt, es_nuevo):
    """Cambiar la existencia de un producto = registrar un movimiento por la diferencia (INI-000N si es inicial, AJ-000N si es ajuste).
    Devuelve un mensaje de error o None."""
    try:
        nuevo, orig = float(nuevo_txt), (float(orig_txt) if orig_txt.strip() else 0.0)
        costo = float(costo_txt) if costo_txt.strip() else None
    except ValueError:
        return "La existencia y el costo deben ser números."
    if nuevo < 0:
        return "La existencia no puede ser negativa."
    if abs(nuevo - orig) < 1e-9:
        return None  # no la tocó
    actual = existencia(pid)
    dif = round(nuevo - actual, 6)
    if abs(dif) < 1e-9:
        return None
    nombre, usuario = db.obtener_producto(pid)["nombre"], session["user"]["username"]
    if dif > 0:
        if not costo or costo <= 0:
            return "Para agregar unidades indica el costo unitario."
        doc = db.siguiente_documento("INI" if es_nuevo else "AJ")
        db.crear_movimiento(pid, hoy(), "entrada", dif, costo, usuario=usuario, documento=doc,
                            observaciones="Existencia inicial" if es_nuevo else f"Ajuste de inventario: de {actual:g} a {nuevo:g}")
    else:
        doc = db.siguiente_documento("AJ")
        db.crear_movimiento(pid, hoy(), "salida", -dif, None, usuario=usuario, documento=doc, observaciones=f"Ajuste de inventario: de {actual:g} a {nuevo:g}")
    log_edicion("existencia inicial" if es_nuevo else "ajustó existencia", "productos", nombre, f"existencia: {actual:g} → {nuevo:g} ({doc})")
    return None


def reposicion(inv):
    """Productos en alerta con la cantidad sugerida para llegar al doble de su mínimo y la inversión estimada (último costo de compra)."""
    out = []
    for p in inv:
        if p["estado"] == "ok":
            continue
        sug = math.ceil(max(2 * p["stock_minimo"] - p["existencia"], 1))
        ult = db.sql("SELECT costo_unitario FROM movimientos WHERE producto_id=? AND tipo='entrada' AND costo_unitario>0 AND COALESCE(anulado,0)=0 ORDER BY fecha DESC, id DESC LIMIT 1", (p["id"],), uno=True)
        costo = ult["costo_unitario"] if ult else None
        out.append({"id": p["id"], "nombre": p["nombre"], "estado": p["estado"], "existencia": p["existencia"], "minimo": p["stock_minimo"], "sugerido": sug,
                    "costo": costo, "inversion": round(sug * costo, 2) if costo else None})
    return sorted(out, key=lambda x: x["existencia"] - x["minimo"])


def estatus_inventario(inv):
    """Estatus general del inventario para el panel de alertas: Saludable, Atención o Crítico."""
    tot = len(inv)
    if not tot:
        return {"nivel": "sin", "texto": "Sin productos", "detalle": "Registra tu primer producto para empezar.", "pct": 0}
    ok = sum(1 for p in inv if p["estado"] == "ok")
    bj = sum(1 for p in inv if p["estado"] == "bajo")
    ag = sum(1 for p in inv if p["estado"] in ("agotado", "negativo"))
    neg = any(p["estado"] == "negativo" for p in inv)
    nivel, texto = ("crit", "Crítico") if (neg or ag / tot >= 0.25) else ("aten", "Atención") if (ag or bj) else ("ok", "Saludable")
    partes = [f"{n} {t}" for n, t in ((ag, "agotado" if ag == 1 else "agotados"), (bj, "con stock bajo")) if n]
    detalle = (f"De {tot} producto{'s' if tot != 1 else ''}: " + " y ".join(partes) + ".") if partes else ("Tus productos tienen existencia suficiente." if tot != 1 else "Tu producto tiene existencia suficiente.")
    return {"nivel": nivel, "texto": texto, "detalle": detalle, "pct": round(ok / tot * 100)}


def buscar_producto(nom):
    """Busca por código o nombre sin importar mayúsculas, acentos ni plural/singular (cuaderno = Cuadernos)."""
    sing = lambda t: re.sub(r"(es|s)$", "", t) if len(t) > 3 else t
    k = importador._n(nom)
    for p in db.listar_productos():
        for v in (p["codigo"], p["nombre"]):
            n = importador._n(v)
            if n == k or sing(n) == sing(k):
                return p
    return None


def marcar(filas):
    vistos = set()
    for r in filas:
        r["duplicado"] = False
        nom = r["codigo_producto"] or r["nombre_producto"]
        p = buscar_producto(nom) if nom else None
        r["nuevo"] = bool(nom) and not p
        clave = (str(nom).lower(), r["fecha"], r["tipo"], r["cantidad"])
        if not r["error"] and (clave in vistos or (p and db.sql("SELECT 1 FROM movimientos WHERE producto_id=? AND fecha=? AND tipo=? AND cantidad=? AND COALESCE(anulado,0)=0",
                                                                (p["id"], r["fecha"], r["tipo"], r["cantidad"]), uno=True))):
            r["duplicado"] = True
        vistos.add(clave)
    return filas


def resumen_importacion(res):
    """Lo que encontré en el archivo: totales, productos, fechas y avisos (por ejemplo, salidas que dejarían existencia negativa)."""
    ok = [r for r in res if not r["error"]]
    ent, sal = [r for r in ok if r["tipo"] == "entrada"], [r for r in ok if r["tipo"] == "salida"]
    prods, saldo, avisos = {}, {}, []
    for r in sorted(ok, key=lambda r: r["fecha"]):
        nom = r["nombre_producto"] or r["codigo_producto"]
        p = buscar_producto(nom)
        prods[nom] = p is not None
        k = p["id"] if p else str(nom).lower()
        saldo.setdefault(k, existencia(p["id"]) if p else 0.0)
        saldo[k] += r["cantidad"] if r["tipo"] == "entrada" else -r["cantidad"]
        if r["tipo"] == "salida" and saldo[k] < 0 and len(avisos) < 6:
            avisos.append(f"{nom}: la salida del {r['fecha']} dejaría la existencia en {saldo[k]:g}.")
    fechas = sorted(r["fecha"] for r in ok)
    return {"n_ent": len(ent), "u_ent": sum(r["cantidad"] for r in ent), "v_ent": sum(r["cantidad"] * (r["costo_unitario"] or 0) for r in ent),
            "n_sal": len(sal), "u_sal": sum(r["cantidad"] for r in sal), "prods": sorted(prods.items()), "n_nuevos": sum(1 for v in prods.values() if not v),
            "desde": fechas[0] if fechas else "–", "hasta": fechas[-1] if fechas else "–", "avisos": avisos}


def vista_importacion(enc, filas, mapa, tipo_def, prod_def, archivo):
    p = db.obtener_producto(int(prod_def)) if prod_def else None
    res = marcar(importador.construir(filas, mapa, tipo_def, (p["codigo"], p["nombre"]) if p else None))
    return render_template("revision.html", enc=enc, filas=filas, res=res, mapa=mapa, campos=CAMPOS, tipo_def=tipo_def, prod_def=prod_def or "", archivo=archivo,
                           prods=db.listar_productos(), ok=sum(1 for x in res if not x["error"] and not x["duplicado"]), dup=sum(1 for x in res if x["duplicado"]),
                           mal=sum(1 for x in res if x["error"]), ncols=len(enc), info=resumen_importacion(res))


@app.route("/importar", methods=["GET", "POST"])
@acceso("importar")
def importar():
    if request.method == "GET":
        return render_template("importar.html")
    arch, texto = request.files.get("archivo"), request.form.get("texto", "").strip()
    if arch and arch.filename:
        ext = arch.filename.rsplit(".", 1)[-1].lower()
        with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as t:
            arch.save(t.name)
        try:
            enc, filas, _ = importador.extraer_tabla(t.name, ext)
        except Exception as e:
            flash(f"No se pudo leer el archivo: {e}", "error")
            return redirect(url_for("importar"))
        finally:
            os.unlink(t.name)
        nombre = arch.filename
    elif texto:
        (enc, filas), nombre = importador.tabla_desde_texto(texto), "texto escrito"
    else:
        flash("Elige un archivo o escribe el texto primero.", "error")
        return redirect(url_for("importar"))
    if not filas:
        flash("No encontré ninguna tabla ni frases de compra o venta en eso. Prueba con algo como: \"El 5 de septiembre se vendieron 20 unidades de cuaderno\".", "error")
        return redirect(url_for("importar"))
    return vista_importacion(enc, filas, importador.detectar(enc, filas), "", "", nombre)


@app.post("/importar/vista")
@acceso("importar")
def importar_vista():
    f = request.form
    n, celdas, enc = int(f["ncols"]), f.getlist("cel"), f.getlist("enc")
    filas = [celdas[i:i + n] for i in range(0, len(celdas), n)]
    mapa = {}
    for i, k in enumerate(f.getlist("col")):
        if k and k not in mapa:
            mapa[k] = i
    if f["accion"] == "analizar":
        return vista_importacion(enc, filas, mapa, f.get("tipo_def", ""), f.get("prod_def", ""), f["archivo"])
    p = db.obtener_producto(int(f["prod_def"])) if f.get("prod_def") else None
    res = importador.construir(filas, mapa, f.get("tipo_def", ""), (p["codigo"], p["nombre"]) if p else None)
    lote = []
    for i in map(int, f.getlist("incluir")):
        r = res[i]
        if r["error"]:
            continue
        cod, nom = r["codigo_producto"], r["nombre_producto"]
        if not cod:
            ex = buscar_producto(nom)
            cod = ex["codigo"] if ex else nom
        lote.append({"producto_id": db.obtener_o_crear_producto(cod, nom), "fecha": r["fecha"], "tipo": r["tipo"], "cantidad": r["cantidad"],
                     "costo_unitario": r["costo_unitario"] if r["tipo"] == "entrada" else None, "usuario": session["user"]["username"]})
    if not lote:
        flash("No marcaste ninguna fila válida para importar.", "error")
        return vista_importacion(enc, filas, mapa, f.get("tipo_def", ""), f.get("prod_def", ""), f["archivo"])
    db.crear_movimientos_masivo(lote)
    log("importó archivo", "Importar", f"{f['archivo']}: {len(lote)} movimientos")
    flash(f"Importación completada: {len(lote)} movimientos guardados.", "ok")
    return redirect(url_for("movimientos"))


# ---------- Base de datos ----------

OPS = {"contiene": "contiene", "igual": "es igual a", "distinto": "es distinto de", "empieza": "empieza con", "mayor": "es mayor que", "menor": "es menor que"}
DIMS = {"producto": "Producto", "tipo": "Tipo", "fecha": "Fecha", "mes": "Mes", "usuario": "Usuario", "documento": "Documento"}
MEDIDAS = {"cantidad": "Cantidad", "total": "Total (valuado con el método)", "precio": "Precio", "conteo": "Número de movimientos"}
FUNCS = {"suma": "Suma", "promedio": "Promedio", "max": "Máximo", "min": "Mínimo"}


def tabla_actual():
    t = request.args.get("t", "productos")
    t = t if t in TABLAS else "productos"
    return t, [r[1] for r in db.sql(f"PRAGMA table_info({t})")]


def filtros_base(cols):
    """Arma el WHERE de la búsqueda: texto libre en todas las columnas + filtros (columna, operador, valor). Solo columnas reales."""
    a, w, par = request.args, [], []
    q = a.get("q", "").strip()
    if q:
        w.append("(" + " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in cols) + ")")
        par += [f"%{q}%"] * len(cols)
    usados = []
    for c, o, v in zip(a.getlist("fc"), a.getlist("fo"), a.getlist("fv")):
        if c not in cols or o not in OPS or not v.strip():
            continue
        usados.append((c, o, v.strip()))
        v = v.strip()
        if o == "contiene":
            w.append(f"CAST({c} AS TEXT) LIKE ?"), par.append(f"%{v}%")
        elif o == "empieza":
            w.append(f"CAST({c} AS TEXT) LIKE ?"), par.append(f"{v}%")
        else:
            w.append(f"{c} {dict(igual='=', distinto='<>', mayor='>', menor='<')[o]} ?"), par.append(v)
    return (" WHERE " + " AND ".join(w)) if w else "", par, q, usados


def pivot(m, pf, pc, pm, fn):
    """Tabla dinámica de los movimientos: agrupa por filas y (opcional) columnas y resume la medida elegida."""
    datos = movimientos_costeados(m)
    for x in datos:
        x["mes"] = x["fecha"][:7]
    ag = sum if pm == "conteo" else {"suma": sum, "promedio": lambda v: sum(v) / len(v), "max": max, "min": min}[fn]
    cel, tf, tc, todos = {}, {}, {}, []
    for x in datos:
        f, c = str(x[pf] or "(vacío)"), (str(x[pc] or "(vacío)") if pc else "Total")
        v = (1 if x["primera"] else 0) if pm == "conteo" else x[pm]
        cel.setdefault((f, c), []).append(v)
        tf.setdefault(f, []).append(v)
        tc.setdefault(c, []).append(v)
        todos.append(v)
    celdas = {k: round(ag(v), 2) for k, v in cel.items()}
    return {"filas": sorted(tf), "cols": sorted(tc), "celdas": celdas, "tot_f": {k: round(ag(v), 2) for k, v in tf.items()}, "tot_c": {k: round(ag(v), 2) for k, v in tc.items()},
            "gran": round(ag(todos), 2) if todos else 0, "max": max(celdas.values(), default=0) or 1, "entero": pm == "conteo", "sin_col": not pc}


@app.route("/base")
@acceso("superadmin")
def base():
    t, cols = tabla_actual()
    donde, par, q, usados = filtros_base(cols)
    pag = max(request.args.get("pag", 1, type=int), 1)
    total = db.sql(f"SELECT COUNT(*) FROM {t}{donde}", par, uno=True)[0]
    filas = [dict(r) for r in db.sql(f"SELECT * FROM {t}{donde} ORDER BY id DESC LIMIT 50 OFFSET ?", par + [(pag - 1) * 50])]
    a = request.args
    pf, pc, pm, fn = (a.get("pf") if a.get("pf") in DIMS else "producto"), (a.get("pc", "tipo") if a.get("pc", "tipo") in DIMS else ""), (a.get("pm") if a.get("pm") in MEDIDAS else "cantidad"), (a.get("pfn") if a.get("pfn") in FUNCS else "suma")
    return render_template("base_datos.html", t=t, tablas=TABLAS, cols=cols, filas=filas, total=total, pag=pag, paginas=max(-(-total // 50), 1), q=q, filtros=usados or [("", "contiene", "")],
                           OPS=OPS, DIMS=DIMS, MEDIDAS=MEDIDAS, FUNCS=FUNCS, pf=pf, pc=pc, pm=pm, fn=fn, piv=pivot(metodo_actual(), pf, pc, pm, fn), usa_metodo=True,
                           args={k: v for k, v in request.args.to_dict(flat=False).items() if k != "pag"})


@app.route("/base/exportar/<fmt>")
@acceso("superadmin")
def base_exportar(fmt):
    if fmt not in ("xlsx", "csv"):
        abort(404)
    t, cols = tabla_actual()
    donde, par, _, _ = filtros_base(cols)
    filas = db.sql(f"SELECT * FROM {t}{donde} ORDER BY id DESC LIMIT 5000", par)
    log("exportó datos", "Base de datos", f"{t} ({fmt})")
    return send_file(exportador.exportar(fmt, f"Base de datos – {t}", cols, [["" if v is None else v for v in r] for r in filas]), as_attachment=True, download_name=f"{t}.{fmt}")


@app.post("/base/<t>/<int:i>/<accion>")
@acceso("superadmin")
def base_cambiar(t, i, accion):
    if t not in TABLAS[:2] or accion not in ("editar", "eliminar"):
        abort(404)
    antes = db.sql(f"SELECT * FROM {t} WHERE id=?", (i,), uno=True)
    try:
        cols = [r[1] for r in db.sql(f"PRAGMA table_info({t})") if r[1] != "id"]
        if accion == "eliminar":
            db.sql(f"DELETE FROM {t} WHERE id=?", (i,), escribir=True)
            det = "fila eliminada: " + "; ".join(f"{c}={_nv(antes[c])}" for c in cols[:6]) if antes else "fila eliminada"
        else:
            nuevos = {c: request.form.get(c) or None for c in cols}
            db.sql(f"UPDATE {t} SET " + ", ".join(f"{c}=?" for c in cols) + " WHERE id=?", [nuevos[c] for c in cols] + [i], escribir=True)
            det = cambios(dict(antes) if antes else {}, nuevos, [(c, c) for c in cols])
        log(accion + " fila", "Base de datos", f"{t} #{i}")
        if det:
            log_edicion(accion + " fila (base de datos)", t, f"#{i}", det)
        flash("Hecho.", "ok")
    except sqlite3.Error as e:
        flash(f"No se pudo: {e}", "error")
    return redirect(url_for("base", t=t))


@app.route("/respaldo")
@acceso("superadmin")
def respaldo():
    log("descargó copia de seguridad", "Base de datos")
    return send_file(db.DB_PATH, as_attachment=True, download_name=f"inventario_flash_{hoy()}.db")


# ---------- Empresa, usuarios y roles ----------

@app.route("/configuracion", methods=["GET", "POST"])
@acceso("empresa")
def configuracion():
    if request.method == "POST":
        f = request.form
        try:
            float(f["stock_minimo"])
            assert int(f.get("dias_estadia", "60")) > 0
        except (ValueError, AssertionError):
            flash("El stock mínimo y los días estimados deben ser números válidos.", "error")
            return redirect(url_for("configuracion"))
        for k, d in (("empresa", "Inventario Flash"), ("moneda", "Q"), ("stock_minimo", "5"), ("dias_estadia", "60")):
            db.guardar_config(k, f[k].strip() or d)
        if f["tema"] in TEMAS:
            db.guardar_config("tema", f["tema"])
        for k in ("bajo", "agotado", "negativo"):
            db.guardar_config("notif_" + k, "1" if f.get("notif_" + k) else "0")
        log("modificó configuración", "Configuración")
        flash("Configuración guardada.", "ok")
        return redirect(url_for("configuracion"))
    return render_template("configuracion.html", TEMAS=TEMAS, minimo=cfg("stock_minimo", "5"), dias=cfg("dias_estadia", "60"), n={k: cfg("notif_" + k, "1") == "1" for k in ("bajo", "agotado", "negativo")})


@app.route("/usuarios", methods=["GET", "POST"])
@acceso("superadmin")
def usuarios():
    roles = [r["nombre"] for r in db.sql("SELECT nombre FROM roles ORDER BY nombre")]
    if request.method == "POST":
        f = request.form
        if not f["username"].strip() or len(f["password"]) < 6:
            flash("Usuario obligatorio y contraseña de 6 caracteres o más.", "error")
        elif f["rol"] not in roles + ["superadmin"]:
            flash("Rol no válido.", "error")
        else:
            try:
                db.crear_usuario(f["username"].strip(), f["password"], f["rol"], f["nombre"].strip())
                log("creó usuario", "Usuarios", f["username"].strip())
                flash("Usuario creado.", "ok")
            except sqlite3.IntegrityError:
                flash("Ese usuario ya existe.", "error")
        return redirect(url_for("usuarios"))
    return render_template("usuarios.html", lista=db.sql("SELECT * FROM usuarios ORDER BY username"), roles=roles, PERMISOS=PERMISOS,
                           perms={(r["rol"], r["permiso"]) for r in db.sql("SELECT * FROM permisos")})


@app.post("/usuarios/<int:uid>/editar")
@acceso("superadmin")
def usuario_editar(uid):
    f = request.form
    x = db.sql("SELECT * FROM usuarios WHERE id=?", (uid,), uno=True) or abort(404)
    activo = 1 if f.get("activo") else 0
    if x["username"] == session["user"]["username"] and (not activo or f["rol"] != "superadmin"):
        flash("No puedes desactivarte ni quitarte el rol de superadmin a ti mismo.", "error")
    elif f.get("password") and len(f["password"]) < 6:
        flash("La contraseña debe tener 6 caracteres o más.", "error")
    else:
        db.sql("UPDATE usuarios SET nombre_completo=?, rol=?, activo=? WHERE id=?", (f.get("nombre", "").strip(), f["rol"], activo, uid), escribir=True)
        if f.get("password"):
            db.sql("UPDATE usuarios SET password_hash=? WHERE id=?", (generar_hash(f["password"]), uid), escribir=True)
        log("editó usuario", "Usuarios", x["username"])
        flash("Usuario actualizado.", "ok")
    return redirect(url_for("usuarios"))


@app.post("/roles/guardar")
@acceso("superadmin")
def roles_guardar():
    f = request.form
    nuevo = f.get("nuevo_rol", "").strip().lower()
    if nuevo and nuevo != "superadmin":
        db.sql("INSERT OR IGNORE INTO roles VALUES (?)", (nuevo,), escribir=True)
    for r in db.sql("SELECT nombre FROM roles"):
        db.sql("DELETE FROM permisos WHERE rol=?", (r["nombre"],), escribir=True)
        for k in PERMISOS:
            if f.get(f"p_{r['nombre']}_{k}"):
                db.sql("INSERT INTO permisos VALUES (?, ?)", (r["nombre"], k), escribir=True)
    log("editó roles y permisos", "Usuarios")
    flash("Roles y permisos guardados.", "ok")
    return redirect(url_for("usuarios"))


@app.post("/roles/<nombre>/eliminar")
@acceso("superadmin")
def rol_eliminar(nombre):
    if db.sql("SELECT 1 FROM usuarios WHERE rol=?", (nombre,), uno=True):
        flash("No se puede eliminar: hay usuarios con ese rol.", "error")
    else:
        db.sql("DELETE FROM roles WHERE nombre=?", (nombre,), escribir=True)
        db.sql("DELETE FROM permisos WHERE rol=?", (nombre,), escribir=True)
        flash("Rol eliminado.", "ok")
    return redirect(url_for("usuarios"))


if __name__ == "__main__":
    app.run(debug=True)
