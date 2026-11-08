#!/usr/bin/env python3
"""
Capa oro: almacen de datos con modelo en estrella (Kimball).

Lee la capa plata y construye las dimensiones conformadas y las tablas de hechos de la
matriz de bus (Tabla 11 de la memoria): ventas, proformas y cobros. Ademas:
  - convierte todos los importes a soles (PEN) con el tipo de cambio del dia,
  - historiza la dimension cliente como dimension lentamente cambiante de tipo 2,
  - seudonimiza a las personas fisicas (clientes con DNI y personal de la empresa),
  - concilia los importes con la capa plata y genera el diccionario de datos.

Salida: CSV siempre; si DuckDB esta instalado, tambien almacen.duckdb (tablas + vistas
de indicadores) y una copia en Parquet.

Uso:
    python oro.py <carpeta_plata> <fichero_tipo_cambio> <carpeta_oro> [--restringido CARPETA]
                  [--fecha-carga AAAA-MM-DD]

La carpeta "restringido" guarda la clave de seudonimizacion y la tabla de correspondencias.
No debe subirse al repositorio ni compartirse fuera del equipo.
"""
import argparse
import datetime as dt
import hashlib
import os
import secrets

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument('plata')
ap.add_argument('tipo_cambio')
ap.add_argument('oro')
ap.add_argument('--restringido', default=None, help='carpeta para clave y correspondencias (por defecto, junto a oro)')
ap.add_argument('--fecha-carga', default=None, help='fecha de esta carga, para la historia de clientes (por defecto, hoy)')
args = ap.parse_args()

PLATA, ORO = args.plata, args.oro
RESTRINGIDO = args.restringido or os.path.join(os.path.dirname(os.path.abspath(ORO)), 'restringido')
FECHA_CARGA = pd.Timestamp(args.fecha_carga) if args.fecha_carga else pd.Timestamp(dt.date.today())
os.makedirs(ORO, exist_ok=True)
os.makedirs(RESTRINGIDO, exist_ok=True)

INICIO_VIGENCIA = pd.Timestamp('1900-01-01')  # primera version de cada cliente
FIN_VIGENCIA = pd.Timestamp('9999-12-31')     # version vigente
DESCONOCIDO = -1                              # clave del miembro "desconocido" de cada dimension


def leer(nombre):
    return pd.read_csv(os.path.join(PLATA, nombre), dtype=str, keep_default_na=False)


def num(s):
    return pd.to_numeric(s, errors='coerce')


def clave_fecha(s):
    """Fecha -> entero AAAAMMDD; vacia -> -1."""
    f = pd.to_datetime(s, errors='coerce')
    return f.dt.strftime('%Y%m%d').fillna(str(DESCONOCIDO)).astype(int)


def con_desconocido(dim, clave, valores):
    """Anade la fila 'desconocido' (clave -1), para que ningun hecho quede sin dimension."""
    fila = {c: valores.get(c, 'DESCONOCIDO') for c in dim.columns}
    fila[clave] = DESCONOCIDO
    return pd.concat([pd.DataFrame([fila]), dim], ignore_index=True)


# ======================================================================================
# Lectura de la capa plata
# ======================================================================================
cab = leer('comprobantes_cabecera.csv')
det = leer('comprobantes_detalle.csv')
pfc = leer('proformas_cabecera.csv')
pfd = leer('proformas_detalle.csv')
cob = leer('cobros.csv')
cli = leer('clientes.csv')
prod = leer('productos.csv')
enl = leer('enlace_proforma_comprobante.csv')
for d, cols in ((cab, ['SUBTOTAL', 'IGV', 'TOTAL']), (pfc, ['SUBTOTAL', 'IGV', 'TOTAL']), (cob, ['IMPORTE']),
                (det, ['CANTIDAD', 'PRECIO_UNITARIO', 'DESCUENTO', 'TOTAL_LINEA', 'COSTO_UNITARIO']),
                (pfd, ['CANTIDAD', 'PRECIO_UNITARIO', 'DESCUENTO', 'TOTAL_LINEA', 'COSTO_UNITARIO'])):
    for c in cols:
        d[c] = num(d[c])

# ======================================================================================
# Seudonimizacion de personas fisicas
# ======================================================================================
# Clave secreta: se crea la primera vez y se reutiliza, para que los seudonimos sean estables
# entre cargas. Sin ella no se puede volver del seudonimo al dato original.
ruta_clave = os.path.join(RESTRINGIDO, 'clave_seudonimizacion.txt')
if not os.path.exists(ruta_clave):
    with open(ruta_clave, 'w') as f:
        f.write(secrets.token_hex(32))
with open(ruta_clave) as f:
    CLAVE = f.read().strip()

CORRESPONDENCIAS = []


def es_persona(ident):
    """DNI (8 cifras) o RUC de persona natural (empieza por 10, 15 o 17)."""
    return len(ident) == 8 and ident != '00000000' or (len(ident) == 11 and ident[:2] in ('10', '15', '17'))


def seudonimo_cliente(ident):
    h = hashlib.sha256((CLAVE + ident).encode()).hexdigest()[:10].upper()
    return f'PN-{h}'


def codigos_secuenciales(nombres, prefijo, tipo):
    """Personal de la empresa: VEN-01, USR-01..., por orden de primera aparicion."""
    out = {}
    for i, n in enumerate(nombres, 1):
        out[n] = f'{prefijo}-{i:02d}'
        CORRESPONDENCIAS.append(dict(TIPO=tipo, ORIGINAL=n, SEUDONIMO=out[n]))
    return out


# Vendedores y usuarios: orden por fecha de su primer documento (deterministico)
docs_personal = pd.concat([cab[['FECHA_EMISION', 'VENDEDOR', 'USUARIO']], pfc[['FECHA_EMISION', 'VENDEDOR', 'USUARIO']]])
orden_vend = (docs_personal[docs_personal.VENDEDOR != 'MOSTRADOR'].groupby('VENDEDOR').FECHA_EMISION.min()
              .reset_index().sort_values(['FECHA_EMISION', 'VENDEDOR']).VENDEDOR)
MAPA_VEND = codigos_secuenciales(orden_vend, 'VEN', 'VENDEDOR')
MAPA_VEND['MOSTRADOR'] = 'MOSTRADOR'
orden_usr = (docs_personal.groupby('USUARIO').FECHA_EMISION.min().reset_index()
             .sort_values(['FECHA_EMISION', 'USUARIO']).USUARIO)
MAPA_USR = codigos_secuenciales(orden_usr, 'USR', 'USUARIO EMISOR')

# Clientes personas: identificador y nombre sustituidos
cli['CLIENTE_ID'] = cli.RUC_DNI
cli['NOMBRE'] = cli.RAZON_SOCIAL
personas = cli.RUC_DNI.map(es_persona)
for i in cli.index[personas]:
    s = seudonimo_cliente(cli.at[i, 'RUC_DNI'])
    CORRESPONDENCIAS.append(dict(TIPO='CLIENTE PERSONA', ORIGINAL=f"{cli.at[i, 'RUC_DNI']} | {cli.at[i, 'RAZON_SOCIAL']}",
                                 SEUDONIMO=s))
    cli.at[i, 'CLIENTE_ID'] = s
    cli.at[i, 'NOMBRE'] = f'PERSONA {s[3:]}'
MAPA_CLI = dict(zip(cli.RUC_DNI, cli.CLIENTE_ID))
pd.DataFrame(CORRESPONDENCIAS).to_csv(os.path.join(RESTRINGIDO, 'correspondencias_seudonimos.csv'), index=False)

# ======================================================================================
# DIMENSIONES
# ======================================================================================
# --- dim_fecha ---------------------------------------------------------------------------
def domingo_de_pascua(anio):
    """Algoritmo de Meeus/Jones/Butcher (calendario gregoriano)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return pd.Timestamp(anio, mes, dia)


FERIADOS_FIJOS = {(1, 1): 'Ano Nuevo', (5, 1): 'Dia del Trabajo', (6, 7): 'Batalla de Arica y Dia de la Bandera',
                  (6, 29): 'San Pedro y San Pablo', (7, 23): 'Dia de la Fuerza Aerea', (7, 28): 'Fiestas Patrias',
                  (7, 29): 'Fiestas Patrias', (8, 6): 'Batalla de Junin', (8, 30): 'Santa Rosa de Lima',
                  (10, 8): 'Combate de Angamos', (11, 1): 'Todos los Santos', (12, 8): 'Inmaculada Concepcion',
                  (12, 9): 'Batalla de Ayacucho', (12, 25): 'Navidad'}
MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre',
         'noviembre', 'diciembre']
DIAS = ['lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo']

fechas_datos = pd.to_datetime(pd.concat([cab.FECHA_EMISION, cab.FECHA_VENCIMIENTO, pfc.FECHA_EMISION,
                                         pfc.FECHA_VALIDEZ, cob.FECHA_COBRO]))
calendario = pd.date_range(f'{fechas_datos.min().year}-01-01', f'{fechas_datos.max().year}-12-31')
feriados = {}
for anio in sorted(set(calendario.year)):
    for (m, d), nombre in FERIADOS_FIJOS.items():
        feriados[pd.Timestamp(anio, m, d)] = nombre
    pascua = domingo_de_pascua(anio)
    feriados[pascua - pd.Timedelta(days=3)] = 'Jueves Santo'
    feriados[pascua - pd.Timedelta(days=2)] = 'Viernes Santo'
dim_fecha = pd.DataFrame({'fecha': calendario})
dim_fecha['fecha_key'] = dim_fecha.fecha.dt.strftime('%Y%m%d').astype(int)
dim_fecha['anio'] = dim_fecha.fecha.dt.year
dim_fecha['trimestre'] = dim_fecha.fecha.dt.quarter
dim_fecha['mes'] = dim_fecha.fecha.dt.month
dim_fecha['nombre_mes'] = dim_fecha.mes.map(lambda m: MESES[m - 1])
dim_fecha['anio_mes'] = dim_fecha.fecha.dt.strftime('%Y-%m')
dim_fecha['semana_iso'] = dim_fecha.fecha.dt.isocalendar().week.astype(int)
dim_fecha['dia_mes'] = dim_fecha.fecha.dt.day
dim_fecha['dia_semana'] = dim_fecha.fecha.dt.dayofweek + 1
dim_fecha['nombre_dia'] = dim_fecha.fecha.dt.dayofweek.map(lambda d: DIAS[d])
dim_fecha['nombre_feriado'] = dim_fecha.fecha.map(feriados).fillna('')
dim_fecha['es_feriado'] = dim_fecha.nombre_feriado != ''
# La empresa atiende de lunes a sabado
dim_fecha['es_laborable'] = (dim_fecha.dia_semana <= 6) & ~dim_fecha.es_feriado
dim_fecha['fecha'] = dim_fecha.fecha.dt.strftime('%Y-%m-%d')
dim_fecha = dim_fecha[['fecha_key', 'fecha', 'anio', 'trimestre', 'mes', 'nombre_mes', 'anio_mes', 'semana_iso',
                       'dia_mes', 'dia_semana', 'nombre_dia', 'es_feriado', 'nombre_feriado', 'es_laborable']]
dim_fecha = con_desconocido(dim_fecha, 'fecha_key', dict(fecha='', anio=0, trimestre=0, mes=0, anio_mes='', semana_iso=0,
                                                         dia_mes=0, dia_semana=0, nombre_dia='', es_feriado=False,
                                                         nombre_feriado='', es_laborable=False, nombre_mes='SIN FECHA'))

# --- dim_cliente (dimension lentamente cambiante de tipo 2) -----------------------------
# Atributos con historia (tipo 2): si cambian, se cierra la version anterior y se abre otra.
# Atributos que se sobrescriben (tipo 1): correcciones, no cambios reales del cliente.
ATRIB_TIPO2 = ['tipo_cliente', 'sector', 'departamento', 'provincia', 'condicion_credito', 'agente_retencion',
               'vendedor_asignado']
ATRIB_TIPO1 = ['nombre', 'tipo_documento_id', 'fecha_alta']
nueva = pd.DataFrame({
    'cliente_id': cli.CLIENTE_ID, 'nombre': cli.NOMBRE, 'tipo_documento_id': cli.TIPO_DOC_ID,
    'tipo_cliente': cli.TIPO, 'sector': cli.SECTOR,
    'departamento': cli.DEP.replace('', 'SIN DATO'), 'provincia': cli.PROV.replace('', 'SIN DATO'),
    'condicion_credito': cli.CREDITO, 'agente_retencion': cli.AGENTE_RETENCION,
    'vendedor_asignado': cli.VENDEDOR_ASIGNADO.map(MAPA_VEND).fillna(cli.VENDEDOR_ASIGNADO), 'fecha_alta': cli.FECHA_ALTA,
})

ruta_previa = os.path.join(ORO, 'dim_cliente.csv')
historia = []
if os.path.exists(ruta_previa):
    previa = pd.read_csv(ruta_previa, dtype=str, keep_default_na=False)
    previa = previa[previa.cliente_key.astype(int) != DESCONOCIDO]
    previa['cliente_key'] = previa.cliente_key.astype(int)
    previa['version'] = previa.version.astype(int)
    vigentes = previa[previa.es_actual == 'True'].set_index('cliente_id')
    siguiente_clave = previa.cliente_key.max() + 1
    filas = [previa]
    for _, r in nueva.iterrows():
        if r.cliente_id not in vigentes.index:                       # cliente nuevo
            filas.append(pd.DataFrame([{**r, 'cliente_key': siguiente_clave, 'version': 1,
                                        'vigente_desde': INICIO_VIGENCIA.strftime('%Y-%m-%d'),
                                        'vigente_hasta': FIN_VIGENCIA.strftime('%Y-%m-%d'), 'es_actual': 'True'}]))
            historia.append(('alta', r.cliente_id, ''))
            siguiente_clave += 1
            continue
        v = vigentes.loc[r.cliente_id]
        cambios = [a for a in ATRIB_TIPO2 if str(v[a]) != str(r[a])]
        idx = previa.index[(previa.cliente_id == r.cliente_id)]
        for a in ATRIB_TIPO1:                                         # tipo 1: se corrige en todas las versiones
            previa.loc[idx, a] = r[a]
        if cambios:                                                   # tipo 2: nueva version
            i_act = previa.index[(previa.cliente_id == r.cliente_id) & (previa.es_actual == 'True')]
            previa.loc[i_act, 'vigente_hasta'] = (FECHA_CARGA - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            previa.loc[i_act, 'es_actual'] = 'False'
            filas.append(pd.DataFrame([{**r, 'cliente_key': siguiente_clave, 'version': int(v.version) + 1,
                                        'vigente_desde': FECHA_CARGA.strftime('%Y-%m-%d'),
                                        'vigente_hasta': FIN_VIGENCIA.strftime('%Y-%m-%d'), 'es_actual': 'True'}]))
            historia.append(('nueva version', r.cliente_id, ', '.join(cambios)))
            siguiente_clave += 1
    dim_cliente = pd.concat(filas, ignore_index=True)
else:                                                                 # carga inicial: una version por cliente
    dim_cliente = nueva.copy()
    dim_cliente.insert(0, 'cliente_key', range(1, len(nueva) + 1))
    dim_cliente['version'] = 1
    dim_cliente['vigente_desde'] = INICIO_VIGENCIA.strftime('%Y-%m-%d')
    dim_cliente['vigente_hasta'] = FIN_VIGENCIA.strftime('%Y-%m-%d')
    dim_cliente['es_actual'] = 'True'
dim_cliente['es_actual'] = dim_cliente.es_actual.astype(str) == 'True'
dim_cliente = dim_cliente[['cliente_key', 'cliente_id', 'nombre', 'tipo_documento_id', 'tipo_cliente', 'sector',
                           'departamento', 'provincia', 'condicion_credito', 'agente_retencion', 'vendedor_asignado',
                           'fecha_alta', 'version', 'vigente_desde', 'vigente_hasta', 'es_actual']]
dim_cliente = con_desconocido(dim_cliente, 'cliente_key', dict(version=0, vigente_desde='1900-01-01',
                                                               vigente_hasta='9999-12-31', es_actual=True))


def clave_cliente(ruc, fecha):
    """Version del cliente vigente en la fecha del hecho."""
    ids = pd.Series(ruc).map(MAPA_CLI)
    f = pd.to_datetime(pd.Series(fecha))
    tabla = pd.DataFrame({'cliente_id': ids.values, 'f': f.values, 'orden': range(len(ids))})
    d = dim_cliente[dim_cliente.cliente_key != DESCONOCIDO][['cliente_key', 'cliente_id', 'vigente_desde', 'vigente_hasta']].copy()
    d['desde'] = pd.to_datetime(d.vigente_desde)
    d['hasta'] = pd.to_datetime(d.vigente_hasta.replace('9999-12-31', '2262-04-11'))
    m = tabla.merge(d, on='cliente_id', how='left')
    m = m[(m.f >= m.desde) & (m.f <= m.hasta)].drop_duplicates('orden')
    return tabla.orden.map(m.set_index('orden').cliente_key).fillna(DESCONOCIDO).astype(int).values


# --- dim_producto -------------------------------------------------------------------------
LINEA = {'SERVICIOS TECNICOS': 'SERVICIOS', 'ALQUILER DE EQUIPOS': 'ALQUILER'}
dim_producto = pd.DataFrame({
    'producto_key': range(1, len(prod) + 1), 'codigo_producto': prod.CODIGO_PROD, 'descripcion': prod.DESCRIPCION,
    'familia': prod.FAMILIA, 'linea_negocio': prod.FAMILIA.map(LINEA).fillna('VENTA DE MATERIALES'),
    'marca': prod.MARCA, 'origen': prod.ORIGEN, 'precio_lista_pen': num(prod.PRECIO_BASE)})
dim_producto = con_desconocido(dim_producto, 'producto_key', dict(precio_lista_pen=0.0))
MAPA_PROD = dict(zip(dim_producto.codigo_producto, dim_producto.producto_key))

# --- dim_vendedor ---------------------------------------------------------------------------
ventas_vend = cab[cab.ESTADO == 'ACEPTADO'].groupby('VENDEDOR').FECHA_EMISION.agg(['min', 'max'])
filas = []
for n, cod in MAPA_VEND.items():
    filas.append(dict(vendedor_codigo=cod, tipo_canal='MOSTRADOR' if n == 'MOSTRADOR' else 'VENDEDOR',
                      primera_venta=ventas_vend.at[n, 'min'] if n in ventas_vend.index else '',
                      ultima_venta=ventas_vend.at[n, 'max'] if n in ventas_vend.index else ''))
dim_vendedor = pd.DataFrame(filas).sort_values('vendedor_codigo').reset_index(drop=True)
corte = cab.FECHA_EMISION.max()
# Activo: ha vendido en los ultimos 60 dias del periodo
dim_vendedor['activo_al_corte'] = pd.to_datetime(dim_vendedor.ultima_venta) >= pd.Timestamp(corte) - pd.Timedelta(days=60)
dim_vendedor.insert(0, 'vendedor_key', range(1, len(dim_vendedor) + 1))
dim_vendedor = con_desconocido(dim_vendedor, 'vendedor_key', dict(primera_venta='', ultima_venta='', activo_al_corte=False))
CLAVE_VEND = {n: dim_vendedor.set_index('vendedor_codigo').at[c, 'vendedor_key'] for n, c in MAPA_VEND.items()}

# --- dim_usuario (personal que emite los documentos) --------------------------------------
dim_usuario = pd.DataFrame({'usuario_key': range(1, len(MAPA_USR) + 1), 'usuario_codigo': list(MAPA_USR.values())})
dim_usuario = con_desconocido(dim_usuario, 'usuario_key', {})
CLAVE_USR = {n: i + 1 for i, n in enumerate(MAPA_USR)}

# --- dimensiones pequenas -------------------------------------------------------------------
dim_moneda = con_desconocido(pd.DataFrame({'moneda_key': [1, 2], 'codigo_iso': ['PEN', 'USD'],
                                           'nombre': ['Sol peruano', 'Dolar estadounidense'], 'simbolo': ['S/', 'US$']}),
                             'moneda_key', {})
CLAVE_MON = {'PEN': 1, 'USD': 2}

dim_tipo_documento = con_desconocido(pd.DataFrame({
    'tipo_documento_key': [1, 2, 3, 4],
    'tipo_documento': ['FACTURA', 'BOLETA', 'NOTA DE CREDITO', 'PROFORMA'],
    'series': ['FT01', 'B001', 'FC01, BC01', 'PF01'],
    'es_comprobante_sunat': [True, True, True, False],
    'signo': [1, 1, -1, 0],
    'descripcion': ['Venta a cliente con RUC; da derecho a credito fiscal', 'Venta a consumidor final (DNI)',
                    'Anula o corrige total o parcialmente un comprobante', 'Oferta comercial; no tiene efecto tributario']}),
    'tipo_documento_key', dict(signo=0, es_comprobante_sunat=False))
CLAVE_TD = dict(zip(dim_tipo_documento.tipo_documento, dim_tipo_documento.tipo_documento_key))

dim_condicion_pago = con_desconocido(pd.DataFrame({
    'condicion_pago_key': [1, 2, 3, 4], 'condicion_pago': ['CONTADO', '15 DIAS', '30 DIAS', '45 DIAS'],
    'dias_credito': [0, 15, 30, 45], 'es_credito': [False, True, True, True]}),
    'condicion_pago_key', dict(dias_credito=0, es_credito=False))
CLAVE_CP = dict(zip(dim_condicion_pago.condicion_pago, dim_condicion_pago.condicion_pago_key))

CATEGORIA_PAGO = {'EFECTIVO': 'EFECTIVO', 'YAPE': 'BILLETERA DIGITAL', 'TARJETA': 'TARJETA', 'CHEQUE': 'CHEQUE',
                  'TRANSFERENCIA BCP': 'TRANSFERENCIA', 'TRANSFERENCIA BBVA': 'TRANSFERENCIA',
                  'TRANSFERENCIA INTERBANK': 'TRANSFERENCIA',
                  'DEPOSITO CTA DETRACCIONES BN': 'TRIBUTARIO', 'COMPROBANTE DE RETENCION': 'TRIBUTARIO'}
medios = sorted(cob.MEDIO_PAGO.unique())
dim_forma_pago = pd.DataFrame({'forma_pago_key': range(1, len(medios) + 1), 'medio_pago': medios,
                               'categoria': [CATEGORIA_PAGO.get(m, 'OTRO') for m in medios]})
dim_forma_pago['entra_en_caja'] = dim_forma_pago.categoria != 'TRIBUTARIO'
dim_forma_pago = con_desconocido(dim_forma_pago, 'forma_pago_key', dict(entra_en_caja=False))
CLAVE_FP = dict(zip(dim_forma_pago.medio_pago, dim_forma_pago.forma_pago_key))

ESTADOS_PF = ['ABIERTA', 'CERRADA', 'VENCIDA', 'RECHAZADA']
dim_estado_proforma = con_desconocido(pd.DataFrame({
    'estado_proforma_key': [1, 2, 3, 4], 'estado': ESTADOS_PF,
    'es_resuelta': [False, True, True, True], 'es_ganada': [False, True, False, False],
    'descripcion': ['Vigente y sin venta', 'Tiene al menos una venta', 'Paso la validez sin venta',
                    'El cliente la rechazo']}), 'estado_proforma_key', dict(es_resuelta=False, es_ganada=False))
CLAVE_EP = dict(zip(dim_estado_proforma.estado, dim_estado_proforma.estado_proforma_key))

# ======================================================================================
# Tipo de cambio: se usa el del dia; si falta (domingo, feriado), el ultimo publicado
# ======================================================================================
tc = pd.read_csv(args.tipo_cambio)
tc['FECHA'] = pd.to_datetime(tc.FECHA)
tc = tc.sort_values('FECHA')


def tipo_cambio(fechas, monedas):
    f = pd.DataFrame({'f': pd.to_datetime(pd.Series(fechas).values), 'orden': range(len(fechas))}).sort_values('f')
    m = pd.merge_asof(f, tc, left_on='f', right_on='FECHA', direction='backward').set_index('orden').sort_index()
    return np.where(pd.Series(monedas).values == 'USD', m.TC_VENTA_USD_PEN.values, 1.0)


# ======================================================================================
# HECHOS
# ======================================================================================
# --- fact_ventas: una fila por linea de comprobante aceptado ---------------------------------
# Las notas de credito restan (signo -1). Las de descuento o correccion de precio no
# devuelven unidades: su cantidad neta es 0 aunque la linea repita la cantidad original.
v = det.merge(cab, on=['SERIE', 'NUMERO'])
v = v[v.ESTADO == 'ACEPTADO'].copy()
es_nc = v.TIPO_DOC == 'NOTA DE CREDITO'
signo = np.where(es_nc, -1, 1)
devuelve_unidades = ~es_nc | v.MOTIVO_NC.isin(['DEVOLUCION DE MERCADERIA', 'ANULACION DE LA OPERACION'])
v['tc'] = tipo_cambio(v.FECHA_EMISION, v.MONEDA)
peso_linea = v.TOTAL_LINEA / v.SUBTOTAL  # para repartir el IGV de la cabecera entre las lineas
fact_ventas = pd.DataFrame({
    'fecha_emision_key': clave_fecha(v.FECHA_EMISION).values,
    'fecha_vencimiento_key': clave_fecha(v.FECHA_VENCIMIENTO).values,
    'cliente_key': clave_cliente(v.RUC_CLIENTE, v.FECHA_EMISION),
    'producto_key': v.CODIGO_PROD.map(MAPA_PROD).fillna(DESCONOCIDO).astype(int).values,
    'vendedor_key': v.VENDEDOR.map(CLAVE_VEND).fillna(DESCONOCIDO).astype(int).values,
    'usuario_key': v.USUARIO.map(CLAVE_USR).fillna(DESCONOCIDO).astype(int).values,
    'moneda_key': v.MONEDA.map(CLAVE_MON).fillna(DESCONOCIDO).astype(int).values,
    'tipo_documento_key': v.TIPO_DOC.map(CLAVE_TD).fillna(DESCONOCIDO).astype(int).values,
    'condicion_pago_key': v.CONDICION_PAGO.map(CLAVE_CP).fillna(DESCONOCIDO).astype(int).values,
    # dimensiones degeneradas: identifican el documento sin necesidad de otra tabla
    'serie': v.SERIE.values, 'numero': v.NUMERO.astype(int).values, 'item': v.ITEM.astype(int).values,
    'num_proforma': v.NUM_PROFORMA.values, 'documento_referencia': v.DOC_REF.values, 'motivo_nc': v.MOTIVO_NC.values,
    # medidas
    'cantidad': (signo * np.where(devuelve_unidades, v.CANTIDAD, 0)).astype(int),
    'precio_unitario_doc': v.PRECIO_UNITARIO.values,
    'importe_neto_doc': (signo * v.TOTAL_LINEA).round(2).values,
    'tipo_cambio': v.tc.values,
    'descuento_pen': (signo * v.DESCUENTO * v.tc).round(2).values,
    'importe_neto_pen': (signo * v.TOTAL_LINEA * v.tc).round(2).values,
    'igv_doc': (signo * v.IGV * peso_linea).round(2).values,
    'igv_pen': (signo * v.IGV * peso_linea * v.tc).round(2).values,
    'coste_pen': (signo * v.CANTIDAD * v.COSTO_UNITARIO * v.tc).round(2).values,
})
fact_ventas['margen_pen'] = (fact_ventas.importe_neto_pen - fact_ventas.coste_pen).round(2)
fact_ventas.insert(0, 'venta_linea_id', range(1, len(fact_ventas) + 1))

# --- fact_proformas: una fila por linea de proforma -------------------------------------------
p = pfd.merge(pfc, on=['SERIE', 'NUMERO'])
p['tc'] = tipo_cambio(p.FECHA_EMISION, p.MONEDA)
fact_proformas = pd.DataFrame({
    'fecha_emision_key': clave_fecha(p.FECHA_EMISION).values,
    'fecha_validez_key': clave_fecha(p.FECHA_VALIDEZ).values,
    'cliente_key': clave_cliente(p.RUC_CLIENTE, p.FECHA_EMISION),
    'producto_key': p.CODIGO_PROD.map(MAPA_PROD).fillna(DESCONOCIDO).astype(int).values,
    'vendedor_key': p.VENDEDOR.map(CLAVE_VEND).fillna(DESCONOCIDO).astype(int).values,
    'usuario_key': p.USUARIO.map(CLAVE_USR).fillna(DESCONOCIDO).astype(int).values,
    'moneda_key': p.MONEDA.map(CLAVE_MON).fillna(DESCONOCIDO).astype(int).values,
    'estado_proforma_key': p.ESTADO.map(CLAVE_EP).fillna(DESCONOCIDO).astype(int).values,
    'num_proforma': p.NUMERO.astype(int).values, 'item': p.ITEM.astype(int).values,
    'cantidad': p.CANTIDAD.astype(int).values,
    'precio_unitario_doc': p.PRECIO_UNITARIO.values,
    'importe_ofertado_doc': p.TOTAL_LINEA.round(2).values,
    'tipo_cambio': p.tc.values,
    'importe_ofertado_pen': (p.TOTAL_LINEA * p.tc).round(2).values,
    'coste_pen': (p.CANTIDAD * p.COSTO_UNITARIO * p.tc).round(2).values,
})
fact_proformas.insert(0, 'proforma_linea_id', range(1, len(fact_proformas) + 1))

# --- fact_embudo_proforma: una fila por proforma (instantanea acumulativa) --------------------
# Sigue cada proforma desde la emision hasta su primera venta. Es la tabla del embudo comercial:
# tasa de conversion, tiempo hasta la venta y cuanto de lo ofertado se acaba facturando.
ventas_ok = cab[(cab.ESTADO == 'ACEPTADO') & (cab.TIPO_DOC != 'NOTA DE CREDITO')]
e = enl.merge(ventas_ok, left_on=['SERIE_DOC', 'NUMERO_DOC'], right_on=['SERIE', 'NUMERO'])
e['tc'] = tipo_cambio(e.FECHA_EMISION, e.MONEDA)
e['facturado_pen'] = e.SUBTOTAL * e.tc
por_pf = e.groupby('NUMERO_PROFORMA').agg(primera_venta=('FECHA_EMISION', 'min'), n_comprobantes=('NUMERO', 'count'),
                                          facturado_pen=('facturado_pen', 'sum'))
h = pfc.copy()
h['tc'] = tipo_cambio(h.FECHA_EMISION, h.MONEDA)
h = h.merge(por_pf, left_on='NUMERO', right_index=True, how='left')
dias = (pd.to_datetime(h.primera_venta) - pd.to_datetime(h.FECHA_EMISION)).dt.days
fact_embudo = pd.DataFrame({
    'num_proforma': h.NUMERO.astype(int).values,
    'fecha_emision_key': clave_fecha(h.FECHA_EMISION).values,
    'fecha_validez_key': clave_fecha(h.FECHA_VALIDEZ).values,
    'fecha_primera_venta_key': clave_fecha(h.primera_venta).values,
    'cliente_key': clave_cliente(h.RUC_CLIENTE, h.FECHA_EMISION),
    'vendedor_key': h.VENDEDOR.map(CLAVE_VEND).fillna(DESCONOCIDO).astype(int).values,
    'moneda_key': h.MONEDA.map(CLAVE_MON).fillna(DESCONOCIDO).astype(int).values,
    'estado_proforma_key': h.ESTADO.map(CLAVE_EP).fillna(DESCONOCIDO).astype(int).values,
    'convertida': h.primera_venta.notna().astype(int).values,
    'dias_hasta_venta': dias.astype('Int64').values,
    'n_comprobantes': h.n_comprobantes.fillna(0).astype(int).values,
    'importe_ofertado_pen': (h.SUBTOTAL * h.tc).round(2).values,
    'importe_facturado_pen': h.facturado_pen.fillna(0).round(2).values,
    'pendiente_revision': (h.REVISAR != '').values,
})

# --- fact_cobros: una fila por movimiento de cobro --------------------------------------------
c = cob.merge(cab[['SERIE', 'NUMERO', 'RUC_CLIENTE', 'FECHA_EMISION', 'FECHA_VENCIMIENTO', 'CONDICION_PAGO']],
              left_on=['SERIE_DOC', 'NUMERO_DOC'], right_on=['SERIE', 'NUMERO'], how='left')
c['tc'] = tipo_cambio(c.FECHA_COBRO, c.MONEDA)
fact_cobros = pd.DataFrame({
    'cobro_id': c.ID_COBRO.values,
    'fecha_cobro_key': clave_fecha(c.FECHA_COBRO).values,
    'fecha_emision_doc_key': clave_fecha(c.FECHA_EMISION).values,
    'fecha_vencimiento_doc_key': clave_fecha(c.FECHA_VENCIMIENTO).values,
    'cliente_key': clave_cliente(c.RUC_CLIENTE, c.FECHA_EMISION),
    'moneda_key': c.MONEDA.map(CLAVE_MON).fillna(DESCONOCIDO).astype(int).values,
    'forma_pago_key': c.MEDIO_PAGO.map(CLAVE_FP).fillna(DESCONOCIDO).astype(int).values,
    'condicion_pago_key': c.CONDICION_PAGO.map(CLAVE_CP).fillna(DESCONOCIDO).astype(int).values,
    'serie_doc': c.SERIE_DOC.values, 'numero_doc': c.NUMERO_DOC.astype(int).values,
    'tipo_movimiento': c.TIPO_MOVIMIENTO.values,
    'importe_doc': c.IMPORTE.round(2).values,
    'tipo_cambio': c.tc.values,
    'importe_pen': (c.IMPORTE * c.tc).round(2).values,
    'dias_desde_emision': (pd.to_datetime(c.FECHA_COBRO) - pd.to_datetime(c.FECHA_EMISION)).dt.days.values,
    'dias_sobre_vencimiento': (pd.to_datetime(c.FECHA_COBRO) - pd.to_datetime(c.FECHA_VENCIMIENTO)).dt.days.values,
    'pendiente_revision': (c.REVISAR != '').values,
})

# ======================================================================================
# Conciliacion con la capa plata
# ======================================================================================
conc = []


def comprobar(nombre, esperado, obtenido, tol=0.01):
    conc.append(dict(COMPROBACION=nombre, PLATA=round(float(esperado), 2), ORO=round(float(obtenido), 2),
                     CUADRA=abs(float(esperado) - float(obtenido)) <= tol))


acept = cab[cab.ESTADO == 'ACEPTADO']
for mon in ('PEN', 'USD'):
    a = acept[acept.MONEDA == mon]
    esperado = a.SUBTOTAL.where(a.TIPO_DOC != 'NOTA DE CREDITO', -a.SUBTOTAL).sum()
    obtenido = fact_ventas[fact_ventas.moneda_key == CLAVE_MON[mon]].importe_neto_doc.sum()
    comprobar(f'Ventas netas sin IGV en {mon} (comprobantes aceptados)', esperado, obtenido)
    esperado_igv = a.IGV.where(a.TIPO_DOC != 'NOTA DE CREDITO', -a.IGV).sum()
    obtenido_igv = fact_ventas[fact_ventas.moneda_key == CLAVE_MON[mon]].igv_doc.sum()
    comprobar(f'IGV en {mon} (repartido por lineas)', esperado_igv, obtenido_igv, tol=1.0)
    comprobar(f'Cobros en {mon}', cob[cob.MONEDA == mon].IMPORTE.sum(),
              fact_cobros[fact_cobros.moneda_key == CLAVE_MON[mon]].importe_doc.sum())
    comprobar(f'Proformas ofertadas en {mon}', pfc[pfc.MONEDA == mon].SUBTOTAL.sum(),
              fact_proformas[fact_proformas.moneda_key == CLAVE_MON[mon]].importe_ofertado_doc.sum())
comprobar('Lineas de comprobantes aceptados', len(det.merge(acept[['SERIE', 'NUMERO']])), len(fact_ventas), 0)
comprobar('Lineas de proforma', len(pfd), len(fact_proformas), 0)
comprobar('Proformas', len(pfc), len(fact_embudo), 0)
comprobar('Proformas convertidas = proformas con enlace', enl.merge(ventas_ok, left_on=['SERIE_DOC', 'NUMERO_DOC'],
          right_on=['SERIE', 'NUMERO']).NUMERO_PROFORMA.nunique(), fact_embudo.convertida.sum(), 0)
comprobar('Movimientos de cobro', len(cob), len(fact_cobros), 0)
for nombre, f in (('fact_ventas', fact_ventas), ('fact_proformas', fact_proformas), ('fact_cobros', fact_cobros),
                  ('fact_embudo_proforma', fact_embudo)):
    claves = [k for k in f.columns if k.endswith('_key') and k != 'fecha_primera_venta_key']
    sin = int((f[claves] == DESCONOCIDO).sum().sum())
    comprobar(f'{nombre}: claves sin resolver (miembro desconocido)', 0, sin, 0)
comprobar('Importes en USD sin tipo de cambio', 0,
          int(pd.isna(fact_ventas.tipo_cambio).sum() + pd.isna(fact_cobros.tipo_cambio).sum()), 0)
conciliacion = pd.DataFrame(conc)

# ======================================================================================
# Diccionario de datos (documenta cada columna; lo usa tambien el asistente de consulta)
# ======================================================================================
DESCRIPCION = {
    'dim_fecha': ('Calendario con feriados nacionales de Peru. Una fila por dia.', {
        'fecha_key': 'Clave AAAAMMDD; -1 = sin fecha', 'fecha': 'Fecha (aaaa-mm-dd)', 'anio': 'Ano', 'trimestre': 'Trimestre (1-4)',
        'mes': 'Mes (1-12)', 'nombre_mes': 'Nombre del mes', 'anio_mes': 'Ano y mes (aaaa-mm)', 'semana_iso': 'Semana ISO',
        'dia_mes': 'Dia del mes', 'dia_semana': 'Dia de la semana (1 = lunes)', 'nombre_dia': 'Nombre del dia',
        'es_feriado': 'Feriado nacional', 'nombre_feriado': 'Nombre del feriado',
        'es_laborable': 'Dia de atencion (lunes a sabado, no feriado)'}),
    'dim_cliente': ('Clientes. Dimension lentamente cambiante de tipo 2: una fila por version. Las personas fisicas estan seudonimizadas.', {
        'cliente_key': 'Clave subrogada (una por version)', 'cliente_id': 'RUC de empresa o seudonimo PN-... de persona',
        'nombre': 'Razon social (empresas) o seudonimo (personas)', 'tipo_documento_id': 'RUC o DNI',
        'tipo_cliente': 'EMPRESA, PERSONA CON NEGOCIO o PERSONA NATURAL', 'sector': 'Sector de actividad',
        'departamento': 'Departamento (SIN DATO si se desconoce)', 'provincia': 'Provincia (SIN DATO si se desconoce)',
        'condicion_credito': 'Condicion de credito concedida', 'agente_retencion': 'SI si el cliente es agente de retencion del IGV',
        'vendedor_asignado': 'Codigo del vendedor que lleva la cuenta', 'fecha_alta': 'Fecha de alta como cliente',
        'version': 'Numero de version del cliente', 'vigente_desde': 'Inicio de vigencia de esta version',
        'vigente_hasta': 'Fin de vigencia (9999-12-31 = vigente)', 'es_actual': 'Version vigente'}),
    'dim_producto': ('Catalogo de productos y servicios.', {
        'producto_key': 'Clave subrogada', 'codigo_producto': 'Codigo del sistema (PRD-0000)', 'descripcion': 'Descripcion',
        'familia': 'Familia del catalogo (16)', 'linea_negocio': 'VENTA DE MATERIALES, SERVICIOS o ALQUILER',
        'marca': 'Marca', 'origen': 'NACIONAL o IMPORTADO', 'precio_lista_pen': 'Precio de lista sin IGV en soles'}),
    'dim_vendedor': ('Vendedores (seudonimizados) y canal de mostrador.', {
        'vendedor_key': 'Clave subrogada', 'vendedor_codigo': 'VEN-nn o MOSTRADOR', 'tipo_canal': 'VENDEDOR o MOSTRADOR',
        'primera_venta': 'Fecha de su primera venta', 'ultima_venta': 'Fecha de su ultima venta',
        'activo_al_corte': 'Ha vendido en los 60 dias anteriores al cierre de los datos'}),
    'dim_usuario': ('Personal que emite los documentos (seudonimizado).', {
        'usuario_key': 'Clave subrogada', 'usuario_codigo': 'USR-nn'}),
    'dim_moneda': ('Monedas.', {'moneda_key': 'Clave', 'codigo_iso': 'Codigo ISO 4217', 'nombre': 'Nombre', 'simbolo': 'Simbolo'}),
    'dim_tipo_documento': ('Tipos de documento comercial.', {
        'tipo_documento_key': 'Clave', 'tipo_documento': 'FACTURA, BOLETA, NOTA DE CREDITO o PROFORMA', 'series': 'Series usadas',
        'es_comprobante_sunat': 'Comprobante de pago electronico', 'signo': '1 suma ventas, -1 resta, 0 sin efecto',
        'descripcion': 'Descripcion'}),
    'dim_condicion_pago': ('Condiciones de pago.', {
        'condicion_pago_key': 'Clave', 'condicion_pago': 'CONTADO o n DIAS', 'dias_credito': 'Dias de credito',
        'es_credito': 'Venta a credito'}),
    'dim_forma_pago': ('Medios de cobro.', {
        'forma_pago_key': 'Clave', 'medio_pago': 'Medio de pago normalizado',
        'categoria': 'EFECTIVO, TRANSFERENCIA, BILLETERA DIGITAL, TARJETA, CHEQUE o TRIBUTARIO',
        'entra_en_caja': 'FALSE para detracciones y retenciones (no llegan a la cuenta de la empresa)'}),
    'dim_estado_proforma': ('Estado de la proforma derivado en la capa plata.', {
        'estado_proforma_key': 'Clave', 'estado': 'ABIERTA, CERRADA, VENCIDA o RECHAZADA', 'es_resuelta': 'Ya no esta abierta',
        'es_ganada': 'Termino en venta', 'descripcion': 'Descripcion'}),
    'fact_ventas': ('Ventas. Grano: una linea de comprobante aceptado (factura, boleta o nota de credito). Las notas de credito restan.', {
        'venta_linea_id': 'Identificador de la fila', 'fecha_emision_key': 'Fecha de emision', 'fecha_vencimiento_key': 'Fecha de vencimiento',
        'cliente_key': 'Cliente (version vigente en la fecha)', 'producto_key': 'Producto', 'vendedor_key': 'Vendedor',
        'usuario_key': 'Usuario emisor', 'moneda_key': 'Moneda del documento', 'tipo_documento_key': 'Tipo de documento',
        'condicion_pago_key': 'Condicion de pago', 'serie': 'Serie del comprobante', 'numero': 'Numero del comprobante',
        'item': 'Linea dentro del comprobante', 'num_proforma': 'Proforma de origen (vacio si es venta directa)',
        'documento_referencia': 'Comprobante que corrige la nota de credito', 'motivo_nc': 'Motivo de la nota de credito',
        'cantidad': 'Unidades netas (las notas de credito de precio no devuelven unidades)',
        'precio_unitario_doc': 'Precio unitario sin IGV en la moneda del documento',
        'importe_neto_doc': 'Importe sin IGV en la moneda del documento (con signo)', 'tipo_cambio': 'Soles por dolar aplicado (1 en PEN)',
        'descuento_pen': 'Descuento en soles', 'importe_neto_pen': 'Importe sin IGV en soles (con signo)',
        'igv_doc': 'IGV de la cabecera repartido por lineas, en la moneda del documento',
        'igv_pen': 'IGV de la cabecera repartido por lineas, en soles', 'coste_pen': 'Coste de la mercaderia en soles',
        'margen_pen': 'Importe neto menos coste, en soles'}),
    'fact_proformas': ('Proformas. Grano: una linea de proforma.', {
        'proforma_linea_id': 'Identificador de la fila', 'fecha_emision_key': 'Fecha de emision', 'fecha_validez_key': 'Fecha de validez',
        'cliente_key': 'Cliente', 'producto_key': 'Producto', 'vendedor_key': 'Vendedor', 'usuario_key': 'Usuario emisor',
        'moneda_key': 'Moneda', 'estado_proforma_key': 'Estado', 'num_proforma': 'Numero de proforma', 'item': 'Linea',
        'cantidad': 'Unidades ofertadas', 'precio_unitario_doc': 'Precio unitario sin IGV',
        'importe_ofertado_doc': 'Importe sin IGV en la moneda de la proforma', 'tipo_cambio': 'Soles por dolar aplicado',
        'importe_ofertado_pen': 'Importe sin IGV en soles', 'coste_pen': 'Coste estimado en soles'}),
    'fact_embudo_proforma': ('Embudo comercial. Grano: una proforma, seguida hasta su primera venta (instantanea acumulativa).', {
        'num_proforma': 'Numero de proforma', 'fecha_emision_key': 'Fecha de emision', 'fecha_validez_key': 'Fecha de validez',
        'fecha_primera_venta_key': 'Fecha de la primera venta (-1 si no se vendio)', 'cliente_key': 'Cliente', 'vendedor_key': 'Vendedor',
        'moneda_key': 'Moneda', 'estado_proforma_key': 'Estado', 'convertida': '1 si termino en venta',
        'dias_hasta_venta': 'Dias entre la proforma y la primera venta', 'n_comprobantes': 'Comprobantes emitidos a partir de ella',
        'importe_ofertado_pen': 'Importe ofertado sin IGV en soles', 'importe_facturado_pen': 'Importe facturado sin IGV en soles',
        'pendiente_revision': 'Cerrada sin venta localizada; revisar con la empresa'}),
    'fact_cobros': ('Cobros. Grano: un movimiento (cobro, detraccion o retencion).', {
        'cobro_id': 'Identificador del cobro', 'fecha_cobro_key': 'Fecha del cobro', 'fecha_emision_doc_key': 'Emision del comprobante cobrado',
        'fecha_vencimiento_doc_key': 'Vencimiento del comprobante cobrado', 'cliente_key': 'Cliente', 'moneda_key': 'Moneda',
        'forma_pago_key': 'Medio de pago', 'condicion_pago_key': 'Condicion de pago del comprobante', 'serie_doc': 'Serie del comprobante',
        'numero_doc': 'Numero del comprobante', 'tipo_movimiento': 'COBRO, DETRACCION o RETENCION', 'importe_doc': 'Importe en la moneda del documento',
        'tipo_cambio': 'Soles por dolar del dia del cobro', 'importe_pen': 'Importe en soles',
        'dias_desde_emision': 'Dias entre emision y cobro', 'dias_sobre_vencimiento': 'Dias de retraso (negativo = pago anticipado)',
        'pendiente_revision': 'Cobro marcado para revision en la limpieza'}),
}
TABLAS = {'dim_fecha': dim_fecha, 'dim_cliente': dim_cliente, 'dim_producto': dim_producto, 'dim_vendedor': dim_vendedor,
          'dim_usuario': dim_usuario, 'dim_moneda': dim_moneda, 'dim_tipo_documento': dim_tipo_documento,
          'dim_condicion_pago': dim_condicion_pago, 'dim_forma_pago': dim_forma_pago, 'dim_estado_proforma': dim_estado_proforma,
          'fact_ventas': fact_ventas, 'fact_proformas': fact_proformas, 'fact_embudo_proforma': fact_embudo,
          'fact_cobros': fact_cobros}
dicc = []
for t, df in TABLAS.items():
    desc_t, cols = DESCRIPCION[t]
    faltan = set(df.columns) - set(cols)
    assert not faltan, f'{t}: columnas sin documentar {faltan}'
    for col in df.columns:
        dicc.append(dict(TABLA=t, DESCRIPCION_TABLA=desc_t, COLUMNA=col, TIPO=str(df[col].dtype), DESCRIPCION=cols[col],
                         CLAVE='PK' if col == df.columns[0] and t.startswith('dim') else ('FK' if col.endswith('_key') else '')))
diccionario = pd.DataFrame(dicc)

# ======================================================================================
# Escritura: CSV siempre; DuckDB y Parquet si esta disponible
# ======================================================================================
for t, df in TABLAS.items():
    df.to_csv(os.path.join(ORO, f'{t}.csv'), index=False)
conciliacion.to_csv(os.path.join(ORO, 'conciliacion_oro.csv'), index=False)
diccionario.to_csv(os.path.join(ORO, 'diccionario_datos.csv'), index=False)

VISTAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vistas_oro.sql')
try:
    import duckdb
    ruta_db = os.path.join(ORO, 'almacen.duckdb')
    if os.path.exists(ruta_db):
        os.remove(ruta_db)
    con = duckdb.connect(ruta_db)
    for t, df in TABLAS.items():
        con.register('tmp', df)
        con.execute(f'CREATE TABLE {t} AS SELECT * FROM tmp')
        con.unregister('tmp')
    if os.path.exists(VISTAS):
        con.execute(open(VISTAS, encoding='utf-8').read())
    CATALOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catalogo_indicadores.csv')
    if os.path.exists(CATALOGO):  # el catalogo viaja con el almacen (lo consulta el asistente)
        con.register('tmp', pd.read_csv(CATALOGO, dtype=str))
        con.execute('CREATE TABLE catalogo_indicadores AS SELECT * FROM tmp')
        con.unregister('tmp')
    os.makedirs(os.path.join(ORO, 'parquet'), exist_ok=True)
    for t in TABLAS:
        con.execute(f"COPY {t} TO '{os.path.join(ORO, 'parquet', t + '.parquet')}' (FORMAT PARQUET)")
    vistas = [r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]
    con.close()
    salida_db = f'almacen.duckdb con {len(TABLAS)} tablas, {len(vistas)} vistas y el catalogo de indicadores; copia en parquet/'
except ImportError:
    salida_db = 'DuckDB no instalado: solo CSV (para la base de datos y Parquet: pip install duckdb)'

# ======================================================================================
# Resumen
# ======================================================================================
pd.set_option('display.width', 200, 'display.max_colwidth', 70)
print('TABLAS')
for t, df in TABLAS.items():
    print(f'  {t:<22} {len(df):>6} filas  {len(df.columns):>3} columnas')
if historia:
    print(f'\nHISTORIA DE CLIENTES (SCD2): {len(historia)} cambios en esta carga')
    for tipo, cid, detalle in historia[:10]:
        print(f'  {tipo}: {cid} {detalle}')
print('\nCONCILIACION CON LA CAPA PLATA')
print(conciliacion.to_string(index=False))
ok = conciliacion.CUADRA.all()
print('->', 'OK: el almacen cuadra con la capa plata' if ok else 'ATENCION: hay comprobaciones que no cuadran')
print('\nSALIDA:', ORO, '|', salida_db)
print('Seudonimos y clave en:', RESTRINGIDO, '(no compartir)')
