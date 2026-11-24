"""Validacion de las reglas R7 (enlace proforma-comprobante) y R9 (cobros reimputados).

Que los importes cuadren tras la limpieza no demuestra que cada documento este enlazado con el
correcto. Este script comprueba los enlaces uno a uno con informacion que las reglas NO usan,
mide cuanto cambian los indicadores con y sin cada regla y genera la cola de casos que debe
revisar una persona de la empresa.

Uso:  python scripts/validar_enlaces.py <carpeta_plata> <fichero_tipo_cambio> <carpeta_salida>
Lee la capa plata y no modifica nada.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PLATA, TIPO_CAMBIO, OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
OUT.mkdir(parents=True, exist_ok=True)
TOL = 0.02          # tolerancia de importe de R7 (la misma que en limpieza.py)
VENTANA = 60        # dias maximos entre la proforma y el comprobante en R7
TOL_SALDO = 0.05    # tolerancia entre el cobro y el saldo del comprobante de destino en R9
UMBRAL_TARDIO = 15

cab = pd.read_csv(PLATA / 'comprobantes_cabecera.csv', dtype=str).fillna('')
det = pd.read_csv(PLATA / 'comprobantes_detalle.csv', dtype=str)
pfc = pd.read_csv(PLATA / 'proformas_cabecera.csv', dtype=str).fillna('')
pfd = pd.read_csv(PLATA / 'proformas_detalle.csv', dtype=str)
cob = pd.read_csv(PLATA / 'cobros.csv', dtype=str).fillna('')
reg = pd.read_csv(PLATA / 'registro_limpieza.csv', dtype=str).fillna('')
tc = pd.read_csv(TIPO_CAMBIO, parse_dates=['FECHA']).set_index('FECHA').TC_VENTA_USD_PEN

cab['DOC'] = cab.SERIE + '-' + cab.NUMERO
cab['TOTAL'] = cab.TOTAL.astype(float)
cab['F'] = pd.to_datetime(cab.FECHA_EMISION)
cab['F_VENC'] = pd.to_datetime(cab.FECHA_VENCIMIENTO)
pfc['N'] = pfc.NUMERO.astype(int).astype(str)
pfc['TOTAL'] = pfc.TOTAL.astype(float)
pfc['F'] = pd.to_datetime(pfc.FECHA_EMISION)
det['DOC'] = det.SERIE + '-' + det.NUMERO
pfd['N'] = pfd.NUMERO.astype(int).astype(str)
cob['IMPORTE'] = cob.IMPORTE.astype(float)
cob['FECHA_COBRO'] = pd.to_datetime(cob.FECHA_COBRO)
CORTE = max(cab.F.max(), pfc.F.max())
ventas = cab[cab.TIPO_DOC != 'NOTA DE CREDITO']

# ======================================================================================
# R7. Enlace proforma - comprobante
# ======================================================================================
def lineas(df, clave):
    """Contenido de cada documento: conjunto de (producto, cantidad)."""
    return {k: frozenset(zip(g.CODIGO_PROD, g.CANTIDAD.astype(float))) for k, g in df.groupby(clave)}


lin_doc, lin_pf = lineas(det, 'DOC'), lineas(pfd, 'N')
enl = ventas[ventas.NUM_PROFORMA != ''].merge(
    pfc[['N', 'RUC_CLIENTE', 'MONEDA', 'TOTAL', 'F', 'VENDEDOR']], left_on='NUM_PROFORMA', right_on='N', suffixes=('', '_PF'))
enl['dias_proforma_a_venta'] = (enl.F - enl.F_PF).dt.days
enl['mismo_cliente'] = enl.RUC_CLIENTE == enl.RUC_CLIENTE_PF
enl['mismo_importe'] = (enl.TOTAL - enl.TOTAL_PF).abs() <= TOL
enl['mismas_lineas'] = [lin_doc.get(d) == lin_pf.get(n) for d, n in zip(enl.DOC, enl.N)]
enl['productos_de_la_proforma'] = [
    {p for p, _ in lin_doc.get(d, [])} <= {p for p, _ in lin_pf.get(n, [])} for d, n in zip(enl.DOC, enl.N)]
enl['mismo_vendedor'] = enl.VENDEDOR == enl.VENDEDOR_PF
reconstruido = enl.METODO_ENLACE.str.startswith('reconstruido')
# Un enlace reconstruido solo se da por bueno si ademas coincide el contenido, que la regla no usa
enl['confianza'] = np.select(
    [~reconstruido, reconstruido & enl.mismas_lineas, reconstruido & enl.productos_de_la_proforma],
    ['REFERENCIA ESCRITA EN EL COMPROBANTE', 'RECONSTRUIDO Y CORROBORADO (mismas lineas)',
     'RECONSTRUIDO, CORROBORACION PARCIAL: REVISAR'], 'RECONSTRUIDO SIN CORROBORAR: REVISAR')
enl[['DOC', 'ESTADO', 'N', 'METODO_ENLACE', 'confianza', 'dias_proforma_a_venta', 'mismo_cliente', 'mismo_importe',
     'mismas_lineas', 'productos_de_la_proforma', 'mismo_vendedor', 'TOTAL', 'TOTAL_PF']] \
    .rename(columns={'DOC': 'comprobante', 'N': 'num_proforma'}).to_csv(OUT / 'r7_enlaces_validados.csv', index=False)

resumen_r7 = enl.groupby('METODO_ENLACE').agg(
    enlaces=('DOC', 'size'), aceptados=('ESTADO', lambda s: (s == 'ACEPTADO').sum()), mismo_cliente=('mismo_cliente', 'sum'),
    mismo_importe=('mismo_importe', 'sum'), mismas_lineas=('mismas_lineas', 'sum'),
    productos_de_la_proforma=('productos_de_la_proforma', 'sum'), mismo_vendedor=('mismo_vendedor', 'sum'),
    dias_mediana=('dias_proforma_a_venta', 'median'), dias_maximo=('dias_proforma_a_venta', 'max')).reset_index()
resumen_r7.to_csv(OUT / 'r7_resumen_por_metodo.csv', index=False)

# Prueba de la regla sobre enlaces cuya respuesta se conoce: se aplica el criterio de R7
# (cliente + moneda + importe exacto + 0-60 dias) a las ventas que SI traian referencia escrita
# y se mira si el candidato que propone es la proforma que figura en la referencia.
con_ref = ventas[ventas.METODO_ENLACE.isin(['referencia', 'referencia_texto_libre'])]
cand = con_ref.merge(pfc[['N', 'RUC_CLIENTE', 'MONEDA', 'TOTAL', 'F']], on=['RUC_CLIENTE', 'MONEDA'], suffixes=('', '_PF'))
cand = cand[((cand.TOTAL - cand.TOTAL_PF).abs() <= TOL) & (cand.F - cand.F_PF).dt.days.between(0, VENTANA)]
cand['acierta'] = cand.NUM_PROFORMA == cand.N
por_doc = cand.groupby('DOC').agg(candidatos=('N', 'nunique'), acierta=('acierta', 'max'))
unico = por_doc[por_doc.candidatos == 1]
prueba_regla = pd.DataFrame([{
    'ventas_con_referencia_escrita': len(con_ref), 'regla_encuentra_candidato': len(por_doc),
    'sin_candidato (importe distinto: facturacion parcial o modificada)': len(con_ref) - len(por_doc),
    'candidato_unico': len(unico), 'candidato_unico_correcto': int(unico.acierta.sum()),
    'candidato_unico_incorrecto': int((~unico.acierta).sum()), 'varios_candidatos (la regla no asigna)': int((por_doc.candidatos > 1).sum()),
}]).T.reset_index()
prueba_regla.columns = ['concepto', 'valor']
prueba_regla.to_csv(OUT / 'r7_prueba_de_la_regla.csv', index=False)

# Efecto de R7 en la tasa de conversion: con y sin los enlaces reconstruidos
resueltas = pfc[pfc.ESTADO != 'ABIERTA']
conv_todos = set(enl[enl.ESTADO == 'ACEPTADO'].N)
conv_sin = set(enl[(enl.ESTADO == 'ACEPTADO') & ~reconstruido].N)
efecto_r7 = pd.DataFrame([
    {'escenario': 'Solo enlaces con referencia escrita', 'proformas_con_venta': len(conv_sin),
     'conversion_pct': round(resueltas.N.isin(conv_sin).mean() * 100, 2)},
    {'escenario': 'Con los enlaces reconstruidos (R7)', 'proformas_con_venta': len(conv_todos),
     'conversion_pct': round(resueltas.N.isin(conv_todos).mean() * 100, 2)}])
efecto_r7.to_csv(OUT / 'r7_efecto_en_conversion.csv', index=False)

# ======================================================================================
# R9. Cobros reimputados
# ======================================================================================
r9 = reg[reg.REGLA.str.startswith('R9 cobro reimputado')].set_index('CLAVE')
cob['NUMERO_ORIGINAL'] = cob.ID_COBRO.map(r9.VALOR_ORIGINAL).fillna(cob.NUMERO_DOC)
docs = ventas[ventas.ESTADO == 'ACEPTADO'].set_index('DOC')
nc = cab[(cab.TIPO_DOC == 'NOTA DE CREDITO') & (cab.ESTADO == 'ACEPTADO') & (cab.DOC_REF != '')].groupby('DOC_REF').TOTAL.sum()
cambio = docs.F.map(lambda f: tc.asof(f)).where(docs.MONEDA == 'USD', 1.0)


def situacion(columna_numero):
    """Saldo y retraso de cada comprobante segun a que numero se imputa cada cobro."""
    clave = cob.SERIE_DOC + '-' + cob[columna_numero]
    es_cobro = cob.TIPO_MOVIMIENTO == 'COBRO'
    d = docs[['RUC_CLIENTE', 'CLIENTE_NOMBRE', 'MONEDA', 'CONDICION_PAGO', 'F', 'F_VENC', 'TOTAL']].copy()
    d['saldo'] = (d.TOTAL - nc.reindex(d.index).fillna(0) - cob.groupby(clave).IMPORTE.sum().reindex(d.index).fillna(0)).round(2)
    d['saldo_pen'] = (d.saldo * cambio).round(2)
    d['ultimo_cobro'] = cob[es_cobro].groupby(clave[es_cobro]).FECHA_COBRO.max().reindex(d.index)
    d['pendiente'] = d.saldo > 0.5
    d['cobrado_de_mas'] = d.saldo < -0.5
    d['retraso_dias'] = np.where(d.pendiente, (CORTE - d.F_VENC).dt.days, (d.ultimo_cobro - d.F_VENC).dt.days)
    d['pago_tardio'] = (d.CONDICION_PAGO != 'CONTADO') & (d.retraso_dias > UMBRAL_TARDIO)
    return d


sin, con = situacion('NUMERO_ORIGINAL'), situacion('NUMERO_DOC')

# Detalle de cada reimputacion, con las condiciones que cumple
filas = []
for id_cobro, r in r9.iterrows():
    c = cob[cob.ID_COBRO == id_cobro].iloc[0]
    origen, destino = f'{c.SERIE_DOC}-{r.VALOR_ORIGINAL}', f'{c.SERIE_DOC}-{r.VALOR_NUEVO}'
    otros = cob[(cob.SERIE_DOC + '-' + cob.NUMERO_ORIGINAL == destino)]
    filas.append({
        'id_cobro': id_cobro, 'fecha_cobro': c.FECHA_COBRO.date(), 'importe': c.IMPORTE,
        'comprobante_original': origen, 'comprobante_asignado': destino,
        'cliente_original': docs.CLIENTE_NOMBRE.get(origen), 'cliente_asignado': docs.CLIENTE_NOMBRE.get(destino),
        'original_cobrado_de_mas_en': -sin.saldo.get(origen), 'saldo_del_asignado_antes': sin.saldo.get(destino),
        'exceso_igual_al_cobro': abs(-sin.saldo.get(origen) - c.IMPORTE) <= TOL_SALDO,
        'saldo_asignado_igual_al_cobro': abs(sin.saldo.get(destino) - c.IMPORTE) <= TOL_SALDO,
        'cobro_posterior_a_emision_del_asignado': c.FECHA_COBRO >= docs.F.get(destino),
        'otros_movimientos_previos_del_asignado': len(otros),
        'estado': 'PROPUESTA AUTOMATICA: pendiente de confirmar con el extracto bancario'})
detalle_r9 = pd.DataFrame(filas)
detalle_r9.to_csv(OUT / 'r9_reimputaciones.csv', index=False)


def indicadores(d, nombre):
    p = d[d.pendiente]
    credito = d[d.CONDICION_PAGO != 'CONTADO']
    return {'escenario': nombre, 'documentos_pendientes': len(p), 'cartera_pendiente_pen': round(p.saldo_pen.sum(), 2),
            'cartera_vencida_mas_90d_pen': round(p[(CORTE - p.F_VENC).dt.days > 90].saldo_pen.sum(), 2),
            'clientes_con_deuda': p.RUC_CLIENTE.nunique(), 'comprobantes_cobrados_de_mas': int(d.cobrado_de_mas.sum()),
            'importe_cobrado_de_mas_pen': round(-d[d.cobrado_de_mas].saldo_pen.sum(), 2),
            'facturas_credito_pago_tardio': int(credito.pago_tardio.sum())}


efecto_r9 = pd.DataFrame([indicadores(sin, 'Sin R9 (cobros como se registraron)'), indicadores(con, 'Con R9 (cobros reimputados)')])
efecto_r9.to_csv(OUT / 'r9_efecto_en_indicadores.csv', index=False)

cambios = sin.join(con[['saldo_pen', 'pendiente', 'retraso_dias', 'pago_tardio']], rsuffix='_con_r9')
cambios = cambios[cambios.saldo_pen != cambios.saldo_pen_con_r9]
por_cliente = pd.concat([sin[sin.pendiente].groupby(['RUC_CLIENTE', 'CLIENTE_NOMBRE']).saldo_pen.sum().rename('deuda_sin_r9_pen'),
                         con[con.pendiente].groupby(['RUC_CLIENTE', 'CLIENTE_NOMBRE']).saldo_pen.sum().rename('deuda_con_r9_pen')],
                        axis=1).fillna(0)
por_cliente['diferencia_pen'] = (por_cliente.deuda_con_r9_pen - por_cliente.deuda_sin_r9_pen).round(2)
por_cliente = por_cliente[por_cliente.diferencia_pen.abs() > 0.5].reset_index().sort_values('diferencia_pen')
por_cliente.to_csv(OUT / 'r9_efecto_por_cliente.csv', index=False)

# ======================================================================================
# Cola de revision humana: todo lo que las reglas no resuelven solas o conviene confirmar
# ======================================================================================
cola = []
for _, r in reg[reg.REGLA.str.contains('R7 enlace ambiguo')].iterrows():
    cola.append({'tipo': 'R7 venta con varias proformas posibles', 'documento': r.CLAVE, 'accion': 'Indicar la proforma correcta', 'detalle': 'No se asigna enlace'})
for _, r in reg[reg.REGLA.str.contains('R7 NC sin documento')].iterrows():
    cola.append({'tipo': 'R7 nota de credito sin comprobante de referencia', 'documento': r.CLAVE, 'accion': 'Indicar el comprobante que corrige', 'detalle': r.REGLA})
for _, r in enl[enl.confianza.str.endswith('REVISAR')].iterrows():
    cola.append({'tipo': 'R7 enlace reconstruido sin corroborar', 'documento': r.DOC, 'accion': 'Confirmar la proforma', 'detalle': f'Proforma {r.N}'})
for _, r in pfc[pfc.REVISAR != ''].iterrows():
    cola.append({'tipo': 'R8 proforma marcada como cerrada sin venta localizada', 'documento': f'{r.SERIE}-{r.NUMERO}', 'accion': 'Indicar la venta o corregir el estado', 'detalle': r.REVISAR})
for _, r in detalle_r9.iterrows():
    cola.append({'tipo': 'R9 cobro reimputado', 'documento': r.id_cobro, 'accion': 'Confirmar con el extracto bancario',
                 'detalle': f'{r.comprobante_original} -> {r.comprobante_asignado}, {r.importe:.2f}'})
for _, r in cob[cob.REVISAR != ''].iterrows():
    cola.append({'tipo': 'R9 cobro dudoso sin reimputar', 'documento': r.ID_COBRO, 'accion': 'Revisar', 'detalle': r.REVISAR})
cola = pd.DataFrame(cola)
cola['decision_de_la_empresa'] = ''
cola.to_csv(OUT / 'cola_revision.csv', index=False)

# ======================================================================================
print('R7. ENLACES PROFORMA - COMPROBANTE')
print(resumen_r7.to_string(index=False))
print(enl.confianza.value_counts().to_string())
print(prueba_regla.to_string(index=False))
print(efecto_r7.to_string(index=False))
print('\nR9. COBROS REIMPUTADOS')
print(f'{len(detalle_r9)} reimputaciones; cumplen las tres condiciones: '
      f'{int((detalle_r9.exceso_igual_al_cobro & detalle_r9.saldo_asignado_igual_al_cobro & detalle_r9.cobro_posterior_a_emision_del_asignado).sum())}')
print(efecto_r9.T.to_string())
print(f'Documentos cuyo saldo cambia: {len(cambios)}; clientes distintos: {cambios.RUC_CLIENTE.nunique()}; '
      f'cambian de pendiente a cobrado: {int((cambios.pendiente & ~cambios.pendiente_con_r9).sum())}; '
      f'cambia la etiqueta de pago tardio: {int((cambios.pago_tardio != cambios.pago_tardio_con_r9).sum())}; '
      f'clientes con deuda distinta: {len(por_cliente)}')
print('\nCOLA DE REVISION')
print(cola.tipo.value_counts().to_string())
