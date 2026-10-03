"""Motores de valuación de inventarios: PEPS, UEPS y Promedio Ponderado.

Cada función recibe los movimientos de un producto (ya ordenados por fecha) y devuelve las
filas del kardex. Cada fila trae 'idx' (posición del movimiento de origen).

En PEPS y UEPS una salida que consume varios lotes se parte en UNA FILA POR LOTE, cada una con
su cantidad, su costo unitario y su total (no se mezclan los precios en un promedio).
"""
from typing import Any, Dict, List, Sequence

Movimiento = Any  # sqlite3.Row o dict, accesible como m["campo"]
FilaKardex = Dict[str, Any]


def _fila(idx: int, fecha: str, tipo: str, cantidad: float, costo_unitario: float, costo_total: float,
          saldo_cantidad: float, saldo_valor: float) -> FilaKardex:
    return {"idx": idx, "fecha": fecha, "tipo": tipo, "cantidad": cantidad, "costo_unitario": round(costo_unitario, 2),
            "costo_total": round(costo_total, 2), "saldo_cantidad": round(saldo_cantidad, 2), "saldo_valor": round(saldo_valor, 2)}


def calcular_promedio_ponderado(movimientos: Sequence[Movimiento]) -> List[FilaKardex]:
    filas: List[FilaKardex] = []
    sc = sv = 0.0
    for i, m in enumerate(movimientos):
        cant: float = m["cantidad"]
        if m["tipo"] == "entrada":
            cu = m["costo_unitario"] or 0.0
            ct = cant * cu
            sc, sv = sc + cant, sv + ct
        else:
            cu = (sv / sc) if sc > 0 else 0.0
            ct = cant * cu
            sc, sv = sc - cant, sv - ct
        filas.append(_fila(i, m["fecha"], m["tipo"], cant, cu, ct, sc, sv))
    return filas


def _por_lotes(movimientos: Sequence[Movimiento], ueps: bool) -> List[FilaKardex]:
    filas: List[FilaKardex] = []
    lotes: List[List[float]] = []  # [cantidad_restante, costo_unitario]; PEPS toma del inicio, UEPS del final
    sc = sv = 0.0
    for i, m in enumerate(movimientos):
        cant: float = m["cantidad"]
        if m["tipo"] == "entrada":
            cu = m["costo_unitario"] or 0.0
            lotes.append([cant, cu])
            sc, sv = sc + cant, sv + cant * cu
            filas.append(_fila(i, m["fecha"], "entrada", cant, cu, cant * cu, sc, sv))
            continue
        falta = cant
        while falta > 1e-9 and lotes:
            pos = -1 if ueps else 0
            lote = lotes[pos]
            usar = min(lote[0], falta)
            ct = usar * lote[1]
            lote[0] -= usar
            falta -= usar
            sc, sv = sc - usar, sv - ct
            filas.append(_fila(i, m["fecha"], "salida", usar, lote[1], ct, sc, sv))  # una fila por lote consumido
            if lote[0] <= 1e-9:
                lotes.pop(pos)
        if falta > 1e-9:  # se sacó más de lo que había en lotes: queda a costo cero y la existencia en negativo
            sc -= falta
            filas.append(_fila(i, m["fecha"], "salida", falta, 0.0, 0.0, sc, sv))
    return filas


def calcular_peps(movimientos: Sequence[Movimiento]) -> List[FilaKardex]:
    return _por_lotes(movimientos, False)


def calcular_ueps(movimientos: Sequence[Movimiento]) -> List[FilaKardex]:
    return _por_lotes(movimientos, True)
