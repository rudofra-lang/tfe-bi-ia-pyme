#!/usr/bin/env python3
"""
Capa plata: limpieza e integracion de las fuentes.

Aplica, en orden, las nueve reglas del apartado "Implicaciones para el ETL" del informe
de auditoria. No borra informacion: cada valor que cambia queda en registro_limpieza.csv
con su valor original, y los casos que no se pueden resolver con seguridad se marcan
para revision en lugar de corregirse.

Uso: python limpieza.py <carpeta_fuentes> <carpeta_plata>
"""
import json
import os
import re
import sys
from collections import Counter

import numpy as np
import pandas as pd

SRC = sys.argv[1] if len(sys.argv) > 1 else 'fuentes'
OUT = sys.argv[2] if len(sys.argv) > 2 else 'plata'
os.makedirs(OUT, exist_ok=True)

ESQUEMA_COMPROBANTES = ['TIPO_DOC', 'SERIE', 'NUMERO', 'FECHA_EMISION', 'FECHA_VENCIMIENTO', 'RUC_CLIENTE',
                        'CLIENTE_NOMBRE', 'MONEDA', 'CONDICION_PAGO', 'SUBTOTAL', 'IGV', 'TOTAL', 'ESTADO',
                        'VENDEDOR', 'USUARIO', 'REFERENCIA', 'MOTIVO_NC']
MAPEO_2026 = {'F. EMISION': 'FECHA_EMISION', 'F. VENCIMIENTO': 'FECHA_VENCIMIENTO',
              'NRO DOC CLIENTE': 'RUC_CLIENTE', 'RAZON SOCIAL': 'CLIENTE_NOMBRE'}
VACIO = ['', '-', 'NULL', 'N/A']
TOL = 0.02  # tolerancia de redondeo en importes (soles)

# --------------------------------------------------------------------------------------
# Registro de cambios
# --------------------------------------------------------------------------------------
LOG = []


def registrar(tabla, claves, campo, regla, antes, despues):
    """Anota cada valor que cambia. claves, antes y despues son Series alineadas."""
    antes = pd.Series(antes).astype(str).reset_index(drop=True)
    despues = pd.Series(despues).astype(str).reset_index(drop=True)
    claves = pd.Series(claves).astype(str).reset_index(drop=True)
    distinto = antes != despues
    for k, a, d in zip(claves[distinto], antes[distinto], despues[distinto]):
        LOG.append(dict(TABLA=tabla, CLAVE=k, CAMPO=campo, REGLA=regla, VALOR_ORIGINAL=a, VALOR_NUEVO=d))


def registrar_uno(tabla, clave, campo, regla, antes, despues):
    LOG.append(dict(TABLA=tabla, CLAVE=str(clave), CAMPO=campo, REGLA=regla,
                    VALOR_ORIGINAL=str(antes), VALOR_NUEVO=str(despues)))


# --------------------------------------------------------------------------------------
# R1. Lectura por version de exportacion
# --------------------------------------------------------------------------------------
def leer(nombre):
    """Detecta la fila de cabecera y el separador; lee todo como texto."""
    ruta = os.path.join(SRC, nombre)
    with open(ruta, encoding='utf-8') as f:
        lineas = [f.readline() for _ in range(10)]
    fila, sep = 0, ','
    for i, linea in enumerate(lineas):
        if linea.count(';') >= 4:
            fila, sep = i, ';'
            break
        if linea.count(',') >= 4:
            fila, sep = i, ','
            break
    df = pd.read_csv(ruta, sep=sep, skiprows=fila, dtype=str, keep_default_na=False)
    return df, sep


def a_numero(s, decimal):
    s = s.astype(str)
    if decimal == ',':
        s = s.str.replace('.', '', regex=False).str.replace(',', '.', regex=False)
    return pd.to_numeric(s, errors='coerce')


# --------------------------------------------------------------------------------------
# R2. Fechas
# --------------------------------------------------------------------------------------
def a_fecha(s, orden='dmy'):
    """aaaa-mm-dd, aaaa-mm-dd hh:mm:ss o dd/mm/aaaa (orden fijo para todo el fichero)."""
    s = s.astype(str)
    iso = pd.to_datetime(s.where(s.str.match(r'^\d{4}-\d{2}-\d{2}')).str[:10], format='%Y-%m-%d', errors='coerce')
    fmt = '%d/%m/%Y' if orden == 'dmy' else '%m/%d/%Y'
    barra = pd.to_datetime(s.where(s.str.match(r'^\d{2}/\d{2}/\d{4}$')), format=fmt, errors='coerce')
    return iso.fillna(barra)


def orden_fichero(s):
    """Decide si un fichero con fechas dd/mm o mm/dd usa dia primero, mirando los valores > 12."""
    p = s[s.str.match(r'^\d{2}/\d{2}/\d{4}$')].str.split('/', expand=True)
    if p.empty:
        return 'dmy'
    dia_primero = (p[0].astype(int) > 12).sum()
    mes_primero = (p[1].astype(int) > 12).sum()
    return 'dmy' if dia_primero >= mes_primero else 'mdy'


def fecha_proforma(txt, validez):
    """Fechas de proforma con formato mixto. Las ambiguas se resuelven con la validez,
    que en el sistema es siempre la emision + 7, 15 o 30 dias."""
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?', txt):
        return pd.Timestamp(txt[:10]), 'iso'
    m = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})', txt)
    if not m:
        return pd.NaT, 'ilegible'
    a, b, y = map(int, m.groups())
    candidatos = []
    for dia, mes, etiqueta in ((a, b, 'dd/mm'), (b, a, 'mm/dd')):
        try:
            candidatos.append((pd.Timestamp(y, mes, dia), etiqueta))
        except ValueError:
            pass
    if len({c[0] for c in candidatos}) == 1:
        return candidatos[0][0], candidatos[0][1]
    validos = [c for c in candidatos if (validez - c[0]).days in (7, 15, 30)]
    if len({c[0] for c in validos}) == 1:
        return validos[0][0], validos[0][1] + ' (por validez)'
    return candidatos[0][0], 'ambigua_no_resuelta'


def iso(s):
    return pd.to_datetime(s).dt.strftime('%Y-%m-%d')


# --------------------------------------------------------------------------------------
# R3. Identificadores
# --------------------------------------------------------------------------------------
def norm_id(x):
    """DNI = 8 cifras. Un identificador numerico de menos de 8 cifras es un DNI que perdio
    los ceros iniciales. Los RUC empiezan por 10/15/17/20 y no pueden perderlos."""
    x = str(x).strip()
    if x.isdigit() and len(x) < 8:
        return x.zfill(8)
    return x


# --------------------------------------------------------------------------------------
# R5. Normalizacion de codigos y catalogos
# --------------------------------------------------------------------------------------
def norm_producto(x):
    x = str(x).strip().upper()
    m = re.fullmatch(r'PRD-?(\d+)', x)
    return f'PRD-{int(m.group(1)):04d}' if m else x


MONEDAS = {'PEN': 'PEN', 'S/': 'PEN', 'S/.': 'PEN', 'SOLES': 'PEN', 'USD': 'USD', 'US$': 'USD', 'DOLARES': 'USD', '$': 'USD'}


def norm_medio(x):
    x = re.sub(r'\s+', ' ', str(x).strip().upper())
    x = re.sub(r'^TRANSF(?:\.\s*|\s+)', 'TRANSFERENCIA ', x)
    x = re.sub(r'\bIBK\b', 'INTERBANK', x)
    if x.rstrip('.') in ('EFECT', 'EFEC'):
        x = 'EFECTIVO'
    return x


def norm_vendedor(serie):
    """'A. APELLIDO' -> el unico nombre completo con esa inicial y ese apellido."""
    completos = sorted({v for v in serie.unique() if '.' not in v})
    out = {}
    for v in serie.unique():
        m = re.fullmatch(r'([A-Z])\.\s*(.+)', v)
        if not m:
            out[v] = v
            continue
        ini, ape = m.groups()
        c = [n for n in completos if n.split()[0].startswith(ini) and n.split()[-1] == ape.strip()]
        out[v] = c[0] if len(c) == 1 else v
    return serie.map(out)


# ======================================================================================
# CLIENTES Y PRODUCTOS
# ======================================================================================
cli, _ = leer('clientes.csv')
prod, _ = leer('productos.csv')

orig = cli.RUC_DNI.copy()
cli['RUC_DNI'] = cli.RUC_DNI.map(norm_id)
registrar('clientes', orig, 'RUC_DNI', 'R3 ceros iniciales', orig, cli.RUC_DNI)
cli['TIPO_DOC_ID'] = np.where(cli.RUC_DNI.str.len() == 11, 'RUC', 'DNI')

for c in ('DEP', 'PROV'):
    vac = cli[c].isin(VACIO)
    registrar('clientes', cli.RUC_DNI[vac], c, 'R3 ubicacion desconocida', cli[c][vac], [''] * vac.sum())
    cli.loc[vac, c] = ''

# ======================================================================================
# COMPROBANTES: lectura, union y deduplicacion
# ======================================================================================
partes = []
for f in sorted(os.listdir(SRC)):
    if not re.fullmatch(r'comprobantes_\d{4}\.csv', f):
        continue
    d, sep = leer(f)
    d = d.rename(columns=MAPEO_2026)
    extra = [c for c in d.columns if c not in ESQUEMA_COMPROBANTES]
    for c in extra:
        if (d[c] != '').any():
            registrar_uno('comprobantes', f, c, 'R1 columna adicional con datos (descartada)', 'con datos', '')
    d = d[ESQUEMA_COMPROBANTES].copy()
    decimal = ',' if sep == ';' else '.'
    for c in ('SUBTOTAL', 'IGV', 'TOTAL'):
        d[c] = a_numero(d[c], decimal)
    for c in ('FECHA_EMISION', 'FECHA_VENCIMIENTO'):
        orden = orden_fichero(d[c])
        d[c] = a_fecha(d[c], orden)
    d['FICHERO_ORIGEN'] = f
    d['ANIO_FICHERO'] = int(f[13:17])
    partes.append(d)
cab = pd.concat(partes, ignore_index=True)
cab['CLAVE'] = cab.SERIE + '-' + cab.NUMERO

# Conciliacion: importes tal como llegan, para comprobar al final que la limpieza solo
# los cambia por motivos registrados
def moneda_iso(s):
    return s.str.upper().map(MONEDAS).fillna(s)


AJ = Counter()  # (tabla, concepto, moneda) -> importe
for mon, v in cab.TOTAL.groupby(moneda_iso(cab.MONEDA)).sum().items():
    AJ[('comprobantes', 'bruto', mon)] += v
FILAS_BRUTO = {'comprobantes': len(cab)}

# R4. Comprobantes repetidos entre exportaciones: se conserva la copia del fichero de su ano
cab['_propio'] = (cab.FECHA_EMISION.dt.year == cab.ANIO_FICHERO).astype(int)
cab = cab.sort_values(['CLAVE', '_propio'], ascending=[True, False])
rep = cab[cab.CLAVE.duplicated(keep='first')]
for _, r in rep.iterrows():
    registrar_uno('comprobantes', r.CLAVE, '(fila)', 'R4 repetido entre exportaciones (eliminada copia)',
                  r.FICHERO_ORIGEN, 'eliminada')
for mon, v in rep.TOTAL.groupby(moneda_iso(rep.MONEDA)).sum().items():
    AJ[('comprobantes', 'duplicados eliminados', mon)] -= v
cab = cab.drop_duplicates('CLAVE', keep='first').drop(columns=['_propio'])

# R3. Identificador y razon social del cliente
orig = cab.RUC_CLIENTE.copy()
cab['RUC_CLIENTE'] = cab.RUC_CLIENTE.map(norm_id)
registrar('comprobantes', cab.CLAVE, 'RUC_CLIENTE', 'R3 ceros iniciales', orig, cab.RUC_CLIENTE)

# Maestro: CLIENTES VARIOS y clientes dados de alta dos veces
if '00000000' not in set(cli.RUC_DNI):
    varios = cab[cab.RUC_CLIENTE == '00000000']
    if len(varios):
        cli = pd.concat([cli, pd.DataFrame([dict(RUC_DNI='00000000', TIPO_DOC_ID='DNI', RAZON_SOCIAL='CLIENTES VARIOS',
                                                 TIPO='PERSONA NATURAL', SECTOR='PERSONA')])], ignore_index=True).fillna('')
        registrar_uno('clientes', '00000000', '(fila)', 'R3 cliente generico CLIENTES VARIOS', '', 'anadido')
nombre_frecuente = cab.groupby('RUC_CLIENTE').CLIENTE_NOMBRE.agg(lambda s: s.mode()[0])
filas = []
for ruc, g in cli.groupby('RUC_DNI', sort=False):
    if len(g) == 1:
        filas.append(g.iloc[0])
        continue
    g = g.sort_values('FECHA_ALTA')
    elegido = g.iloc[0].copy()
    moda = nombre_frecuente.get(ruc)
    if moda in set(g.RAZON_SOCIAL):
        elegido['RAZON_SOCIAL'] = moda
    for c in ('DEP', 'PROV', 'SECTOR', 'CREDITO', 'VENDEDOR_ASIGNADO'):
        if c in g and elegido[c] == '':
            no_vacios = g[c][g[c] != '']
            if len(no_vacios):
                elegido[c] = no_vacios.iloc[0]
    for _, r in g.iterrows():
        if r.RAZON_SOCIAL != elegido.RAZON_SOCIAL or r.FECHA_ALTA != elegido.FECHA_ALTA:
            registrar_uno('clientes', ruc, '(fila)', 'R4 cliente duplicado (fusionado)', r.RAZON_SOCIAL, elegido.RAZON_SOCIAL)
    filas.append(elegido)
cli = pd.DataFrame(filas).reset_index(drop=True)
cli.loc[cli.RUC_DNI == '00000000', 'RAZON_SOCIAL'] = 'CLIENTES VARIOS'

# La razon social del comprobante se toma del maestro
orig = cab.CLIENTE_NOMBRE.copy()
mapa = cli.set_index('RUC_DNI').RAZON_SOCIAL
cab['CLIENTE_NOMBRE'] = cab.RUC_CLIENTE.map(mapa).fillna(cab.CLIENTE_NOMBRE)
registrar('comprobantes', cab.CLAVE, 'CLIENTE_NOMBRE', 'R5 razon social del maestro', orig, cab.CLIENTE_NOMBRE)

# R5. Moneda y vendedor
orig = cab.MONEDA.copy()
cab['MONEDA'] = cab.MONEDA.str.upper().map(MONEDAS).fillna(cab.MONEDA)
registrar('comprobantes', cab.CLAVE, 'MONEDA', 'R5 moneda ISO 4217', orig, cab.MONEDA)
orig = cab.VENDEDOR.copy()
cab['VENDEDOR'] = norm_vendedor(cab.VENDEDOR)
registrar('comprobantes', cab.CLAVE, 'VENDEDOR', 'R5 vendedor nombre completo', orig, cab.VENDEDOR)

# R6. Signo de las notas de credito: importe positivo; el signo lo da el tipo de documento
es_nc = cab.TIPO_DOC == 'NOTA DE CREDITO'
for c in ('SUBTOTAL', 'IGV', 'TOTAL'):
    orig = cab[c].copy()
    cab.loc[es_nc, c] = cab.loc[es_nc, c].abs()
    registrar('comprobantes', cab.CLAVE, c, 'R6 signo de nota de credito', orig, cab[c])
    if c == 'TOTAL':
        for mon, v in (cab[c] - orig).groupby(cab.MONEDA).sum().items():
            AJ[('comprobantes', 'signo de notas de credito', mon)] += v

# ======================================================================================
# DETALLE DE COMPROBANTES
# ======================================================================================
det, _ = leer('comprobantes_detalle.csv')
det['CLAVE'] = det.SERIE + '-' + det.NUMERO + '/' + det.ITEM
AJ[('comprobantes_detalle', 'bruto', '-')] += pd.to_numeric(det.TOTAL_LINEA).sum()
FILAS_BRUTO['comprobantes_detalle'] = len(det)
orig = det.CODIGO_PROD.copy()
det['CODIGO_PROD'] = det.CODIGO_PROD.map(norm_producto)
registrar('comprobantes_detalle', det.CLAVE, 'CODIGO_PROD', 'R5 codigo de producto', orig, det.CODIGO_PROD)
rep = det[det.duplicated(['SERIE', 'NUMERO', 'ITEM'], keep='first')]
for k in rep.CLAVE:
    registrar_uno('comprobantes_detalle', k, '(fila)', 'R4 linea repetida (eliminada)', 'repetida', 'eliminada')
AJ[('comprobantes_detalle', 'duplicados eliminados', '-')] -= pd.to_numeric(rep.TOTAL_LINEA).sum()
det = det.drop_duplicates(['SERIE', 'NUMERO', 'ITEM'], keep='first')
for c in ('CANTIDAD', 'PRECIO_UNITARIO', 'DESCUENTO', 'TOTAL_LINEA', 'COSTO_UNITARIO'):
    det[c] = pd.to_numeric(det[c], errors='coerce')
det['DOC'] = det.SERIE + '-' + det.NUMERO

# Total retocado a mano: si las lineas y el IGV cuadran, el total se recalcula
suma = det.groupby('DOC').TOTAL_LINEA.sum().round(2)
c_idx = cab.set_index('CLAVE')
descuadre = (c_idx.SUBTOTAL + c_idx.IGV - c_idx.TOTAL).abs() > TOL
lineas_ok = (c_idx.SUBTOTAL - suma.reindex(c_idx.index)).abs() <= TOL
igv_ok = (c_idx.SUBTOTAL * 0.18 - c_idx.IGV).abs() <= TOL
arreglar = descuadre & lineas_ok & igv_ok
for k in c_idx.index[arreglar]:
    nuevo = round(c_idx.at[k, 'SUBTOTAL'] + c_idx.at[k, 'IGV'], 2)
    registrar_uno('comprobantes', k, 'TOTAL', 'R5 total recalculado (subtotal + IGV)', c_idx.at[k, 'TOTAL'], nuevo)
    AJ[('comprobantes', 'totales recalculados', c_idx.at[k, 'MONEDA'])] += nuevo - c_idx.at[k, 'TOTAL']
    cab.loc[cab.CLAVE == k, 'TOTAL'] = nuevo

# ======================================================================================
# PROFORMAS
# ======================================================================================
pfc, _ = leer('proformas_cabecera.csv')
pfd, _ = leer('proformas_detalle.csv')
pfc['CLAVE'] = pfc.SERIE + '-' + pfc.NUMERO
pfc['FECHA_VALIDEZ'] = a_fecha(pfc.FECHA_VALIDEZ)
res = [fecha_proforma(t, v) for t, v in zip(pfc.FECHA_EMISION, pfc.FECHA_VALIDEZ)]
orig = pfc.FECHA_EMISION.copy()
pfc['FECHA_EMISION'] = [r[0] for r in res]
pfc['_formato'] = [r[1] for r in res]
for k, a, n, fmt in zip(pfc.CLAVE, orig, pfc.FECHA_EMISION, pfc._formato):
    if fmt != 'iso' or len(a) > 10:
        regla = 'R2 fecha ambigua NO resuelta (se asume dd/mm)' if fmt == 'ambigua_no_resuelta' else f'R2 fecha a aaaa-mm-dd ({fmt})'
        registrar_uno('proformas', k, 'FECHA_EMISION', regla, a, n.strftime('%Y-%m-%d'))
orig = pfc.RUC_CLIENTE.copy()
pfc['RUC_CLIENTE'] = pfc.RUC_CLIENTE.map(norm_id)
registrar('proformas', pfc.CLAVE, 'RUC_CLIENTE', 'R3 ceros iniciales', orig, pfc.RUC_CLIENTE)
for c in ('SUBTOTAL', 'IGV', 'TOTAL'):
    pfc[c] = pd.to_numeric(pfc[c], errors='coerce')
orig = pfc.MONEDA.copy()
pfc['MONEDA'] = pfc.MONEDA.str.upper().map(MONEDAS).fillna(pfc.MONEDA)
registrar('proformas', pfc.CLAVE, 'MONEDA', 'R5 moneda ISO 4217', orig, pfc.MONEDA)
pfc['VENDEDOR'] = norm_vendedor(pfc.VENDEDOR)
pfd['CODIGO_PROD'] = pfd.CODIGO_PROD.map(norm_producto)
pfd = pfd.drop_duplicates(['SERIE', 'NUMERO', 'ITEM'])
for c in ('CANTIDAD', 'PRECIO_UNITARIO', 'DESCUENTO', 'TOTAL_LINEA', 'COSTO_UNITARIO'):
    pfd[c] = pd.to_numeric(pfd[c], errors='coerce')

# ======================================================================================
# R7. Enlace proforma - comprobante
# ======================================================================================
pf_num = pfc.set_index(pfc.NUMERO.astype(int))
cab['NUM_PROFORMA'] = pd.array([pd.NA] * len(cab), dtype='Int64')
cab['DOC_REF'] = ''
cab['METODO_ENLACE'] = ''
cab['REFERENCIA_ORIGINAL'] = cab.REFERENCIA

RE_DOC = re.compile(r'(F[TC]\d{2}|B[0C]\d{2})-(\d+)')
RE_PF = [re.compile(p) for p in (r'PF01-0*(\d+)', r'COTIZ\.?\s*N?\.?\s*(\d+)', r'PROFORMA\s*N[°ºO.]?\s*(\d+)', r'\b(\d{4,8})\b')]
for i, r in cab.iterrows():
    ref = r.REFERENCIA.upper()
    if not ref:
        continue
    if r.TIPO_DOC == 'NOTA DE CREDITO':
        m = RE_DOC.search(ref)
        if m:
            cab.at[i, 'DOC_REF'] = f'{m.group(1)}-{m.group(2)}'
        continue
    for j, rx in enumerate(RE_PF):
        m = rx.search(ref)
        if m:
            n = int(m.group(1))
            if n in pf_num.index and pf_num.at[n, 'RUC_CLIENTE'] == r.RUC_CLIENTE:
                cab.at[i, 'NUM_PROFORMA'] = n
                cab.at[i, 'METODO_ENLACE'] = 'referencia' if (j == 0 and ref.startswith('[PROFORMA')) else 'referencia_texto_libre'
            break

# Facturas y boletas sin referencia: emparejamiento por cliente + importe exacto + fecha (0-60 dias)
usadas = set(cab.NUM_PROFORMA.dropna().astype(int))
libres = pfc[pfc.ESTADO.isin(['CERRADA', 'ABIERTA']) & ~pfc.NUMERO.astype(int).isin(usadas)].copy()
libres['N'] = libres.NUMERO.astype(int)
sin = cab[(cab.TIPO_DOC != 'NOTA DE CREDITO') & cab.NUM_PROFORMA.isna() & (cab.RUC_CLIENTE != '00000000')]
m = sin.reset_index().merge(libres[['N', 'RUC_CLIENTE', 'MONEDA', 'TOTAL', 'FECHA_EMISION']],
                            on=['RUC_CLIENTE', 'MONEDA'], suffixes=('', '_PF'))
m = m[((m.TOTAL - m.TOTAL_PF).abs() <= TOL) & (m.FECHA_EMISION - m.FECHA_EMISION_PF).dt.days.between(0, 60)]
por_doc = m.groupby('index').N.nunique()
por_pf = m.groupby('N')['index'].nunique()
unicos = m[m['index'].map(por_doc).eq(1) & m.N.map(por_pf).eq(1)]
for _, r in unicos.iterrows():
    cab.at[r['index'], 'NUM_PROFORMA'] = r.N
    cab.at[r['index'], 'METODO_ENLACE'] = 'reconstruido (cliente+importe+fecha)'
    registrar_uno('comprobantes', r.CLAVE, 'NUM_PROFORMA', 'R7 enlace reconstruido', '', r.N)
for k in m[~m.index.isin(unicos.index)].CLAVE.unique():
    registrar_uno('comprobantes', k, 'NUM_PROFORMA', 'R7 enlace ambiguo (sin asignar)', '', 'REVISAR')

# Notas de credito sin documento de referencia: candidato unico del mismo cliente y moneda,
# emitido hasta 60 dias antes y con total >= al de la nota
nc_sin = cab[(cab.TIPO_DOC == 'NOTA DE CREDITO') & (cab.DOC_REF == '')]
ventas = cab[(cab.TIPO_DOC != 'NOTA DE CREDITO') & (cab.ESTADO == 'ACEPTADO')]
for i, r in nc_sin.iterrows():
    serie_ok = ('F' if r.SERIE.startswith('F') else 'B')
    c = ventas[(ventas.RUC_CLIENTE == r.RUC_CLIENTE) & (ventas.MONEDA == r.MONEDA) & ventas.SERIE.str.startswith(serie_ok) &
               (r.FECHA_EMISION - ventas.FECHA_EMISION).dt.days.between(0, 60) & (ventas.TOTAL >= r.TOTAL - TOL)]
    if r.MOTIVO_NC == 'ANULACION DE LA OPERACION':
        c = c[(c.TOTAL - r.TOTAL).abs() <= TOL]
    if len(c) == 1:
        cab.at[i, 'DOC_REF'] = c.iloc[0].CLAVE
        registrar_uno('comprobantes', r.CLAVE, 'DOC_REF', 'R7 documento de la NC reconstruido', '', c.iloc[0].CLAVE)
    else:
        registrar_uno('comprobantes', r.CLAVE, 'DOC_REF', f'R7 NC sin documento ({len(c)} candidatos)', '', 'REVISAR')

# Referencia normalizada
cab['REFERENCIA'] = np.where(cab.NUM_PROFORMA.notna(),
                             '[PROFORMA : PF01-' + cab.NUM_PROFORMA.astype('string').str.zfill(8) + ']',
                             np.where(cab.DOC_REF != '', '[DOC. REF. : ' + cab.DOC_REF + ']', ''))
cab['REFERENCIA'] = cab.REFERENCIA.fillna('')

# ======================================================================================
# R8. Estado derivado de las proformas
# ======================================================================================
corte = max(cab.FECHA_EMISION.max(), pfc.FECHA_EMISION.max())
vinculadas = set(cab[(cab.ESTADO == 'ACEPTADO') & cab.NUM_PROFORMA.notna()].NUM_PROFORMA.astype(int))
pfc['ESTADO_ORIGINAL'] = pfc.ESTADO
n = pfc.NUMERO.astype(int)
pfc['ESTADO'] = np.select(
    [n.isin(vinculadas), pfc.ESTADO_ORIGINAL == 'RECHAZADA', pfc.ESTADO_ORIGINAL == 'CERRADA',
     pfc.FECHA_VALIDEZ < corte],
    ['CERRADA', 'RECHAZADA', 'CERRADA', 'VENCIDA'], default='ABIERTA')
registrar('proformas', pfc.CLAVE, 'ESTADO', 'R8 estado derivado (enlace y validez)', pfc.ESTADO_ORIGINAL, pfc.ESTADO)
pfc['REVISAR'] = np.where((pfc.ESTADO_ORIGINAL == 'CERRADA') & ~n.isin(vinculadas), 'cerrada sin venta localizada', '')

# ======================================================================================
# R9. Cobros
# ======================================================================================
cob, _ = leer('cobros.csv')
cob['IMPORTE'] = pd.to_numeric(cob.IMPORTE, errors='coerce')
cob['FECHA_COBRO'] = a_fecha(cob.FECHA_COBRO)
orig = cob.MEDIO_PAGO.copy()
cob['MEDIO_PAGO'] = cob.MEDIO_PAGO.map(norm_medio)
registrar('cobros', cob.ID_COBRO, 'MEDIO_PAGO', 'R5 medio de pago', orig, cob.MEDIO_PAGO)
orig = cob.MONEDA.copy()
cob['MONEDA'] = cob.MONEDA.str.upper().map(MONEDAS).fillna(cob.MONEDA)
registrar('cobros', cob.ID_COBRO, 'MONEDA', 'R5 moneda ISO 4217', orig, cob.MONEDA)
for mon, v in cob.IMPORTE.groupby(cob.MONEDA).sum().items():
    AJ[('cobros', 'bruto', mon)] += v
FILAS_BRUTO['cobros'] = len(cob)

# R4. Cobros registrados dos veces (mismo contenido, distinto ID): se conserva el primero
contenido = ['SERIE_DOC', 'NUMERO_DOC', 'FECHA_COBRO', 'MONEDA', 'IMPORTE', 'MEDIO_PAGO', 'TIPO_MOVIMIENTO']
cob = cob.sort_values('ID_COBRO')
dup = cob[cob.duplicated(contenido, keep='first')]
for k in dup.ID_COBRO:
    registrar_uno('cobros', k, '(fila)', 'R4 cobro registrado dos veces (eliminado)', 'duplicado', 'eliminado')
for mon, v in dup.IMPORTE.groupby(dup.MONEDA).sum().items():
    AJ[('cobros', 'duplicados eliminados', mon)] -= v
cob = cob.drop_duplicates(contenido, keep='first')

# Cobros imputados a un comprobante equivocado (numero mal tecleado)
cob['DOC'] = cob.SERIE_DOC + '-' + cob.NUMERO_DOC
cob['REVISAR'] = ''
docs = cab[cab.TIPO_DOC != 'NOTA DE CREDITO'].set_index('CLAVE')
nc_tot = cab[(cab.TIPO_DOC == 'NOTA DE CREDITO') & (cab.DOC_REF != '')].groupby('DOC_REF').TOTAL.sum()


def saldos():
    pagado = cob.groupby('DOC').IMPORTE.sum()
    return (docs.TOTAL - nc_tot.reindex(docs.index).fillna(0) - pagado.reindex(docs.index).fillna(0)).round(2)


def alternativas(num):
    """Numeros a un error de tecleo: dos digitos contiguos intercambiados o el ultimo +-1."""
    s, out = str(num), set()
    for i in range(len(s) - 1):
        out.add(s[:i] + s[i + 1] + s[i] + s[i + 2:])
    for d in (-1, 1):
        out.add(s[:-1] + str((int(s[-1]) + d) % 10))
    out.discard(s)
    return out


saldo = saldos()
emision = docs.FECHA_EMISION
sospechosos = cob[(cob.DOC.map(saldo) < -0.5) | (cob.FECHA_COBRO < cob.DOC.map(emision))]
for i, r in sospechosos.iterrows():
    cands = []
    for alt in alternativas(r.NUMERO_DOC):
        k = f'{r.SERIE_DOC}-{alt}'
        if k in docs.index and docs.at[k, 'ESTADO'] == 'ACEPTADO' and emision[k] <= r.FECHA_COBRO \
                and docs.at[k, 'MONEDA'] == r.MONEDA and abs(saldo[k] - r.IMPORTE) <= 0.05:
            cands.append(alt)
    if len(cands) == 1:
        registrar_uno('cobros', r.ID_COBRO, 'NUMERO_DOC', 'R9 cobro reimputado (numero mal tecleado)', r.NUMERO_DOC, cands[0])
        cob.at[i, 'NUMERO_DOC'] = cands[0]
        cob.at[i, 'DOC'] = f'{r.SERIE_DOC}-{cands[0]}'
        saldo = saldos()
    else:
        motivo = 'cobro anterior a la emision' if r.FECHA_COBRO < emision.get(r.DOC, r.FECHA_COBRO) else 'comprobante cobrado de mas'
        cob.at[i, 'REVISAR'] = motivo
saldo = saldos()
for i, r in cob[(cob.REVISAR != '') & (cob.DOC.map(saldo) >= -0.5) & (cob.FECHA_COBRO >= cob.DOC.map(emision))].iterrows():
    cob.at[i, 'REVISAR'] = ''  # se resolvio al reimputar otro cobro del mismo documento
for _, r in cob[cob.REVISAR != ''].iterrows():
    registrar_uno('cobros', r.ID_COBRO, 'NUMERO_DOC', f'R9 a revisar: {r.REVISAR}', r.NUMERO_DOC, 'REVISAR')

# ======================================================================================
# Salida
# ======================================================================================
for c in ('FECHA_EMISION', 'FECHA_VENCIMIENTO'):
    cab[c] = iso(cab[c])
for c in ('FECHA_EMISION', 'FECHA_VALIDEZ'):
    pfc[c] = iso(pfc[c])
cob['FECHA_COBRO'] = iso(cob.FECHA_COBRO)
cab['NUM_PROFORMA'] = cab.NUM_PROFORMA.astype('string').fillna('')

cab = cab.sort_values(['SERIE', 'NUMERO'], key=lambda s: s.astype(int) if s.name == 'NUMERO' else s)
cab[ESQUEMA_COMPROBANTES + ['NUM_PROFORMA', 'DOC_REF', 'METODO_ENLACE', 'REFERENCIA_ORIGINAL', 'FICHERO_ORIGEN']] \
    .to_csv(f'{OUT}/comprobantes_cabecera.csv', index=False)
det.drop(columns=['CLAVE', 'DOC']).to_csv(f'{OUT}/comprobantes_detalle.csv', index=False)
pfc.drop(columns=['CLAVE', '_formato']).to_csv(f'{OUT}/proformas_cabecera.csv', index=False)
pfd.to_csv(f'{OUT}/proformas_detalle.csv', index=False)
cob.drop(columns=['DOC']).to_csv(f'{OUT}/cobros.csv', index=False)
cols_cli = ['RUC_DNI', 'TIPO_DOC_ID', 'RAZON_SOCIAL', 'TIPO', 'SECTOR', 'DEP', 'PROV', 'CREDITO', 'AGENTE_RETENCION',
            'FECHA_ALTA', 'VENDEDOR_ASIGNADO']
cli[[c for c in cols_cli if c in cli]].to_csv(f'{OUT}/clientes.csv', index=False)
prod.to_csv(f'{OUT}/productos.csv', index=False)
enl = cab[(cab.ESTADO == 'ACEPTADO') & (cab.NUM_PROFORMA != '')]
enl = pd.DataFrame(dict(SERIE_PROFORMA='PF01', NUMERO_PROFORMA=enl.NUM_PROFORMA, SERIE_DOC=enl.SERIE,
                        NUMERO_DOC=enl.NUMERO, METODO=enl.METODO_ENLACE))
enl.to_csv(f'{OUT}/enlace_proforma_comprobante.csv', index=False)

# ======================================================================================
# Conciliacion de importes: bruto + ajustes registrados = limpio
# ======================================================================================
limpio = {('comprobantes', m): v for m, v in cab.groupby('MONEDA').TOTAL.sum().items()}
limpio[('comprobantes_detalle', '-')] = det.TOTAL_LINEA.sum()
limpio.update({('cobros', m): v for m, v in cob.groupby('MONEDA').IMPORTE.sum().items()})
filas_limpio = {'comprobantes': len(cab), 'comprobantes_detalle': len(det), 'cobros': len(cob)}
conc = []
for (tabla, mon), v_limpio in limpio.items():
    bruto = AJ.get((tabla, 'bruto', mon), 0.0)
    ajustes = {k[1]: v for k, v in AJ.items() if k[0] == tabla and k[2] == mon and k[1] != 'bruto'}
    residuo = round(bruto + sum(ajustes.values()) - v_limpio, 2)
    conc.append(dict(TABLA=tabla, MONEDA=mon, FILAS_BRUTO=FILAS_BRUTO[tabla], FILAS_LIMPIO=filas_limpio[tabla],
                     IMPORTE_BRUTO=round(bruto, 2),
                     **{f'AJUSTE: {k}': round(v, 2) for k, v in ajustes.items()},
                     IMPORTE_LIMPIO=round(v_limpio, 2), RESIDUO_SIN_EXPLICAR=residuo))
conc = pd.DataFrame(conc).fillna(0)
conc.to_csv(f'{OUT}/conciliacion.csv', index=False)

log = pd.DataFrame(LOG)
log.to_csv(f'{OUT}/registro_limpieza.csv', index=False)
resumen = log.groupby(['TABLA', 'REGLA']).size().rename('CAMBIOS').reset_index()
resumen.to_csv(f'{OUT}/resumen_limpieza.csv', index=False)
pd.set_option('display.width', 200, 'display.max_colwidth', 80)
print(resumen.to_string(index=False))
print(f'\nFilas -> clientes {len(cli)}, comprobantes {len(cab)}, lineas {len(det)}, proformas {len(pfc)}, '
      f'cobros {len(cob)}, enlaces {len(enl)}  |  cambios registrados: {len(log)}')
print('Enlaces por metodo:', dict(Counter(enl.METODO)))
print('Proformas por estado (derivado):', dict(Counter(pfc.ESTADO)))
print('\nCONCILIACION (importe bruto + ajustes registrados = importe limpio)')
print(conc.T.to_string(header=False))
estado = 'OK: toda diferencia esta explicada' if (conc.RESIDUO_SIN_EXPLICAR.abs() < 0.01).all() else 'ATENCION: hay diferencias sin explicar'
print('->', estado, '\n')
print('Pendientes de revision -> cobros:', int((cob.REVISAR != '').sum()),
      '| proformas cerradas sin venta:', int((pfc.REVISAR != '').sum()))
