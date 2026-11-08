#!/usr/bin/env python3
"""
Auditoria de calidad de las fuentes (OE2).
Lee los ficheros tal como llegan, los inventaria y mide sus incidencias por
dimension de calidad (ISO/IEC 25012). No corrige nada: solo describe y cuantifica.

Uso: python auditoria.py fuentes/ resultados/
"""
import json
import os
import re
import sys
from collections import Counter

import numpy as np
import pandas as pd

SRC = sys.argv[1] if len(sys.argv) > 1 else 'fuentes'
OUT = sys.argv[2] if len(sys.argv) > 2 else 'resultados'
os.makedirs(OUT, exist_ok=True)

INC = []  # incidencias: una fila por regla


def inc(fuente, dimension, regla, n, total, ejemplo='', severidad='media', tipo='REGLA'):
    """Registra el resultado de una comprobacion.
    tipo='REGLA'    -> comprobacion independiente (cuenta en el recuento de incidencias)
    tipo='DESGLOSE' -> detalle de la regla anterior (no cuenta como incidencia propia)"""
    INC.append(dict(TIPO=tipo, FUENTE=fuente, DIMENSION=dimension, REGLA=regla, AFECTADOS=int(n), TOTAL=int(total),
                    PCT=round(100 * n / total, 2) if total else 0,
                    SEVERIDAD=severidad if n else '', EJEMPLO=str(ejemplo)[:160]))


def ejemplos(s, k=3):
    return ' | '.join(map(str, list(s)[:k]))


def ruc_valido(r):
    r = str(r)
    if not re.fullmatch(r'\d{11}', r):
        return False
    w = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    d = 11 - sum(int(a) * b for a, b in zip(r[:10], w)) % 11
    return {10: 0, 11: 1}.get(d, d) == int(r[10])


def detectar_cabecera(ruta, sep_candidatos=(',', ';')):
    with open(ruta, encoding='utf-8') as f:
        lineas = [f.readline() for _ in range(10)]
    for i, l in enumerate(lineas):
        for sep in sep_candidatos:
            if l.count(sep) >= 4:
                return i, sep
    return 0, ','


def formato_fecha(v):
    v = str(v)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
        return 'aaaa-mm-dd'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', v):
        return 'aaaa-mm-dd hh:mm:ss'
    if re.fullmatch(r'\d{2}/\d{2}/\d{4}', v):
        return 'dd/mm/aaaa'
    return 'otro'


def a_fecha(s):
    s = s.astype(str)
    out = pd.to_datetime(s.where(s.str.match(r'^\d{4}-')).str[:10], format='%Y-%m-%d', errors='coerce')
    out = out.fillna(pd.to_datetime(s.where(s.str.match(r'^\d{2}/')), format='%d/%m/%Y', errors='coerce'))
    return out


# ------------------------------------------------------------------------------------
# 1. Inventario
# ------------------------------------------------------------------------------------
inventario, crudos = [], {}
for f in sorted(os.listdir(SRC)):
    if not f.endswith('.csv'):
        continue
    ruta = os.path.join(SRC, f)
    fila, sep = detectar_cabecera(ruta)
    decimal = ',' if sep == ';' else '.'
    df = pd.read_csv(ruta, sep=sep, skiprows=fila, dtype=str, keep_default_na=False, decimal=decimal)
    crudos[f] = df
    fechas = [c for c in df.columns if 'FECHA' in c.upper() or c.upper().startswith('F. ')]
    formatos = Counter(formato_fecha(v) for c in fechas for v in df[c] if v)
    rango = ''
    if fechas:
        fx = a_fecha(df[fechas[0]])
        rango = f"{fx.min():%Y-%m-%d} a {fx.max():%Y-%m-%d}"
    inventario.append(dict(FICHERO=f, FILAS=len(df), COLUMNAS=len(df.columns), SEPARADOR=sep, DECIMAL=decimal,
                           FILA_CABECERA=fila + 1, FORMATOS_FECHA=dict(formatos), PERIODO=rango,
                           TAMANO_KB=round(os.path.getsize(ruta) / 1024, 1)))
pd.DataFrame(inventario).to_csv(f'{OUT}/inventario.csv', index=False)

# ------------------------------------------------------------------------------------
# 2. Estructura: comparar las exportaciones anuales de comprobantes
# ------------------------------------------------------------------------------------
# Exportaciones anuales (fuentes brutas) o un unico fichero integrado (capa plata)
comp_files = sorted(f for f in crudos if re.fullmatch(r'comprobantes_\d{4}\.csv', f)) or ['comprobantes_cabecera.csv']
base_cols = list(crudos[comp_files[0]].columns)
estructura = []
for f in comp_files:
    cols = list(crudos[f].columns)
    estructura.append(dict(FICHERO=f, FALTAN=sorted(set(base_cols) - set(cols)), NUEVAS=sorted(set(cols) - set(base_cols)),
                           MISMO_ORDEN=cols[:len(base_cols)] == base_cols))
    if set(cols) != set(base_cols) or cols[:len(base_cols)] != base_cols:
        inc(f, 'Consistencia', 'Estructura distinta a la exportacion de referencia (2024)', 1, 1,
            f"faltan {sorted(set(base_cols) - set(cols))}; nuevas {sorted(set(cols) - set(base_cols))}", 'alta')
    meta = next(i for i in inventario if i['FICHERO'] == f)
    if meta['SEPARADOR'] != ',' or meta['FILA_CABECERA'] != 1:
        inc(f, 'Consistencia', 'Formato de exportacion distinto (separador, decimal o filas de titulo)', 1, 1,
            f"separador '{meta['SEPARADOR']}', decimal '{meta['DECIMAL']}', cabecera en fila {meta['FILA_CABECERA']}", 'alta')

# Union provisional de comprobantes para poder auditarlos juntos (mapeo de nombres)
MAPEO = {'F. EMISION': 'FECHA_EMISION', 'F. VENCIMIENTO': 'FECHA_VENCIMIENTO',
         'NRO DOC CLIENTE': 'RUC_CLIENTE', 'RAZON SOCIAL': 'CLIENTE_NOMBRE'}
partes = []
for f in comp_files:
    d = crudos[f].rename(columns=MAPEO).copy()
    d['_FICHERO'] = f
    partes.append(d)
cab = pd.concat(partes, ignore_index=True)
for c in ('SUBTOTAL', 'IGV', 'TOTAL'):
    cab[c] = pd.to_numeric(cab[c].str.replace(',', '.'), errors='coerce')
cab['FE'] = a_fecha(cab.FECHA_EMISION)
cab['FV'] = a_fecha(cab.FECHA_VENCIMIENTO)
cab['CLAVE'] = cab.SERIE + '-' + cab.NUMERO
N = len(cab)

cli = crudos['clientes.csv'].copy()
prod = crudos['productos.csv'].copy()
det = crudos['comprobantes_detalle.csv'].copy()
pfc = crudos['proformas_cabecera.csv'].copy()
pfd = crudos['proformas_detalle.csv'].copy()
cob = crudos['cobros.csv'].copy()
for d in (det, pfd):
    for c in ('CANTIDAD', 'PRECIO_UNITARIO', 'DESCUENTO', 'TOTAL_LINEA', 'COSTO_UNITARIO'):
        d[c] = pd.to_numeric(d[c], errors='coerce')
for c in ('SUBTOTAL', 'IGV', 'TOTAL'):
    pfc[c] = pd.to_numeric(pfc[c], errors='coerce')
cob['IMPORTE'] = pd.to_numeric(cob.IMPORTE, errors='coerce')

# ------------------------------------------------------------------------------------
# 3. Clientes
# ------------------------------------------------------------------------------------
n = len(cli)
lon = cli.RUC_DNI.str.len()
inc('clientes.csv', 'Exactitud', 'Identificador con longitud distinta de 8 (DNI) u 11 (RUC)', (~lon.isin([8, 11])).sum(), n,
    ejemplos(cli.RUC_DNI[~lon.isin([8, 11])]), 'alta')
r11 = cli[lon == 11]
inc('clientes.csv', 'Exactitud', 'RUC con digito de control no valido', (~r11.RUC_DNI.map(ruc_valido)).sum(), len(r11),
    ejemplos(r11.RUC_DNI[~r11.RUC_DNI.map(ruc_valido)]), 'media')
dup = cli[cli.RUC_DNI.duplicated(keep=False)]
inc('clientes.csv', 'Unicidad', 'Mismo identificador dado de alta mas de una vez', dup.RUC_DNI.nunique(), cli.RUC_DNI.nunique(),
    ejemplos(dup.sort_values('RUC_DNI').RAZON_SOCIAL), 'alta')
VACIO = ['', '-', 'NULL', 'N/A']
sin_ubic = cli.DEP.isin(VACIO) | cli.PROV.isin(VACIO)
inc('clientes.csv', 'Completitud', 'Departamento y provincia vacios o con marcador ("-")', sin_ubic.sum(), n,
    ejemplos(cli.RUC_DNI[sin_ubic]), 'baja')
otros = [c for c in cli.columns if c not in ('DEP', 'PROV') and cli[c].isin(VACIO).any()]
inc('clientes.csv', 'Completitud', 'Otros campos del maestro vacios', sum(cli[c].isin(VACIO).sum() for c in otros), n,
    ', '.join(otros), 'baja')
inc('clientes.csv', 'Consistencia', 'Tipo de cliente incoherente con el documento (empresa sin RUC 20 / persona con RUC 20)',
    (((cli.TIPO == 'EMPRESA') & ~cli.RUC_DNI.str.startswith('20')) |
     ((cli.TIPO != 'EMPRESA') & cli.RUC_DNI.str.startswith('20'))).sum(), n)
alta = pd.to_datetime(cli.FECHA_ALTA, errors='coerce')

# ------------------------------------------------------------------------------------
# 4. Comprobantes (cabecera)
# ------------------------------------------------------------------------------------
dupk = cab[cab.CLAVE.duplicated(keep=False)]
inc('comprobantes_*', 'Unicidad', 'Comprobante repetido entre exportaciones (misma serie-numero)', dupk.CLAVE.nunique(), cab.CLAVE.nunique(),
    f"{ejemplos(dupk.CLAVE.unique())} en {sorted(dupk._FICHERO.unique())}", 'alta')
anual = cab._FICHERO.str.fullmatch(r'comprobantes_\d{4}\.csv')
fuera = cab[anual & (cab.FE.dt.year.astype('Int64').astype(str) != cab._FICHERO.str[13:17])]
inc('comprobantes_*', 'Unicidad', '...de ellos, por solape de periodos (fecha fuera del ano del fichero)', len(fuera), N,
    ejemplos(fuera.CLAVE + ' ' + fuera.FECHA_EMISION + ' en ' + fuera._FICHERO), 'alta', 'DESGLOSE')
u = cab.drop_duplicates('CLAVE').copy()
NU = len(u)
# correlatividad y huecos
huecos = {}
for s, g in u.groupby('SERIE'):
    nums = g.NUMERO.astype(int).sort_values()
    falt = set(range(nums.min(), nums.max() + 1)) - set(nums)
    huecos[s] = len(falt)
    orden = g.assign(n=g.NUMERO.astype(int)).sort_values('n')
    desord = (orden.FE.diff().dt.days < 0).sum()
    if desord:
        inc('comprobantes_*', 'Consistencia', f'Serie {s}: numeracion no sigue el orden de fechas', desord, len(g))
inc('comprobantes_*', 'Completitud', 'Huecos en la numeracion correlativa de las series', sum(huecos.values()), NU,
    str(huecos), 'baja')
# monedas
monedas = Counter(u.MONEDA)
no_std = u[~u.MONEDA.isin(['PEN', 'USD'])]
inc('comprobantes_*', 'Consistencia', 'Moneda con codificacion no estandar (no ISO 4217)', len(no_std), NU, str(dict(Counter(no_std.MONEDA))), 'media')
# identificadores de cliente
lonc = u.RUC_CLIENTE.str.len()
inc('comprobantes_*', 'Exactitud', 'Documento del cliente con longitud invalida (probable perdida de ceros)', (~lonc.isin([8, 11])).sum(), NU,
    ejemplos(u.RUC_CLIENTE[~lonc.isin([8, 11])]), 'alta')
no_maestro = u[~u.RUC_CLIENTE.isin(cli.RUC_DNI)]
# Severidad media: si el unico identificador ausente es el cliente generico 00000000 (CLIENTES VARIOS),
# se resuelve con una regla de mapeo y no impide los cruces; alta si faltan clientes reales.
reales = no_maestro[no_maestro.RUC_CLIENTE.str.lstrip('0') != '']
inc('comprobantes_*', 'Integridad referencial', 'Cliente del comprobante que no existe en el maestro', len(no_maestro), NU,
    ejemplos(no_maestro.RUC_CLIENTE.unique()), 'alta' if len(reales) else 'media')
inc('comprobantes_*', 'Consistencia', 'Factura emitida a DNI o boleta emitida a RUC 20',
    (((u.TIPO_DOC == 'FACTURA') & (lonc != 11)) | ((u.TIPO_DOC == 'BOLETA') & u.RUC_CLIENTE.str.startswith('20'))).sum(), NU)
nombres = u.groupby('RUC_CLIENTE').CLIENTE_NOMBRE.nunique()
varios = nombres[nombres > 1]
ej = u[u.RUC_CLIENTE.isin(varios.index[:2])].groupby('RUC_CLIENTE').CLIENTE_NOMBRE.unique().map(list).to_dict()
filas_var = u[u.RUC_CLIENTE.isin(varios.index)]
moda = filas_var.groupby('RUC_CLIENTE').CLIENTE_NOMBRE.agg(lambda s: s.mode()[0])
n_var = (filas_var.CLIENTE_NOMBRE != filas_var.RUC_CLIENTE.map(moda)).sum()
inc('comprobantes_*', 'Consistencia', 'Razon social escrita de varias formas para un mismo cliente', n_var, NU,
    json.dumps(ej, ensure_ascii=False))
vend = Counter(u.VENDEDOR)
inc('comprobantes_*', 'Consistencia', 'Vendedor escrito de forma abreviada o distinta', sum(v for k, v in vend.items() if '.' in k), NU,
    str({k: v for k, v in vend.items() if '.' in k}))
# importes
cuadre = (u.SUBTOTAL.abs() + u.IGV.abs() - u.TOTAL.abs()).abs() > .02
inc('comprobantes_*', 'Exactitud', 'SUBTOTAL + IGV distinto de TOTAL', cuadre.sum(), NU,
    ejemplos(u[cuadre].CLAVE + ' tot=' + u[cuadre].TOTAL.astype(str)), 'alta')
igv = (u.SUBTOTAL.abs() * .18 - u.IGV.abs()).abs() > .02
inc('comprobantes_*', 'Exactitud', 'IGV distinto del 18 % del subtotal', igv.sum(), NU, '', 'media')
nc = u[u.TIPO_DOC == 'NOTA DE CREDITO']
signos = Counter(np.sign(nc.TOTAL))
inc('comprobantes_*', 'Consistencia', 'Notas de credito con criterio de signo mixto (positivas y negativas)',
    min(signos.values()) if len(signos) > 1 else 0, len(nc), f"{dict(signos)} — negativas en {sorted(nc[nc.TOTAL < 0]._FICHERO.unique())}", 'alta')
inc('comprobantes_*', 'Completitud', 'Nota de credito sin documento de referencia', (nc.REFERENCIA == '').sum(), len(nc), '', 'alta')
inc('comprobantes_*', 'Completitud', 'Nota de credito sin motivo', (nc.MOTIVO_NC == '').sum(), len(nc), '', 'media')
# vencimientos
dias = (u.FV - u.FE).dt.days
esperado = u.CONDICION_PAGO.str.extract(r'(\d+)')[0].astype(float).fillna(0)
inc('comprobantes_*', 'Consistencia', 'Fecha de vencimiento no coherente con la condicion de pago', (dias != esperado).sum(), NU)
# referencias a proformas
ventas = u[u.TIPO_DOC != 'NOTA DE CREDITO']
patrones = Counter(re.sub(r'\d+', '#', r) if r else '(vacia)' for r in ventas.REFERENCIA)
pd.DataFrame(patrones.most_common(), columns=['PATRON', 'N']).to_csv(f'{OUT}/patrones_referencia.csv', index=False)
canonica = ventas.REFERENCIA.str.match(r'^\[PROFORMA : PF01-\d{8}\]$')
otra = (~canonica) & (ventas.REFERENCIA != '')
inc('comprobantes_*', 'Consistencia', 'Referencia a la proforma en texto libre no normalizado', otra.sum(), len(ventas),
    ejemplos(ventas.REFERENCIA[otra].unique()), 'alta')
inc('comprobantes_*', 'Completitud', 'Venta sin referencia a proforma (venta directa o referencia perdida)', (ventas.REFERENCIA == '').sum(),
    len(ventas), 'no se puede distinguir sin reconstruir el enlace', 'media')
num_ref = ventas.REFERENCIA.str.extract(r'(\d{4,8})')[0].dropna().astype(int)
pf_nums = set(pfc.NUMERO.astype(int))
inc('comprobantes_*', 'Integridad referencial', 'Referencia a una proforma que no existe', (~num_ref.isin(pf_nums)).sum(), len(num_ref))

# ------------------------------------------------------------------------------------
# 5. Detalle de comprobantes
# ------------------------------------------------------------------------------------
nd = len(det)
dupl = det.duplicated(keep='first')
inc('comprobantes_detalle.csv', 'Unicidad', 'Linea repetida (fila identica)', dupl.sum(), nd,
    ejemplos(det[dupl].SERIE + '-' + det[dupl].NUMERO + '/' + det[dupl].ITEM), 'alta')
cod_ok = det.CODIGO_PROD.isin(prod.CODIGO_PROD)
inc('comprobantes_detalle.csv', 'Integridad referencial', 'Codigo de producto que no existe en el catalogo', (~cod_ok).sum(), nd,
    ejemplos(det.CODIGO_PROD[~cod_ok].unique()), 'alta')
recup = det.CODIGO_PROD[~cod_ok].str.strip().str.upper().str.replace(r'PRD-(\d+)$', lambda m: f"PRD-{int(m.group(1)):04d}", regex=True)
inc('comprobantes_detalle.csv', 'Exactitud', '...de ellos, recuperables normalizando (espacios, mayusculas, ceros)', recup.isin(prod.CODIGO_PROD).sum(),
    (~cod_ok).sum(), '', 'baja', 'DESGLOSE')
calc = (det.CANTIDAD * det.PRECIO_UNITARIO - det.DESCUENTO - det.TOTAL_LINEA).abs() > .02
inc('comprobantes_detalle.csv', 'Exactitud', 'Total de linea distinto de cantidad x precio - descuento', calc.sum(), nd)
inc('comprobantes_detalle.csv', 'Exactitud', 'Precio unitario por debajo del coste (venta a perdida)', (det.PRECIO_UNITARIO < det.COSTO_UNITARIO).sum(), nd, '', 'baja')
det['CLAVE'] = det.SERIE + '-' + det.NUMERO
sin_cab = ~det.CLAVE.isin(u.CLAVE)
inc('comprobantes_detalle.csv', 'Integridad referencial', 'Linea cuyo comprobante no esta en ninguna cabecera', sin_cab.sum(), nd)
sin_det = ~u.CLAVE.isin(det.CLAVE)
inc('comprobantes_*', 'Completitud', 'Comprobante sin lineas de detalle', sin_det.sum(), NU)
suma = det.drop_duplicates().groupby('CLAVE').TOTAL_LINEA.sum()
dif = (u.set_index('CLAVE').SUBTOTAL.abs() - suma).abs()
desc = dif[dif > .05]
inc('comprobantes_*', 'Exactitud', 'Subtotal de cabecera distinto de la suma de lineas (sin duplicados)', len(desc), NU,
    ejemplos(desc.index), 'alta')
suma_d = det.groupby('CLAVE').TOTAL_LINEA.sum()
dif_d = (u.set_index('CLAVE').SUBTOTAL.abs() - suma_d).abs()
inc('comprobantes_*', 'Exactitud', '...si no se eliminan las lineas duplicadas', (dif_d > .05).sum(), NU, '', 'baja', 'DESGLOSE')

# ------------------------------------------------------------------------------------
# 6. Proformas
# ------------------------------------------------------------------------------------
npf = len(pfc)
fmt = Counter(pfc.FECHA_EMISION.map(formato_fecha))
inc('proformas_cabecera.csv', 'Consistencia', 'Fecha de emision en formato distinto de aaaa-mm-dd', npf - fmt.get('aaaa-mm-dd', 0), npf, str(dict(fmt)), 'alta')
pfc['FE'] = a_fecha(pfc.FECHA_EMISION)
pfc['FV'] = a_fecha(pfc.FECHA_VALIDEZ)
amb = pfc.FECHA_EMISION.str.match(r'^(0[1-9]|1[0-2])/(0[1-9]|1[0-2])/')
inc('proformas_cabecera.csv', 'Exactitud', 'Fecha de emision ambigua (dia y mes <= 12: dd/mm o mm/dd)', amb.sum(), npf, '', 'media')
lonp = pfc.RUC_CLIENTE.str.len()
inc('proformas_cabecera.csv', 'Exactitud', 'Documento del cliente con longitud invalida', (~lonp.isin([8, 11])).sum(), npf,
    ejemplos(pfc.RUC_CLIENTE[~lonp.isin([8, 11])]), 'alta')
inc('proformas_cabecera.csv', 'Integridad referencial', 'Cliente de la proforma que no existe en el maestro', (~pfc.RUC_CLIENTE.isin(cli.RUC_DNI)).sum(), npf)
corte = max(u.FE.max(), pfc.FE.max())
abiertas = pfc[pfc.ESTADO == 'ABIERTA']
caducadas = abiertas[abiertas.FV < corte]
inc('proformas_cabecera.csv', 'Actualidad', 'Proforma ABIERTA con la validez ya vencida a la fecha de extraccion', len(caducadas), len(abiertas),
    f"{len(caducadas)} de {len(abiertas)} abiertas", 'alta')
refs = set(num_ref)
pfc['N'] = pfc.NUMERO.astype(int)
inc('proformas_cabecera.csv', 'Actualidad', 'Proforma ABIERTA que ya tiene una venta que la referencia', abiertas.NUMERO.astype(int).isin(refs).sum(),
    len(abiertas), '', 'alta')
cerr = pfc[pfc.ESTADO == 'CERRADA']
inc('proformas_cabecera.csv', 'Consistencia', 'Proforma CERRADA sin ninguna venta que la referencie (referencia perdida?)', (~cerr.N.isin(refs)).sum(), len(cerr))
inc('proformas_cabecera.csv', 'Consistencia', 'Proforma RECHAZADA o VENCIDA que aparece referenciada en una venta',
    pfc[pfc.ESTADO.isin(['RECHAZADA', 'VENCIDA'])].N.isin(refs).sum(), npf)
pfd['CLAVE'] = pfd.SERIE + '-' + pfd.NUMERO
pfc['CLAVE'] = pfc.SERIE + '-' + pfc.NUMERO
sp = pfd.groupby('CLAVE').TOTAL_LINEA.sum()
inc('proformas_cabecera.csv', 'Exactitud', 'Subtotal distinto de la suma de lineas', ((pfc.set_index('CLAVE').SUBTOTAL - sp).abs() > .05).sum(), npf)
inc('proformas_cabecera.csv', 'Consistencia', 'Validez anterior a la emision', (pfc.FV < pfc.FE).sum(), npf)
inc('proformas_detalle.csv', 'Integridad referencial', 'Codigo de producto que no existe en el catalogo', (~pfd.CODIGO_PROD.isin(prod.CODIGO_PROD)).sum(), len(pfd))
inc('proformas_detalle.csv', 'Unicidad', 'Linea repetida', pfd.duplicated().sum(), len(pfd))
# fecha de venta anterior a la de su proforma
vr = ventas.assign(N=ventas.REFERENCIA.str.extract(r'(\d{4,8})')[0]).dropna(subset=['N'])
vr['N'] = vr.N.astype(int)
vr = vr.merge(pfc[['N', 'FE', 'RUC_CLIENTE']], on='N', suffixes=('', '_PF'))
inc('comprobantes_*', 'Consistencia', 'Venta con fecha anterior a la de su proforma', (vr.FE < vr.FE_PF).sum(), len(vr))
inc('comprobantes_*', 'Consistencia', 'Venta y proforma enlazadas con clientes distintos (comparando sin ceros iniciales)',
    (vr.RUC_CLIENTE.str.lstrip('0') != vr.RUC_CLIENTE_PF.str.lstrip('0')).sum(), len(vr))

# ------------------------------------------------------------------------------------
# 7. Cobros
# ------------------------------------------------------------------------------------
nco = len(cob)
cob['CLAVE'] = cob.SERIE_DOC + '-' + cob.NUMERO_DOC
inc('cobros.csv', 'Unicidad', 'Identificador de cobro repetido', cob.ID_COBRO.duplicated().sum(), nco)
contenido = cob.drop(columns=['ID_COBRO']).duplicated(keep=False)
grupos = cob[contenido].groupby(['CLAVE', 'FECHA_COBRO', 'IMPORTE', 'MEDIO_PAGO', 'TIPO_MOVIMIENTO']).size()
inc('cobros.csv', 'Unicidad', 'Cobro registrado dos veces (mismo documento, fecha, importe y medio con otro ID)', (grupos - 1).sum(), nco,
    ejemplos(cob[contenido].ID_COBRO), 'alta')
sin_doc = ~cob.CLAVE.isin(u.CLAVE)
inc('cobros.csv', 'Integridad referencial', 'Cobro de un comprobante que no existe', sin_doc.sum(), nco, ejemplos(cob[sin_doc].CLAVE), 'alta')
medios = Counter(cob.MEDIO_PAGO)
std = {'TRANSFERENCIA BCP', 'TRANSFERENCIA BBVA', 'TRANSFERENCIA INTERBANK', 'CHEQUE', 'EFECTIVO', 'YAPE', 'TARJETA',
       'DEPOSITO CTA DETRACCIONES BN', 'COMPROBANTE DE RETENCION'}
inc('cobros.csv', 'Consistencia', 'Medio de pago escrito de forma no estandar', sum(v for k, v in medios.items() if k not in std), nco,
    str({k: v for k, v in medios.items() if k not in std}))
cj = cob.merge(u[['CLAVE', 'FE', 'ESTADO', 'MONEDA', 'TOTAL', 'TIPO_DOC']], on='CLAVE', how='inner', suffixes=('', '_DOC'))
cj['FC'] = pd.to_datetime(cj.FECHA_COBRO)
inc('cobros.csv', 'Consistencia', 'Cobro de un comprobante ANULADO', (cj.ESTADO == 'ANULADO').sum(), len(cj), '', 'alta')
inc('cobros.csv', 'Consistencia', 'Cobro anterior a la emision del comprobante', (cj.FC < cj.FE).sum(), len(cj))
mon = cj.MONEDA_DOC.replace({'S/': 'PEN', 'SOLES': 'PEN', 'US$': 'USD'})
inc('cobros.csv', 'Consistencia', 'Moneda del cobro distinta de la del comprobante', (cj.MONEDA != mon).sum(), len(cj))
nc_ref = nc.assign(R=nc.REFERENCIA.str.extract(r': (\S+)\]')[0]).groupby('R').TOTAL.apply(lambda s: s.abs().sum())
saldo = u[u.TIPO_DOC != 'NOTA DE CREDITO'].set_index('CLAVE').TOTAL
pagado = cob[~cob.duplicated(subset=[c for c in cob.columns if c != 'ID_COBRO'])].groupby('CLAVE').IMPORTE.sum()
exceso = (pagado.reindex(saldo.index).fillna(0) + nc_ref.reindex(saldo.index).fillna(0) - saldo)
inc('cobros.csv', 'Exactitud', 'Comprobante cobrado por encima de su total (descontadas NC y duplicados)', (exceso > .5).sum(), len(saldo),
    ejemplos(exceso[exceso > .5].index))

# ------------------------------------------------------------------------------------
# 8. Actualidad y cobertura
# ------------------------------------------------------------------------------------
cobertura = dict(
    comprobantes=f"{u.FE.min():%Y-%m-%d} a {u.FE.max():%Y-%m-%d}",
    proformas=f"{pfc.FE.min():%Y-%m-%d} a {pfc.FE.max():%Y-%m-%d}",
    cobros=f"{pd.to_datetime(cob.FECHA_COBRO).min():%Y-%m-%d} a {pd.to_datetime(cob.FECHA_COBRO).max():%Y-%m-%d}",
    clientes_alta=f"{alta.min():%Y-%m-%d} a {alta.max():%Y-%m-%d}")
mensual = u[u.TIPO_DOC != 'NOTA DE CREDITO'].groupby(u.FE.dt.to_period('M')).size()
inc('(conjunto)', 'Actualidad', 'Meses sin comprobantes dentro del periodo', (mensual == 0).sum(), len(mensual), '', 'baja')

# ------------------------------------------------------------------------------------
# Resumen
# ------------------------------------------------------------------------------------
res = pd.DataFrame(INC)
res.to_csv(f'{OUT}/incidencias.csv', index=False)
resumen = dict(inventario=inventario, estructura=estructura, cobertura=cobertura,
               comprobantes_unicos=NU, filas_cabecera=N, tipos=Counter(u.TIPO_DOC), estados=Counter(u.ESTADO),
               monedas=monedas, condiciones=Counter(u.CONDICION_PAGO), proformas_estado=Counter(pfc.ESTADO),
               cobros_tipo=Counter(cob.TIPO_MOVIMIENTO), medios=medios, huecos=huecos,
               clientes_tipo=Counter(cli.TIPO), meses=len(mensual))
with open(f'{OUT}/resumen.json', 'w') as f:
    json.dump(resumen, f, ensure_ascii=False, indent=1, default=str)
pd.set_option('display.width', 250, 'display.max_colwidth', 90)
reglas = res[res.TIPO == 'REGLA']
con = reglas[reglas.AFECTADOS > 0]
print(f"COMPROBACIONES: {len(reglas)} | con incidencias: {len(con)} "
      f"(alta {(con.SEVERIDAD == 'alta').sum()}, media {(con.SEVERIDAD == 'media').sum()}, baja {(con.SEVERIDAD == 'baja').sum()}) "
      f"| sin incidencias: {(reglas.AFECTADOS == 0).sum()}\n")
orden = {'alta': 0, 'media': 1, 'baja': 2}
print(res[res.AFECTADOS > 0].assign(o=res.SEVERIDAD.map(orden)).sort_values('o', kind='stable')
      [['TIPO', 'FUENTE', 'DIMENSION', 'REGLA', 'AFECTADOS', 'TOTAL', 'PCT', 'SEVERIDAD', 'EJEMPLO']].to_string())
print('\nSin incidencias:'); print(reglas[reglas.AFECTADOS == 0][['FUENTE', 'REGLA']].to_string())
print(json.dumps({k: v for k, v in resumen.items() if k not in ('inventario',)}, ensure_ascii=False, default=str))

# ------------------------------------------------------------------------------------
# 9. Analisis complementarios
# ------------------------------------------------------------------------------------
norm = lambda s: s.str.lstrip('0').replace('', '0')
ids_m = set(norm(cli.RUC_DNI))
nm = u[~u.RUC_CLIENTE.isin(cli.RUC_DNI)]
desglose = dict(total=len(nm), clientes_varios=int((nm.RUC_CLIENTE == '00000000').sum()),
                resueltos_sin_ceros=int(((nm.RUC_CLIENTE != '00000000') & norm(nm.RUC_CLIENTE).isin(ids_m)).sum()),
                sin_resolver=int(((nm.RUC_CLIENTE != '00000000') & ~norm(nm.RUC_CLIENTE).isin(ids_m)).sum()))
sinref = ventas[ventas.REFERENCIA == '']
desglose_ref = dict(Counter(sinref.TIPO_DOC))
# Reconstruccion del enlace por cliente + importe + fecha
libres = pfc[(pfc.ESTADO.isin(['CERRADA', 'ABIERTA'])) & ~pfc.N.isin(refs)].copy()
libres['CLI'] = norm(libres.RUC_CLIENTE)
cand = sinref[sinref.TIPO_DOC == 'FACTURA'].copy()
cand['CLI'] = norm(cand.RUC_CLIENTE)
m = cand.merge(libres[['N', 'CLI', 'TOTAL', 'FE']], on='CLI', suffixes=('', '_PF'))
m = m[((m.TOTAL - m.TOTAL_PF).abs() < .05) & ((m.FE - m.FE_PF).dt.days.between(0, 60))]
unicos = m.groupby('CLAVE').N.nunique()
recon = dict(facturas_sin_ref=len(cand), con_candidato_exacto=int((unicos == 1).sum()), ambiguas=int((unicos > 1).sum()))
# Tasa de conversion de proformas: con los estados tal como vienen y corrigiendo las ABIERTAS
e = Counter(pfc.ESTADO)
abiertas_con_venta = int(((pfc.ESTADO == 'ABIERTA') & pfc.N.isin(refs)).sum())
abiertas_caducadas = int(((pfc.ESTADO == 'ABIERTA') & (pfc.FV < corte) & ~pfc.N.isin(refs)).sum())
resueltas = e['CERRADA'] + e['VENCIDA'] + e['RECHAZADA']
conversion = dict(
    tal_como_viene=round(100 * e['CERRADA'] / resueltas, 1),
    corrigiendo_abiertas=round(100 * (e['CERRADA'] + abiertas_con_venta) /
                               (resueltas + abiertas_con_venta + abiertas_caducadas), 1),
    abiertas_con_venta=abiertas_con_venta, abiertas_caducadas_sin_venta=abiertas_caducadas)
# cobros sobre documento equivocado
ex = exceso[exceso > .5].index
sosp = cob[cob.CLAVE.isin(ex)]
print('\nCLIENTES NO EN MAESTRO', desglose)
print('VENTAS SIN REFERENCIA por tipo', desglose_ref)
print('RECONSTRUCCION ENLACE', recon)
print('CONVERSION PROFORMAS (%)', conversion)
print('COBROS sobre doc con exceso', len(sosp))
json.dump(dict(no_maestro=desglose, sinref=desglose_ref, reconstruccion=recon, conversion_proformas=conversion),
          open(f'{OUT}/complementarios.json', 'w'), default=str, indent=1)
